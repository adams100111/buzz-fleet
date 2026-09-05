//! Hand-rolled builders for the four event kinds with no reusable crate —
//! their real schemas live in Desktop's Tauri-only backend or binary-only
//! CLI crate. Each builder cites the exact upstream file/line its shape was
//! copied from, so a future schema drift is at least detectable by diffing
//! against a named source. See the design spec's "Architecture" section.

use nostr::{Event, EventBuilder, Kind, Tag};
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
}
