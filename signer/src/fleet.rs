//! Builders and relay helpers for the orchestration subcommands.

use std::time::Duration;

use buzz_ws_client::connection::NostrWsConnection;
use buzz_ws_client::message::RelayMessage;
use nostr::{EventBuilder, JsonUtil};
use uuid::Uuid;

/// A kind 9 channel message with optional NIP-10 thread markers, `p`
/// mentions, and extra tags. The relay stores tags verbatim; only single-
/// letter tags are filterable, which is why completeness rests on the
/// retrieval-key `p` tag the caller passes in `mentions`.
pub fn build_fleet_message(
    channel: Uuid,
    content: &str,
    mentions: &[String],
    root: Option<&str>,
    parent: Option<&str>,
    extra_tags: &[(String, String)],
) -> anyhow::Result<EventBuilder> {
    for m in mentions {
        nostr::PublicKey::from_hex(m).map_err(|e| anyhow::anyhow!("invalid mention {m}: {e}"))?;
    }
    let thread_ref = match (root, parent) {
        (Some(r), p) => {
            let root_id = nostr::EventId::from_hex(r).map_err(|e| anyhow::anyhow!("invalid root: {e}"))?;
            let parent_id = match p {
                Some(p) => nostr::EventId::from_hex(p).map_err(|e| anyhow::anyhow!("invalid parent: {e}"))?,
                None => root_id,
            };
            Some(buzz_sdk::ThreadRef { root_event_id: root_id, parent_event_id: parent_id })
        }
        (None, Some(_)) => anyhow::bail!("--parent requires --root"),
        (None, None) => None,
    };
    let mention_refs: Vec<&str> = mentions.iter().map(String::as_str).collect();
    let mut builder = buzz_sdk::builders::build_message(
        channel, content, thread_ref.as_ref(), &mention_refs, false, &[], &[],
    ).map_err(|e| anyhow::anyhow!(e))?;
    for (name, value) in extra_tags {
        let tag = nostr::Tag::parse([name.as_str(), value.as_str()])
            .map_err(|e| anyhow::anyhow!("invalid tag {name}: {e}"))?;
        builder = builder.tag(tag);
    }
    Ok(builder)
}

/// Parses a relay filter from JSON, refusing multi-letter `#xx` tag keys.
///
/// The relay pushes only a fixed set of filter keys down into SQL —
/// `kinds`, `authors`, `ids`, `since`, `until`, `limit`, single-letter tag
/// filters (`#p`, `#h`, `#e`, ...), and `#d` on NIP-33 kinds. Everything
/// else, including a multi-letter key like `#fleet`, is silently dropped
/// by `nostr::Filter`'s own parsing rather than rejected — so a caller who
/// filters on one would get a filter that quietly matches far more than
/// they asked for, with no symptom. Refuse it here instead.
pub fn parse_filter(json: &str) -> anyhow::Result<nostr::Filter> {
    let raw: serde_json::Value = serde_json::from_str(json)?;
    if let Some(obj) = raw.as_object() {
        for key in obj.keys() {
            if key.starts_with('#') && key.chars().count() != 2 {
                anyhow::bail!("filter key {key:?} is not a single-letter tag filter; nostr would silently ignore it");
            }
        }
    }
    nostr::Filter::from_json(json).map_err(|e| anyhow::anyhow!("invalid filter: {e}"))
}

/// One REQ on an authenticated connection; collect until EOSE, then CLOSE.
pub async fn collect_events(conn: &mut NostrWsConnection, filter: nostr::Filter) -> anyhow::Result<Vec<nostr::Event>> {
    let sub_id = format!("fleet-{}", Uuid::new_v4().simple());
    conn.send_raw(&serde_json::json!(["REQ", sub_id, filter])).await?;
    let mut out = Vec::new();
    loop {
        match conn.next_event(Duration::from_secs(30)).await? {
            RelayMessage::Event { event, .. } => out.push(*event),
            RelayMessage::Eose { .. } => break,
            RelayMessage::Closed { message, .. } => anyhow::bail!("relay closed subscription: {message}"),
            _ => {}
        }
    }
    let _ = conn.send_raw(&serde_json::json!(["CLOSE", sub_id])).await;
    Ok(out)
}

