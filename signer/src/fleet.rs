//! Builders and relay helpers for the orchestration subcommands.

use std::time::Duration;

use buzz_ws_client::connection::NostrWsConnection;
use buzz_ws_client::message::RelayMessage;
use nostr::{EventBuilder, JsonUtil};
// Brings `sha256::Hash::hash(&[u8])` into scope for `run_query_http`'s NIP-98
// payload digest -- `nostr::hashes` is the `bitcoin_hashes` crate the `nostr`
// crate itself depends on and re-exports (see its `nip98` module).
use nostr::hashes::Hash as _;
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

/// One kind:30177 managed-agent record: `pubkey` is the value of the
/// event's own `d` tag (the agent's pubkey, per
/// `agent_events::build_managed_agent` -- not the pubkey that *signed* the
/// event, which is always the owner), `content` is the event's JSON body
/// parsed per `visibility.py`'s field set.
#[derive(Debug, Clone, serde::Serialize)]
pub struct ManagedAgentRecord {
    pub pubkey: String,
    pub content: serde_json::Value,
}

/// Parses kind:30177 events into `(pubkey, content)` pairs. An event with no
/// `d` tag, or whose content is not valid JSON, is skipped rather than
/// failing the whole read -- one malformed record must not take down `fleet
/// agents` for every other agent.
pub fn managed_agents_from_events(events: &[nostr::Event]) -> Vec<ManagedAgentRecord> {
    events.iter().filter_map(|e| {
        let pubkey = tag_value(e, "d")?.to_string();
        let content: serde_json::Value = serde_json::from_str(&e.content).ok()?;
        // A record whose content is valid JSON but not an *object* (a bare
        // array, string, number...) would make `directory()`'s `rec.get("role")`
        // (Python side) raise on every member, not just the malformed one --
        // the same one-bad-event-breaks-every-reader failure mode ruled on in
        // Task 12. `managed_agent_content()` always writes a dict, so this is
        // unreachable from buzz-fleet's own publish path, but a foreign or
        // corrupted record must still be skipped, not trusted.
        if !content.is_object() {
            return None;
        }
        Some(ManagedAgentRecord { pubkey, content })
    }).collect()
}

pub async fn run_read_managed_agents(
    relay: &str, nsec: &str, auth_tag: Option<&nostr::Tag>, owner: nostr::PublicKey,
) -> anyhow::Result<Vec<ManagedAgentRecord>> {
    let keys = nostr::Keys::parse(nsec)?;
    let mut conn = NostrWsConnection::connect_authenticated(relay, &keys, auth_tag).await?;
    let events = collect_events(&mut conn, nostr::Filter::new().kind(nostr::Kind::Custom(30177)).authors([owner])).await?;
    let _ = conn.disconnect().await;
    Ok(managed_agents_from_events(&events))
}

/// One kind:40902 presence entry, matching `read-presence`'s documented
/// `{"pubkey","status","updated_at"}` interface: `pubkey`/`status` come from
/// the matching event's `pubkey`/`content`, `updated_at` from its
/// `created_at`.
///
/// IMPORTANT -- confirmed against the upstream relay (`buzz` @ 7a9a523):
/// this interface, and the `.authors(pubkeys)` filter `run_read_presence`
/// builds below, are written exactly as specified, but neither kind:40902
/// nor kind:20001 (`KIND_PRESENCE_SNAPSHOT`/`KIND_PRESENCE_UPDATE` in
/// `crates/buzz-core/src/kind.rs`) is reachable through the plain NIP-01
/// `REQ`/`EOSE` websocket protocol `collect_events` speaks:
/// - kind:40902 is documented as a "relay-only sidecar kind (never
///   client-submitted)" and is never persisted; the only place it is ever
///   produced is `synthesize_presence` in the relay's HTTP bridge
///   (`crates/buzz-relay/src/api/bridge.rs`), which answers a
///   `kind:40902`/`kind:20001` + `authors` filter with a Redis lookup,
///   returning synthesized **kind:20001** events **signed by the relay's
///   own keypair** (not the subject), with the subject in a `p` tag and
///   `content` a bare status string -- never a stored, subject-signed
///   kind:40902 event an `authors` filter could ever match.
/// - kind:20001 is itself in the ephemeral range (20000-29999) and is
///   explicitly "never stored" (`handlers/event.rs`'s
///   `handle_ephemeral_event`).
/// - The HTTP bridge is a *separate* transport (`reqwest` + a NIP-98
///   signed request, per upstream `buzz-cli`'s `BuzzClient`) from the
///   websocket connection every other subcommand in this file uses.
///
/// This is why `run_read_presence` below goes over `run_query_http` (the
/// HTTP bridge) rather than `collect_events` (the plain websocket REQ every
/// other read in this file uses) -- see `run_query_http`'s own doc comment.
#[derive(Debug, Clone, serde::Serialize)]
pub struct PresenceEntry {
    pub pubkey: String,
    pub status: String,
    pub updated_at: u64,
}

