use nostr::{Event, EventBuilder, Kind, Tag};

pub const RELAY_ADD_MEMBER: u16 = 9030;
pub const RELAY_REMOVE_MEMBER: u16 = 9031;

fn check_pubkey_hex(target_pubkey_hex: &str) -> anyhow::Result<()> {
    if target_pubkey_hex.len() != 64 || !target_pubkey_hex.chars().all(|c| c.is_ascii_hexdigit()) {
        anyhow::bail!("invalid pubkey: expected 64 hex characters, got {target_pubkey_hex:?}");
    }
    Ok(())
}

pub fn build_add_member(target_pubkey_hex: &str, role: Option<&str>) -> anyhow::Result<EventBuilder> {
    check_pubkey_hex(target_pubkey_hex)?;
    let mut tags = vec![Tag::parse(["p", target_pubkey_hex])?];
    if let Some(role) = role {
        tags.push(Tag::parse(["role", role])?);
    }
    Ok(EventBuilder::new(Kind::Custom(RELAY_ADD_MEMBER), "").tags(tags))
}

pub fn build_remove_member(target_pubkey_hex: &str) -> anyhow::Result<EventBuilder> {
    check_pubkey_hex(target_pubkey_hex)?;
    let tags = vec![Tag::parse(["p", target_pubkey_hex])?];
    Ok(EventBuilder::new(Kind::Custom(RELAY_REMOVE_MEMBER), "").tags(tags))
}

#[cfg(test)]
mod tests {
    use super::*;
    use nostr::Keys;

    fn sign(builder: EventBuilder) -> Event {
        let keys = Keys::generate();
        builder.sign_with_keys(&keys).expect("sign")
    }

    #[test]
    fn add_member_sets_kind_and_p_tag() {
        let event = sign(build_add_member("a".repeat(64).as_str(), None).unwrap());
        assert_eq!(event.kind, Kind::Custom(RELAY_ADD_MEMBER));
        assert!(event.tags.iter().any(|t| {
            let v: Vec<&str> = t.as_slice().iter().map(String::as_str).collect();
            v == ["p", "a".repeat(64).as_str()]
        }));
    }

    #[test]
    fn add_member_with_role_sets_role_tag() {
        let event = sign(build_add_member("b".repeat(64).as_str(), Some("admin")).unwrap());
        assert!(event.tags.iter().any(|t| {
            let v: Vec<&str> = t.as_slice().iter().map(String::as_str).collect();
            v == ["role", "admin"]
        }));
    }

    #[test]
    fn remove_member_sets_kind_and_p_tag() {
        let event = sign(build_remove_member("c".repeat(64).as_str()).unwrap());
        assert_eq!(event.kind, Kind::Custom(RELAY_REMOVE_MEMBER));
        assert!(event.tags.iter().any(|t| {
            let v: Vec<&str> = t.as_slice().iter().map(String::as_str).collect();
            v == ["p", "c".repeat(64).as_str()]
        }));
    }

    #[test]
    fn add_member_rejects_short_pubkey() {
        assert!(build_add_member("deadbeef", None).is_err());
    }

    #[test]
    fn add_member_rejects_non_hex_pubkey() {
        assert!(build_add_member(&"z".repeat(64), None).is_err());
    }

    #[test]
    fn remove_member_rejects_malformed_pubkey() {
        assert!(build_remove_member("not-hex").is_err());
    }
}