pub async fn run_query(relay: &str, nsec: &str, auth_tag: Option<&nostr::Tag>, filter: nostr::Filter) -> anyhow::Result<Vec<nostr::Event>> {
    let keys = nostr::Keys::parse(nsec)?;
    let mut conn = NostrWsConnection::connect_authenticated(relay, &keys, auth_tag).await?;
    let events = collect_events(&mut conn, filter).await;
    let _ = conn.disconnect().await;
    events
}

#[cfg(test)]
mod tests {
    use super::*;
    use nostr::{Keys, Kind};

    #[test]
    fn parse_filter_accepts_single_letter_tag_filters() {
        let f = parse_filter(r##"{"kinds":[9],"#p":["ab"],"#h":["6f1c0000-0000-4000-8000-000000000000"],"until":5,"limit":1000}"##).unwrap();
        assert!(f.kinds.as_ref().unwrap().contains(&Kind::Custom(9)));
        let p = nostr::SingleLetterTag::lowercase(nostr::Alphabet::P);
        assert!(f.generic_tags.get(&p).unwrap().contains("ab"));
        assert_eq!(f.until.unwrap().as_secs(), 5);
        assert_eq!(f.limit, Some(1000));
    }

    #[test]
    fn parse_filter_rejects_multi_letter_tag_keys() {
        let err = parse_filter(r##"{"kinds":[9],"#fleet":["x"]}"##).unwrap_err();
        assert!(err.to_string().contains("single-letter"));
    }

    fn tags_of(event: &nostr::Event) -> Vec<Vec<String>> {
        event.tags.iter().map(|t| t.as_slice().to_vec()).collect()
    }

    fn has(tags: &[Vec<String>], want: &[&str]) -> bool {
        tags.iter().any(|t| t.iter().map(String::as_str).eq(want.iter().copied()))
    }

    #[test]
    fn fleet_message_carries_channel_mentions_thread_and_extra_tags() {
        let channel = Uuid::new_v4();
        let to = "b".repeat(64);
        let root = "c".repeat(64);
        let event = build_fleet_message(
            channel, "@Reviewer ▶ task a1b2c3d4", &[to.clone()], Some(&root), Some(&root),
            &[("t".into(), "fleet".into()), ("fleet".into(), "{\"type\":\"delegate\"}".into())],
        ).unwrap().sign_with_keys(&Keys::generate()).unwrap();

        assert_eq!(event.kind, Kind::Custom(9));
        let tags = tags_of(&event);
        assert!(has(&tags, &["h", &channel.to_string()]));
        assert!(has(&tags, &["p", &to]));
        assert!(has(&tags, &["e", &root, "", "reply"]));
        assert!(has(&tags, &["t", "fleet"]));
        assert!(has(&tags, &["fleet", "{\"type\":\"delegate\"}"]));
    }

    #[test]
    fn fleet_message_nested_reply_emits_root_and_reply_markers() {
        let root = "c".repeat(64);
        let parent = "d".repeat(64);
        let event = build_fleet_message(Uuid::new_v4(), "x", &[], Some(&root), Some(&parent), &[])
            .unwrap().sign_with_keys(&Keys::generate()).unwrap();
        let tags = tags_of(&event);
        assert!(has(&tags, &["e", &root, "", "root"]));
        assert!(has(&tags, &["e", &parent, "", "reply"]));
    }

    #[test]
    fn fleet_message_rejects_bad_mention_and_parent_without_root() {
        assert!(build_fleet_message(Uuid::new_v4(), "x", &["nope".into()], None, None, &[]).is_err());
        assert!(build_fleet_message(Uuid::new_v4(), "x", &[], None, Some(&"d".repeat(64)), &[]).is_err());
    }
}