/// The subject of a presence event is its `p` tag, not its signer:
/// `synthesize_presence` signs with the *relay's* keypair and carries the
/// subject in a `p` tag (see the doc comment above). Both real upstream
/// consumers of this shape -- `buzz-cli`'s `presence_subject()`
/// (`crates/buzz-cli/src/commands/users.rs`) and desktop's `get_presence`
/// (`desktop/src-tauri/src/commands/profile.rs`) -- read the `p` tag first
/// and fall back to `event.pubkey` only when there is none (a self-signed
/// event, which is what this parser's own unit fixtures use, but which
/// never occurs on a real relay for this kind). Falling back to
/// `event.pubkey` unconditionally would collapse every subject onto the
/// relay's own key once the transport gap above is closed -- silently
/// wrong, not just unreachable today.
pub fn presence_from_events(events: &[nostr::Event]) -> Vec<PresenceEntry> {
    events.iter().map(|e| PresenceEntry {
        pubkey: tag_value(e, "p").map(str::to_string).unwrap_or_else(|| e.pubkey.to_hex()),
        status: e.content.clone(),
        updated_at: e.created_at.as_secs(),
    }).collect()
}

/// Rewrites a `ws://`/`wss://` relay URL to the `http://`/`https://` base
/// the relay's HTTP bridge (`/query`, `/count`, `/events`, ...) is served
/// from -- the inverse of upstream `buzz-cli`'s `to_ws_url`
/// (`crates/buzz-cli/src/client.rs`), which goes the other way for its own
/// ephemeral-event websocket fallback. Every `buzz-fleet-signer` caller
/// hands this function the same `--relay` value already used for the
/// websocket subcommands in this file, so `read-presence` needs no new CLI
/// argument.
fn ws_to_http_url(relay: &str) -> anyhow::Result<String> {
    let http = if let Some(rest) = relay.strip_prefix("wss://") {
        format!("https://{rest}")
    } else if let Some(rest) = relay.strip_prefix("ws://") {
        format!("http://{rest}")
    } else {
        anyhow::bail!("relay URL {relay:?} must start with ws:// or wss://");
    };
    Ok(http.trim_end_matches('/').to_string())
}

/// One-shot `POST {relay}/query` over the relay's HTTP bridge, NIP-98-authed.
///
/// This is the transport `presence_from_events`'s doc comment (above)
/// establishes is required: kind:40902/20001 presence is synthesized only
/// inside `synthesize_presence` in the relay's HTTP `/query` handler
/// (`crates/buzz-relay/src/api/bridge.rs`), never reachable over the plain
/// NIP-01 websocket REQ `collect_events` speaks. Mirrors upstream
/// `buzz-cli`'s `BuzzClient::query`/`sign_nip98`
/// (`crates/buzz-cli/src/client.rs`) but built on the `nostr` crate's own
/// `nip98` feature (`HttpData::to_authorization`) rather than hand-rolling
/// event construction and base64 encoding a second time.
///
/// `filter` is wrapped in a single-element array, matching the bridge's
/// `Vec<Filter>` request body (a caller ORing multiple filters together
/// would need `query_multi`'s shape instead; no current subcommand needs
/// that, so this function only exposes the one-filter case). The response
/// body is a bare JSON array of Nostr events -- deserialized directly via
/// `nostr::Event`'s own `Deserialize` impl, the same shape `collect_events`
/// produces from the websocket path, so callers of either can share
/// downstream parsing (`presence_from_events`, etc.).
async fn run_query_http(
    relay: &str, nsec: &str, auth_tag: Option<&nostr::Tag>, filter: nostr::Filter,
) -> anyhow::Result<Vec<nostr::Event>> {
    let keys = nostr::Keys::parse(nsec)?;
    let base = ws_to_http_url(relay)?;
    let url = format!("{base}/query");
    let body = serde_json::to_vec(&[filter])?;

    let http_url = nostr::Url::parse(&url).map_err(|e| anyhow::anyhow!("invalid relay URL {url:?}: {e}"))?;
    let mut auth_data = nostr::nips::nip98::HttpData::new(http_url, nostr::nips::nip98::HttpMethod::POST);
    auth_data = auth_data.payload(nostr::hashes::sha256::Hash::hash(&body));
    let authorization = auth_data
        .to_authorization(&keys)
        .await
        .map_err(|e| anyhow::anyhow!("NIP-98 signing failed: {e}"))?;

    let client = reqwest::Client::new();
    let mut req = client
        .post(&url)
        .header("Authorization", authorization)
        .header("Content-Type", "application/json")
        .body(body);
    if let Some(tag) = auth_tag {
        let tag_json = serde_json::to_string(tag)?;
        req = req.header("x-auth-tag", tag_json);
    }

    let resp = req.send().await.map_err(|e| anyhow::anyhow!("query request failed: {e}"))?;
    let status = resp.status();
    let text = resp.text().await.map_err(|e| anyhow::anyhow!("failed to read query response: {e}"))?;
    if !status.is_success() {
        let message = serde_json::from_str::<serde_json::Value>(&text)
            .ok()
            .and_then(|v| v.get("error").or_else(|| v.get("message")).and_then(|m| m.as_str()).map(str::to_string))
            .unwrap_or(text);
        anyhow::bail!("relay query failed ({status}): {message}");
    }
    serde_json::from_str(&text).map_err(|e| anyhow::anyhow!("failed to parse query response: {e}"))
}

