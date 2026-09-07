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
///
/// Every branch is guarded on `subscription_id == sub_id`: `buzz-relay`'s
/// `handle_close` (`crates/buzz-relay/src/handlers/close.rs`) sends a
/// `CLOSED` acknowledgement for every client `CLOSE`, including the CLOSE
/// this function itself sends at the end of a call. That ack is not
/// consumed here (this function returns as soon as it sees EOSE), so it
/// sits in the socket's read buffer until whichever `collect_events` call
/// reuses this connection next — a channel-level subcommand that issues a
/// second REQ on the same connection (`run_channel_members`,
/// `run_read_channel_meta`'s callers) would otherwise read that leftover
/// `["CLOSED", <old-sub-id>, ""]` as its own subscription being closed and
/// bail with a spurious "relay closed subscription: " error. Ignoring
/// messages tagged with a subscription id other than this call's own (the
/// existing `_ => {}` arm) fixes that without changing behaviour for any
/// single-REQ-per-connection caller.
pub async fn collect_events(conn: &mut NostrWsConnection, filter: nostr::Filter) -> anyhow::Result<Vec<nostr::Event>> {
    let sub_id = format!("fleet-{}", Uuid::new_v4().simple());
    conn.send_raw(&serde_json::json!(["REQ", sub_id, filter])).await?;
    let mut out = Vec::new();
    loop {
        match conn.next_event(Duration::from_secs(30)).await? {
            RelayMessage::Event { subscription_id, event } if subscription_id == sub_id => out.push(*event),
            RelayMessage::Eose { subscription_id } if subscription_id == sub_id => break,
            RelayMessage::Closed { subscription_id, message } if subscription_id == sub_id => {
                anyhow::bail!("relay closed subscription: {message}")
            }
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

fn tag_value<'a>(event: &'a nostr::Event, name: &str) -> Option<&'a str> {
    event.tags.iter().find_map(|t| {
        let s = t.as_slice();
        if s.first().map(String::as_str) == Some(name) { s.get(1).map(String::as_str) } else { None }
    })
}

fn tag_values<'a>(event: &'a nostr::Event, name: &str) -> Vec<&'a str> {
    event.tags.iter().filter_map(|t| {
        let s = t.as_slice();
        if s.first().map(String::as_str) == Some(name) { s.get(1).map(String::as_str) } else { None }
    }).collect()
}

/// Joins a channel's NIP-29 membership event(s) (kind 39002) with kind:0
/// profile events to resolve each member's display name. A member with no
/// matching profile (or a profile with neither `display_name` nor `name`)
/// maps to `None` rather than being dropped — `channel-members` still
/// needs the pubkey.
pub fn members_from_events(members: &[nostr::Event], profiles: &[nostr::Event]) -> Vec<(String, Option<String>)> {
    let mut names = std::collections::HashMap::new();
    for p in profiles {
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(&p.content) {
            let name = v.get("display_name").and_then(|x| x.as_str())
                .or_else(|| v.get("name").and_then(|x| x.as_str())).map(str::to_string);
            names.insert(p.pubkey.to_hex(), name);
        }
    }
    let mut seen = std::collections::BTreeSet::new();
    let mut out = Vec::new();
    for m in members {
        for pk in tag_values(m, "p") {
            if seen.insert(pk.to_string()) {
                out.push((pk.to_string(), names.get(pk).cloned().flatten()));
            }
        }
    }
    out
}

/// One channel's kind:39000 metadata, as read off the relay.
#[derive(Debug, Clone, serde::Serialize)]
pub struct ChannelMeta {
    pub channel_id: String,
    pub name: String,
    pub about: Option<String>,
    pub archived: bool,
}

/// Parses kind:39000 metadata events into `ChannelMeta`. Mirrors buzz-acp's
/// own archived check (`crates/buzz-acp/src/relay.rs`): a channel is
/// archived iff its `archived` tag's value is exactly `"true"`.
pub fn channel_meta(metadata: &[nostr::Event]) -> Vec<ChannelMeta> {
    metadata.iter().filter_map(|e| Some(ChannelMeta {
        channel_id: tag_value(e, "d")?.to_string(),
        name: tag_value(e, "name").unwrap_or("").to_string(),
        about: tag_value(e, "about").map(str::to_string),
        archived: tag_value(e, "archived") == Some("true"),
    })).collect()
}

