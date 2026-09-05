//! Hand-rolled builders for the four event kinds with no reusable crate —
//! their real schemas live in Desktop's Tauri-only backend or binary-only
//! CLI crate. Each builder cites the exact upstream file/line its shape was
//! copied from, so a future schema drift is at least detectable by diffing
//! against a named source. See the design spec's "Architecture" section.

use nostr::{EventBuilder, Kind, Tag};
use serde::{Deserialize, Serialize};

/// Mirrors `desktop/src-tauri/src/events.rs:428-453`'s `build_profile` —
/// snake_case NIP-01 content. `buzz-fleet` only ever sets `display_name`;
/// `name`/`picture`/`about`/`nip05` have no equivalent concept here.
pub fn build_agent_profile(display_name: &str, auth_tag_json: &str) -> anyhow::Result<EventBuilder> {
    let parts: Vec<String> = serde_json::from_str(auth_tag_json)?;
    if parts.len() != 4 || parts[0] != "auth" {
        anyhow::bail!("invalid auth tag: expected a 4-element JSON array starting with \"auth\"");
    }
    let auth_tag = Tag::parse(parts)?;
    let content = serde_json::json!({"display_name": display_name}).to_string();
    Ok(EventBuilder::new(Kind::Custom(0), content).tags(vec![auth_tag]))
}

pub const KIND_MANAGED_AGENT: u16 = 30177;

/// Mirrors `ManagedAgentEventContent`
/// (`desktop/src-tauri/src/managed_agents/agent_events.rs:38-60`) exactly —
/// snake_case field names, `persona_id`/`provider`/`persona_source_version`
/// always absent for buzz-fleet's always-standalone agents, `parallelism`
/// and `respond_to` non-optional. This struct exists only to *validate* the
/// content JSON `manager.py` builds — buzz-fleet-signer never constructs
/// this content itself (see Task 9's `visibility.managed_agent_content`).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ManagedAgentContent {
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub persona_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub system_prompt: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub persona_source_version: Option<String>,
    pub parallelism: u32,
    pub respond_to: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub respond_to_allowlist: Vec<String>,
}

pub fn build_managed_agent(agent_pubkey_hex: &str, content_json: &str) -> anyhow::Result<EventBuilder> {
    let _: ManagedAgentContent = serde_json::from_str(content_json)
        .map_err(|e| anyhow::anyhow!("content does not match ManagedAgentContent: {e}"))?;
    let tags = vec![Tag::parse(["d", agent_pubkey_hex])?];
    Ok(EventBuilder::new(Kind::Custom(KIND_MANAGED_AGENT), content_json).tags(tags))
}

#[cfg(test)]
mod tests {
    use super::*;
    use nostr::Keys;

    fn sample_auth_tag() -> String {
        serde_json::json!(["auth", "a".repeat(64), "", "b".repeat(128)]).to_string()
    }

    #[test]
    fn build_agent_profile_sets_display_name_and_auth_tag() {
        let keys = Keys::generate();
        let event = build_agent_profile("Laravel Backend Developer", &sample_auth_tag())
            .unwrap()
            .sign_with_keys(&keys)
            .unwrap();
        assert_eq!(event.kind, Kind::Custom(0));
        assert_eq!(
            event.content,
            serde_json::json!({"display_name": "Laravel Backend Developer"}).to_string()
        );
        assert!(event.tags.iter().any(|t| t.as_slice()[0] == "auth"));
    }

    #[test]
    fn build_agent_profile_rejects_malformed_auth_tag() {
        assert!(build_agent_profile("X", "not json").is_err());
        assert!(build_agent_profile("X", &serde_json::json!(["wrong", "a", "b"]).to_string()).is_err());
    }

    #[test]
    fn build_managed_agent_sets_d_tag_and_kind() {
        let owner = Keys::generate();
        let agent_pubkey = "c".repeat(64);
        let content = serde_json::json!({
            "name": "Laravel Backend Developer",
            "parallelism": 1,
            "respond_to": "owner-only",
            "respond_to_allowlist": []
        })
        .to_string();
        let event = build_managed_agent(&agent_pubkey, &content)
            .unwrap()
            .sign_with_keys(&owner)
            .unwrap();
        assert_eq!(event.kind, Kind::Custom(30177));
        assert!(event.tags.iter().any(|t| {
            let v: Vec<&str> = t.as_slice().iter().map(String::as_str).collect();
            v == ["d", agent_pubkey.as_str()]
        }));
        assert_eq!(event.content, content);
    }

    #[test]
    fn build_managed_agent_rejects_content_missing_required_fields() {
        // "respond_to" and "parallelism" are non-optional on the real wire
        // type (ManagedAgentEventContent) — a content file missing them
        // must fail loudly here, not publish a malformed event.
        let bad_content = serde_json::json!({"name": "X"}).to_string();
        assert!(build_managed_agent(&"c".repeat(64), &bad_content).is_err());
    }
}