pub async fn run_read_presence(
    relay: &str, nsec: &str, auth_tag: Option<&nostr::Tag>, pubkeys: Vec<nostr::PublicKey>,
) -> anyhow::Result<Vec<PresenceEntry>> {
    // Matches `run_channel_members`'s own guard: an empty `authors` filter
    // legitimately matches nothing, but there is no reason to round-trip a
    // request just to learn that.
    if pubkeys.is_empty() {
        return Ok(vec![]);
    }
    let filter = nostr::Filter::new().kind(nostr::Kind::Custom(40902)).authors(pubkeys);
    let events = run_query_http(relay, nsec, auth_tag, filter).await?;
    Ok(presence_from_events(&events))
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

    #[test]
    fn managed_agents_from_events_reads_d_tag_and_parses_content() {
        let owner = Keys::generate();
        let agent_pubkey = "c".repeat(64);
        let content = r#"{"name":"Reviewer","role":"reviewer","harness":"claude","version":"0.8.0"}"#;
        let event = signed(EventBuilder::new(Kind::Custom(30177), content).tags([
            nostr::Tag::parse(["d", &agent_pubkey]).unwrap(),
        ]), &owner);
        let records = managed_agents_from_events(&[event]);
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].pubkey, agent_pubkey);
        assert_eq!(records[0].content["role"], "reviewer");
        assert_eq!(records[0].content["harness"], "claude");
        assert_eq!(records[0].content["version"], "0.8.0");
    }

    #[test]
    fn managed_agents_from_events_skips_missing_d_tag_and_malformed_content() {
        let owner = Keys::generate();
        let no_d_tag = signed(EventBuilder::new(Kind::Custom(30177), r#"{"name":"x"}"#), &owner);
        let bad_json = signed(EventBuilder::new(Kind::Custom(30177), "not json").tags([
            nostr::Tag::parse(["d", &"d".repeat(64)]).unwrap(),
        ]), &owner);
        assert!(managed_agents_from_events(&[no_d_tag, bad_json]).is_empty());
    }

    #[test]
    fn managed_agents_from_events_skips_non_object_content() {
        // Valid JSON, but not an object -- a bare array must not reach
        // `ManagedAgentRecord.content`, where the Python side's `rec.get("role")`
        // would raise on every member, not just this malformed one.
        let owner = Keys::generate();
        let bare_array = signed(EventBuilder::new(Kind::Custom(30177), "[1,2,3]").tags([
            nostr::Tag::parse(["d", &"d".repeat(64)]).unwrap(),
        ]), &owner);
        let bare_string = signed(EventBuilder::new(Kind::Custom(30177), "\"hello\"").tags([
            nostr::Tag::parse(["d", &"e".repeat(64)]).unwrap(),
        ]), &owner);
        assert!(managed_agents_from_events(&[bare_array, bare_string]).is_empty());
    }

    #[test]
    fn presence_from_events_reads_subject_from_p_tag_over_signer() {
        // The real shape: `synthesize_presence` signs with the relay's own
        // keypair and carries the *subject* in a `p` tag -- the subject must
        // come from there, not from `event.pubkey` (which would be the relay).
        let relay_keys = Keys::generate();
        let agent = Keys::generate();
        let event = EventBuilder::new(Kind::Custom(40902), "online")
            .tag(nostr::Tag::parse(["p", &agent.public_key().to_hex()]).unwrap())
            .custom_created_at(nostr::Timestamp::from(1700))
            .sign_with_keys(&relay_keys)
            .unwrap();
        let entries = presence_from_events(&[event]);
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].pubkey, agent.public_key().to_hex());
        assert_ne!(entries[0].pubkey, relay_keys.public_key().to_hex());
        assert_eq!(entries[0].status, "online");
        assert_eq!(entries[0].updated_at, 1700);
    }

    #[test]
    fn presence_from_events_falls_back_to_signer_pubkey_when_no_p_tag() {
        // Fallback case for a self-signed event with no `p` tag -- not the
        // real relay-synthesized shape, but a defensible degrade rather than
        // an empty/panicking read.
        let agent = Keys::generate();
        let event = EventBuilder::new(Kind::Custom(40902), "online")
            .custom_created_at(nostr::Timestamp::from(1700))
            .sign_with_keys(&agent)
            .unwrap();
        let entries = presence_from_events(&[event]);
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].pubkey, agent.public_key().to_hex());
        assert_eq!(entries[0].status, "online");
        assert_eq!(entries[0].updated_at, 1700);
    }
}