pub async fn run_channel_members(relay: &str, nsec: &str, auth_tag: Option<&nostr::Tag>, channel: Uuid) -> anyhow::Result<Vec<(String, Option<String>)>> {
    let keys = nostr::Keys::parse(nsec)?;
    let mut conn = NostrWsConnection::connect_authenticated(relay, &keys, auth_tag).await?;
    let d = nostr::SingleLetterTag::lowercase(nostr::Alphabet::D);
    let members = collect_events(&mut conn, nostr::Filter::new().kind(nostr::Kind::Custom(39002)).custom_tags(d, [channel.to_string()])).await?;
    let pubkeys: Vec<nostr::PublicKey> = members.iter()
        .flat_map(|m| tag_values(m, "p").into_iter().filter_map(|p| nostr::PublicKey::from_hex(p).ok()).collect::<Vec<_>>())
        .collect();
    let profiles = if pubkeys.is_empty() { vec![] } else {
        collect_events(&mut conn, nostr::Filter::new().kind(nostr::Kind::Metadata).authors(pubkeys)).await?
    };
    let _ = conn.disconnect().await;
    Ok(members_from_events(&members, &profiles))
}

pub async fn run_read_channel_meta(relay: &str, nsec: &str, auth_tag: Option<&nostr::Tag>, channel: Option<Uuid>) -> anyhow::Result<Vec<ChannelMeta>> {
    let keys = nostr::Keys::parse(nsec)?;
    let mut conn = NostrWsConnection::connect_authenticated(relay, &keys, auth_tag).await?;
    let mut filter = nostr::Filter::new().kind(nostr::Kind::Custom(39000));
    if let Some(id) = channel {
        filter = filter.custom_tags(nostr::SingleLetterTag::lowercase(nostr::Alphabet::D), [id.to_string()]);
    }
    let metadata = collect_events(&mut conn, filter).await?;
    let _ = conn.disconnect().await;
    Ok(channel_meta(&metadata))
}

pub fn build_create_channel(name: &str, about: Option<&str>) -> anyhow::Result<(Uuid, EventBuilder)> {
    let id = Uuid::new_v4();
    let builder = buzz_sdk::builders::build_create_channel(id, name, None, None, about, None).map_err(|e| anyhow::anyhow!(e))?;
    Ok((id, builder))
}

pub fn build_write_about(channel: Uuid, about: &str) -> anyhow::Result<EventBuilder> {
    buzz_sdk::builders::build_update_channel(channel, None, Some(about), None, None).map_err(|e| anyhow::anyhow!(e))
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

    fn signed(builder: EventBuilder, keys: &Keys) -> nostr::Event { builder.sign_with_keys(keys).unwrap() }

    #[test]
    fn members_from_events_joins_membership_with_profiles() {
        let a = Keys::generate();
        let b = Keys::generate();
        let channel = Uuid::new_v4();
        let members = signed(EventBuilder::new(Kind::Custom(39002), "").tags([
            nostr::Tag::parse(["d", &channel.to_string()]).unwrap(),
            nostr::Tag::parse(["p", &a.public_key().to_hex()]).unwrap(),
            nostr::Tag::parse(["p", &b.public_key().to_hex()]).unwrap(),
        ]), &Keys::generate());
        let profile_a = signed(EventBuilder::new(Kind::Metadata, r#"{"display_name":"Reviewer"}"#), &a);
        let out = members_from_events(&[members], &[profile_a]);
        assert!(out.contains(&(a.public_key().to_hex(), Some("Reviewer".into()))));
        assert!(out.contains(&(b.public_key().to_hex(), None)));
    }

    #[test]
    fn channel_meta_reads_name_about_and_archived() {
        let keys = Keys::generate();
        let id1 = Uuid::new_v4();
        let id2 = Uuid::new_v4();
        let live = signed(EventBuilder::new(Kind::Custom(39000), "").tags([
            nostr::Tag::parse(["d", &id1.to_string()]).unwrap(),
            nostr::Tag::parse(["name", "fleet"]).unwrap(),
            nostr::Tag::parse(["about", "line one\n{\"buzz-fleet\":1}"]).unwrap(),
        ]), &keys);
        let archived = signed(EventBuilder::new(Kind::Custom(39000), "").tags([
            nostr::Tag::parse(["d", &id2.to_string()]).unwrap(),
            nostr::Tag::parse(["name", "old"]).unwrap(),
            nostr::Tag::parse(["archived", "true"]).unwrap(),
        ]), &keys);
        let metas = channel_meta(&[live, archived]);
        assert_eq!(metas.len(), 2);
        let m = metas.iter().find(|m| m.channel_id == id1.to_string()).unwrap();
        assert_eq!(m.name, "fleet");
        assert_eq!(m.about.as_deref(), Some("line one\n{\"buzz-fleet\":1}"));
        assert!(!m.archived);
        assert!(metas.iter().find(|m| m.channel_id == id2.to_string()).unwrap().archived);
    }
}
