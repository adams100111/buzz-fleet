//! Builders and relay helpers for the orchestration subcommands.

// Tasks 5, 6, and 18 add relay-reading subcommands to this file (channel
// meta and message history) that need these; Task 4 only writes, so they
// are unused for now. Suppressed here rather than dropped so this file's
// shared header doesn't have to be re-added by each of those tasks.
#![allow(unused_imports)]

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

#[cfg(test)]
mod tests {
    use super::*;
    use nostr::{Keys, Kind};

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