#[cfg(test)]
mod http_query_tests {
    //! `run_query_http` is the HTTP-bridge transport `read-presence` needs
    //! (see the doc comment on it below): a one-shot NIP-98-authed
    //! `POST {relay}/query`, mirroring upstream `buzz-cli`'s
    //! `BuzzClient::query`/`sign_nip98` (`crates/buzz-cli/src/client.rs`).
    //! These tests run a real (hand-rolled, single-connection) HTTP/1.1
    //! server on localhost and drive `run_query_http` against it end to
    //! end, so they bind the actual production seam rather than a
    //! test-only stand-in.
    use super::*;
    use nostr::Keys;
    use std::collections::HashMap;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    struct CapturedRequest {
        method: String,
        path: String,
        headers: HashMap<String, String>,
        body: Vec<u8>,
    }

    /// `main.rs` installs the process-wide rustls `ring` `CryptoProvider`
    /// exactly once, before the Tokio runtime starts -- `reqwest`'s
    /// `rustls-no-provider` feature requires one to be installed before any
    /// `Client` is built, but never installs one itself (see the Cargo.toml
    /// comment on the `reqwest` dependency). The test binary never runs
    /// `main()`, so each test that builds a `reqwest::Client` (via
    /// `run_query_http`) must install it itself; `Once` keeps repeat calls
    /// across tests in the same process from erroring on an already-set
    /// provider.
    fn ensure_crypto_provider() {
        static INIT: std::sync::Once = std::sync::Once::new();
        INIT.call_once(|| {
            let _ = rustls::crypto::ring::default_provider().install_default();
        });
    }

    fn find_double_crlf(buf: &[u8]) -> Option<usize> {
        buf.windows(4).position(|w| w == b"\r\n\r\n")
    }

    /// Binds an ephemeral localhost port, accepts exactly one HTTP/1.1
    /// request, replies with `response_body` (200, application/json), and
    /// hands back what it received. `response_body` must outlive the
    /// spawned task, so callers pass a `'static` string (test fixtures are
    /// small and short-lived; `Box::leak` is fine here).
    async fn one_shot_server(
        response_body: &'static str,
    ) -> (String, tokio::task::JoinHandle<CapturedRequest>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let handle = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut buf = Vec::new();
            let mut tmp = [0u8; 4096];
            let header_end = loop {
                let n = socket.read(&mut tmp).await.unwrap();
                buf.extend_from_slice(&tmp[..n]);
                if let Some(pos) = find_double_crlf(&buf) {
                    break pos;
                }
            };
            let header_text = String::from_utf8_lossy(&buf[..header_end]).to_string();
            let mut lines = header_text.split("\r\n");
            let request_line = lines.next().unwrap_or_default();
            let mut parts = request_line.split_whitespace();
            let method = parts.next().unwrap_or_default().to_string();
            let path = parts.next().unwrap_or_default().to_string();
            let mut headers = HashMap::new();
            for line in lines {
                if let Some((k, v)) = line.split_once(": ") {
                    headers.insert(k.to_ascii_lowercase(), v.to_string());
                }
            }
            let content_length: usize = headers
                .get("content-length")
                .and_then(|v| v.parse().ok())
                .unwrap_or(0);
            let body_start = header_end + 4;
            while buf.len() < body_start + content_length {
                let n = socket.read(&mut tmp).await.unwrap();
                buf.extend_from_slice(&tmp[..n]);
            }
            let body = buf[body_start..body_start + content_length].to_vec();

            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                response_body.len(),
                response_body
            );
            let _ = socket.write_all(response.as_bytes()).await;
            let _ = socket.shutdown().await;

            CapturedRequest { method, path, headers, body }
        });
        (format!("127.0.0.1:{}", addr.port()), handle)
    }

    #[tokio::test]
    async fn run_query_http_posts_nip98_authed_query_and_parses_events() {
        ensure_crypto_provider();
        let signer_keys = Keys::generate();
        let relay_keys = Keys::generate();
        let subject = Keys::generate();
        let event = EventBuilder::new(nostr::Kind::Custom(40902), "online")
            .tag(nostr::Tag::parse(["p", &subject.public_key().to_hex()]).unwrap())
            .custom_created_at(nostr::Timestamp::from(1700))
            .sign_with_keys(&relay_keys)
            .unwrap();
        let response_body: &'static str =
            Box::leak(serde_json::to_string(&[event.clone()]).unwrap().into_boxed_str());

        let (addr, handle) = one_shot_server(response_body).await;
        let relay_ws_url = format!("ws://{addr}");
        let filter = nostr::Filter::new().kind(nostr::Kind::Custom(40902)).authors([subject.public_key()]);

        let events = run_query_http(&relay_ws_url, &signer_keys.secret_key().to_secret_hex(), None, filter)
            .await
            .unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].id, event.id);
        assert_eq!(events[0].pubkey, relay_keys.public_key());

        let req = handle.await.unwrap();
        assert_eq!(req.method, "POST");
        assert_eq!(req.path, "/query");
        let auth = req.headers.get("authorization").expect("Authorization header present");
        assert!(auth.starts_with("Nostr "), "auth header should carry a NIP-98 event: {auth}");
        assert!(!req.headers.contains_key("x-auth-tag"), "no auth tag was passed");
        let sent: serde_json::Value = serde_json::from_slice(&req.body).unwrap();
        assert!(sent.is_array(), "filter body must be wrapped in an array, matching buzz-cli's /query contract");
    }

    #[tokio::test]
    async fn run_query_http_sends_x_auth_tag_header_when_auth_tag_present() {
        ensure_crypto_provider();
        let signer_keys = Keys::generate();
        let response_body: &'static str = "[]";
        let (addr, handle) = one_shot_server(response_body).await;
        let relay_ws_url = format!("ws://{addr}");
        let auth_tag = nostr::Tag::parse(["auth", &"a".repeat(64), "", &"b".repeat(128)]).unwrap();
        let filter = nostr::Filter::new().kind(nostr::Kind::Custom(40902));

        let events = run_query_http(&relay_ws_url, &signer_keys.secret_key().to_secret_hex(), Some(&auth_tag), filter)
            .await
            .unwrap();
        assert!(events.is_empty());

        let req = handle.await.unwrap();
        let header = req.headers.get("x-auth-tag").expect("x-auth-tag header present");
        assert_eq!(header, &serde_json::to_string(&auth_tag).unwrap());
    }

    #[tokio::test]
    async fn run_read_presence_uses_the_http_bridge_and_reads_the_p_tag_subject() {
        ensure_crypto_provider();
        // End-to-end through the public `read-presence` entry point: this is
        // the real regression this task exists to fix -- kind:40902 was
        // never reachable at all over the plain websocket REQ path
        // `presence_from_events`'s doc comment above documents live evidence
        // for. `run_read_presence` must now go over HTTP, not the socket.
        let relay_keys = Keys::generate();
        let subject = Keys::generate();
        let event = EventBuilder::new(nostr::Kind::Custom(40902), "away")
            .tag(nostr::Tag::parse(["p", &subject.public_key().to_hex()]).unwrap())
            .custom_created_at(nostr::Timestamp::from(4200))
            .sign_with_keys(&relay_keys)
            .unwrap();
        let response_body: &'static str =
            Box::leak(serde_json::to_string(&[event]).unwrap().into_boxed_str());
        let (addr, _handle) = one_shot_server(response_body).await;
        let relay_ws_url = format!("ws://{addr}");
        let signer_keys = Keys::generate();

        let presence = run_read_presence(
            &relay_ws_url,
            &signer_keys.secret_key().to_secret_hex(),
            None,
            vec![subject.public_key()],
        )
        .await
        .unwrap();

        assert_eq!(presence.len(), 1);
        assert_eq!(presence[0].pubkey, subject.public_key().to_hex());
        assert_eq!(presence[0].status, "away");
        assert_eq!(presence[0].updated_at, 4200);
    }
}
