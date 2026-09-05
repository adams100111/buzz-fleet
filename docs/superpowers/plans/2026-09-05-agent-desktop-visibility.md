# Agent visibility in Buzz Desktop/mobile — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `buzz-fleet`-created agents show up, correctly owner-attributed, in Buzz Desktop's/mobile's Agents view — today they only publish a relay-membership event that lands them as a plain community member, invisible to the entirely separate Nostr pipeline (kind:0/9000/10100/30177) Desktop's Agents view actually reads.

**Architecture:** Reuse `buzz-sdk` (new git dependency in `signer/Cargo.toml`, same repo+rev as the existing `buzz-ws-client` dependency) for kind:9000/9001 (NIP-29 channel join/leave), NIP-OA auth-tag computation, and kind:5 coordinate deletion — all already have validated, dependency-light builders there. Hand-roll the three kinds with no externally reusable crate (kind:0 profile, kind:30177 managed-agent record, kind:10100 add-policy, kind:9035 archive request) in a new `signer/src/agent_events.rs` module, each mirroring an exact upstream schema with a doc-comment citation. `manager.py` orchestrates all of it through a single reusable per-agent step function (`AgentManager._sync_visibility`) called identically by `create_agent`'s first attempt, `ensure_runtime_ready`'s retries, and `update_agent`'s field-triggered republishes — so there is exactly one place that knows how to publish/retry each event, not three.

**Tech Stack:** Rust (`nostr` 0.44, `buzz-sdk` + `buzz-ws-client` from `block/buzz`, `clap`) for the signer; Python 3.12 (Typer, Textual, Pydantic) for the manager/CLI/TUI — same stack as the rest of `buzz-fleet`, no new dependencies on the Python side.

**Spec:** `docs/superpowers/specs/2026-09-05-agent-desktop-visibility-design.md` (read it first — this plan implements it task-by-task; the spec has the full rationale for every decision below, reached via `grilling` against the real `/home/dev/apps/buzz` source).

## Global Constraints

- `visibility_managed` defaults to `False` and is set `True` **only** by `create_agent`, **never** retroactively by `update_agent` or `ensure_runtime_ready` — this is the entire mechanism preventing backfill of pre-existing agents (`laravel-backend-developer`, `my-cdx`). Get this exactly right; it is the single most important invariant in this plan.
- kind:0, kind:30177, and kind:10100 are **always** published for every newly-created agent — no opt-out. kind:9000 (channel join) is optional, gated purely on `channel_ids` being non-empty.
- kind:30177's content mirrors `ManagedAgentEventContent` (`desktop/src-tauri/src/managed_agents/agent_events.rs:38-60`) **exactly** — snake_case field names, `persona_id`/`provider`/`persona_source_version` always `None`, `parallelism` defaulting to `1`, `respond_to` derived as `"allowlist"` if `respond_to_allowlist` is set else `"owner-only"`. This is a genuinely different shape from `.agent.json`'s camelCase `AgentSnapshotDefinition` — do not conflate them.
- `team_instructions`, when set, is concatenated *before* the resolved system prompt (blank line separator) in the published `system_prompt` field — it has no dedicated wire slot.
- kind:10100 content is exactly `{"channel_add_policy": "<value>"}`, nothing else. Default `"owner_only"` when `Agent.channel_add_policy` is `None`.
- Every new relay-facing call is one connect→auth→publish→disconnect round trip via the existing `run_publish` helper — no batching subcommand.
- Failure classification: an error message containing `"invalid:"` (the relay's NIP-01 rejection prefix) is **permanent** (never retried again, recorded into `visibility_state`); anything else (connection/timeout/unparseable-output) is **transient** (retried on the next `ensure_runtime_ready` pass).
- `delete_agent`'s visibility teardown mirrors Desktop's real, complete delete flow exactly: `leave-channel` (kind:9001) for every joined channel, then `retract-managed-agent` (kind:5), then `archive-agent` (kind:9035, `reason="retired"`) — confirmed by reading both `useManagedAgentActions.ts`'s `handleDelete` (channel-leaving) and the backend `delete_managed_agent`/`tombstone_managed_agent_pending` (kind:5 + kind:9035), not assumed from either half alone.
- `archive-agent` (kind:9035) is signed by the **owner's** key with the agent's NIP-OA auth tag embedded as a tag on the event — confirmed from `desktop/src-tauri/src/commands/agents_pending.rs:188-224`'s `build_agent_archive_request(keys: &Keys, agent_pubkey: &str, persona_id: Option<&str>)`, which signs with `keys` (the caller's, i.e. the owner's, in Desktop's real usage) and only omits the auth tag when the signer *is* the target — never the case here. This corrects an approximate sketch in the design spec, which described it as "agent-signed"; mirroring Desktop's actual code (the spec's own standing principle) means owner-signed.

---

## File Structure

```
buzz-fleet/
├── signer/
│   ├── Cargo.toml                   # +buzz-sdk git dependency
│   └── src/
│       ├── main.rs                  # +8 new Command variants
│       ├── events.rs                # unchanged
│       └── agent_events.rs          # NEW — kind:0/30177/10100/9035 hand-rolled builders
├── src/buzz_fleet/
│   ├── models.py                    # +AgentVisibilityState, +4 Agent fields
│   ├── signer_client.py             # +8 thin wrapper functions
│   ├── visibility.py                # NEW — content-mapping, error classification, status text
│   ├── manager.py                   # +_sync_visibility, wired into create/update/delete/ensure_runtime_ready
│   ├── cli/app.py                   # +2 flags on agent create/update, +status column on agent list
│   └── tui/screens/
│       ├── agent_form.py            # +2 Access-section fields, +UUID validation
│       └── dashboard.py             # +Visibility column
├── tests/
│   ├── test_manager.py              # extended FakeRunner + new lifecycle tests
│   ├── test_signer_client.py        # +8 wrapper tests
│   ├── test_visibility.py           # NEW
│   ├── test_cli.py                  # +flag tests
│   └── tui/
│       ├── test_agent_form.py       # +field tests
│       └── test_dashboard.py        # +column tests
```

`visibility.py` is a new, single-responsibility module specifically so the content-mapping/classification/status-text logic used by `manager.py`, `cli/app.py`, and `tui/screens/dashboard.py` lives in exactly one place instead of being duplicated three ways (CLI needs plain-text status, TUI needs the same text plus a color, `manager.py` needs the classification and content-building logic) — each of those three call sites imports from here rather than reimplementing.

---

## Task 1: `buzz-sdk` dependency + channel join/leave (kind:9000/9001)

**Files:**
- Modify: `signer/Cargo.toml`
- Modify: `signer/src/main.rs`
- Test: `signer/src/main.rs` (inline `#[cfg(test)]`, matching `events.rs`'s existing style — `main.rs` has no tests today, so this task adds the first ones there)

**Interfaces:**
- Produces: `buzz-fleet-signer join-channel --relay <url> --agent-nsec <nsec> --channel-id <uuid>` and the `leave-channel` equivalent, each printing `{"ok": true}` or `{"ok": false, "error": "..."}`.

- [ ] **Step 1: Add the dependency**

In `signer/Cargo.toml`, add alongside the existing `buzz-ws-client` line:

```toml
buzz-sdk = { git = "https://github.com/block/buzz", rev = "b1f6b7ef770dddbb7f33c9f5861c379a47bca1d6" }
uuid = "1"
```

(same rev already pinned for `buzz-ws-client` — keeps both dependencies from the same upstream commit. `uuid` needs no feature flags here — this crate only *parses* channel-id strings via `Uuid::parse_str`, never generates one, so the default `"v4"`-less feature set is enough; also, plain `"1"` lets Cargo's resolver unify with whatever compatible 1.x version `buzz-sdk`'s own workspace already pins, rather than risking two incompatible `Uuid` types across the crate boundary from an over-constrained feature request.)

- [ ] **Step 2: Build to confirm the dependency resolves**

Run: `cd signer && cargo build`
Expected: builds cleanly (no code uses `buzz_sdk` yet, so this only proves the git dependency fetches and compiles).

- [ ] **Step 3: Add the two `Command` variants and their handlers to `main.rs`**

Leave `main.rs`'s existing `use nostr::Keys;` import line unchanged — `Kind` is needed only by this task's own test (Step 4) and Task 5's (both reference `Kind::Custom(...)`), never by production handler code, so it's imported inside the test module in Step 4 instead of at the top of the file. A top-level `use nostr::Kind;` would trigger an `unused import` warning on every plain (non-test) `cargo build`, forever — not something to introduce into a file that otherwise builds warning-free.

Add to the `Command` enum (after the existing `RemoveMember` variant):

```rust
    /// Self-join one NIP-29 channel as role=bot.
    JoinChannel {
        #[arg(long)]
        relay: String,
        #[arg(long)]
        agent_nsec: String,
        #[arg(long)]
        channel_id: String,
    },
    /// Self-leave one NIP-29 channel.
    LeaveChannel {
        #[arg(long)]
        relay: String,
        #[arg(long)]
        agent_nsec: String,
        #[arg(long)]
        channel_id: String,
    },
```

Add to the `match cli.command` block (after the existing `RemoveMember` arm):

```rust
        Command::JoinChannel { relay, agent_nsec, channel_id } => {
            let builder = uuid::Uuid::parse_str(&channel_id)
                .map_err(anyhow::Error::from)
                .and_then(|id| {
                    let agent_pubkey = Keys::parse(&agent_nsec)?.public_key().to_hex();
                    buzz_sdk::builders::build_add_member(id, &agent_pubkey, Some(buzz_sdk::MemberRole::Bot))
                        .map_err(|e| anyhow::anyhow!(e))
                });
            match run_publish(&relay, &agent_nsec, builder).await {
                Ok(()) => { println!("{}", json!({"ok": true})); 0 }
                Err(e) => { println!("{}", json!({"ok": false, "error": e.to_string()})); 1 }
            }
        }
        Command::LeaveChannel { relay, agent_nsec, channel_id } => {
            let builder = uuid::Uuid::parse_str(&channel_id)
                .map_err(anyhow::Error::from)
                .and_then(|id| {
                    let agent_pubkey = Keys::parse(&agent_nsec)?.public_key().to_hex();
                    buzz_sdk::builders::build_remove_member(id, &agent_pubkey)
                        .map_err(|e| anyhow::anyhow!(e))
                });
            match run_publish(&relay, &agent_nsec, builder).await {
                Ok(()) => { println!("{}", json!({"ok": true})); 0 }
                Err(e) => { println!("{}", json!({"ok": false, "error": e.to_string()})); 1 }
            }
        }
```

- [ ] **Step 4: Add unit tests confirming the CLI wires `channel_id`/self-pubkey/role correctly**

Add a `#[cfg(test)] mod tests` block at the bottom of `main.rs` (this file has none yet):

```rust
#[cfg(test)]
mod tests {
    use super::*;
    // Only referenced from test code below — kept scoped to this module so
    // a plain (non-test) `cargo build` never warns about an unused import.
    use nostr::Kind;

    #[test]
    fn join_channel_builds_self_add_with_bot_role() {
        let keys = Keys::generate();
        let channel_id = uuid::Uuid::new_v4();
        let builder = buzz_sdk::builders::build_add_member(
            channel_id,
            &keys.public_key().to_hex(),
            Some(buzz_sdk::MemberRole::Bot),
        )
        .unwrap();
        let event = builder.sign_with_keys(&keys).unwrap();
        assert_eq!(event.kind, Kind::Custom(9000));
        assert!(event.tags.iter().any(|t| {
            let v: Vec<&str> = t.as_slice().iter().map(String::as_str).collect();
            v == ["role", "bot"]
        }));
        assert!(event.tags.iter().any(|t| {
            let v: Vec<&str> = t.as_slice().iter().map(String::as_str).collect();
            v == ["h", channel_id.to_string().as_str()]
        }));
    }

    #[test]
    fn leave_channel_builds_self_remove() {
        let keys = Keys::generate();
        let channel_id = uuid::Uuid::new_v4();
        let builder = buzz_sdk::builders::build_remove_member(channel_id, &keys.public_key().to_hex()).unwrap();
        let event = builder.sign_with_keys(&keys).unwrap();
        assert_eq!(event.kind, Kind::Custom(9001));
        assert!(event.tags.iter().any(|t| {
            let v: Vec<&str> = t.as_slice().iter().map(String::as_str).collect();
            v == ["h", channel_id.to_string().as_str()]
        }));
    }

    #[test]
    fn leave_channel_rejects_malformed_channel_id() {
        assert!(uuid::Uuid::parse_str("not-a-uuid").is_err());
    }
}
```

This test exercises the exact `buzz_sdk` call this task's handlers make (not `buzz_sdk`'s own internals, which are already tested upstream) — confirming the specific combination of arguments (`Some(MemberRole::Bot)`, the channel UUID as the `h` tag) this integration relies on.

- [ ] **Step 5: Run the tests**

Run: `cd signer && cargo test`
Expected: both new tests pass; all existing `events.rs` tests still pass.

- [ ] **Step 6: Commit**

```bash
git add signer/Cargo.toml signer/src/main.rs
git commit -m "signer: add join-channel/leave-channel via buzz-sdk"
```

---

## Task 2: NIP-OA auth-tag computation

**Files:**
- Modify: `signer/src/main.rs`

**Interfaces:**
- Consumes: `buzz_sdk::nip_oa::compute_auth_tag(owner_keys: &Keys, agent_pubkey: &PublicKey, conditions: &str) -> Result<String, SdkError>` and `buzz_sdk::nip_oa::verify_auth_tag` (both from Task 1's dependency).
- Produces: `buzz-fleet-signer compute-auth-tag --owner-nsec <nsec> --agent-pubkey <hex>` → `{"ok": true, "auth_tag": "[\"auth\",...]"}` (the raw JSON-array string, consumed by Tasks 3 and 7).

- [ ] **Step 1: Add the `Command` variant**

```rust
    /// Compute (never publish) an owner-signed NIP-OA auth tag for an agent pubkey.
    ComputeAuthTag {
        #[arg(long)]
        owner_nsec: String,
        #[arg(long)]
        agent_pubkey: String,
    },
```

- [ ] **Step 2: Add the handler function and match arm**

```rust
fn run_compute_auth_tag(owner_nsec: &str, agent_pubkey: &str) -> anyhow::Result<String> {
    let owner_keys = Keys::parse(owner_nsec)?;
    let agent_pk = nostr::PublicKey::from_hex(agent_pubkey)?;
    // conditions is always "" — matches Desktop's own kind:0-embedding call
    // site (`relay.rs`'s call to compute_auth_tag(&owner, &agent, "")).
    Ok(buzz_sdk::nip_oa::compute_auth_tag(&owner_keys, &agent_pk, "")?)
}
```

Add to `match cli.command`:

```rust
        Command::ComputeAuthTag { owner_nsec, agent_pubkey } => {
            match run_compute_auth_tag(&owner_nsec, &agent_pubkey) {
                Ok(auth_tag) => { println!("{}", json!({"ok": true, "auth_tag": auth_tag})); 0 }
                Err(e) => { println!("{}", json!({"ok": false, "error": e.to_string()})); 1 }
            }
        }
```

- [ ] **Step 3: Write the failing test**

```rust
    #[test]
    fn compute_auth_tag_round_trips_through_verify() {
        let owner = Keys::generate();
        let agent = Keys::generate();
        let tag_json = run_compute_auth_tag(
            &owner.secret_key().to_bech32().unwrap(),
            &agent.public_key().to_hex(),
        )
        .unwrap();
        let parts: Vec<String> = serde_json::from_str(&tag_json).unwrap();
        let tag = nostr::Tag::parse(parts).unwrap();
        assert!(buzz_sdk::nip_oa::verify_auth_tag(&tag, &agent.public_key(), &owner.public_key()).is_ok());
    }
```

(`verify_auth_tag`'s exact signature should be confirmed against `crates/buzz-sdk/src/nip_oa.rs` at implementation time — it takes the tag, the agent's pubkey, and the claimed owner's pubkey, per the design spec's citation of `nip_oa.rs:268`.)

- [ ] **Step 4: Run to verify it fails, then passes**

Run: `cd signer && cargo test compute_auth_tag_round_trips_through_verify`
Expected: FAIL (`run_compute_auth_tag` doesn't exist yet) → implement Step 2 → PASS.

- [ ] **Step 5: Commit**

```bash
git add signer/src/main.rs
git commit -m "signer: add compute-auth-tag using buzz-sdk's NIP-OA builder"
```

---

## Task 3: kind:0 agent profile

**Files:**
- Create: `signer/src/agent_events.rs`
- Modify: `signer/src/main.rs` (add `mod agent_events;`, one `Command` variant)

**Interfaces:**
- Consumes: the `auth_tag` JSON-array string from Task 2's `compute-auth-tag`.
- Produces: `agent_events::build_agent_profile(display_name: &str, auth_tag_json: &str) -> anyhow::Result<EventBuilder>`; CLI `publish-agent-profile --relay <url> --agent-nsec <nsec> --display-name <name> --auth-tag <json>`.

- [ ] **Step 1: Create `signer/src/agent_events.rs` with a failing test**

```rust
//! Hand-rolled builders for the four event kinds with no reusable crate —
//! their real schemas live in Desktop's Tauri-only backend or binary-only
//! CLI crate. Each builder cites the exact upstream file/line its shape was
//! copied from, so a future schema drift is at least detectable by diffing
//! against a named source. See the design spec's "Architecture" section.

use nostr::{EventBuilder, Kind, Tag};

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
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd signer && cargo test build_agent_profile`
Expected: FAIL (module doesn't exist / isn't wired into `main.rs` yet).

- [ ] **Step 3: Wire the module into `main.rs` and add the `PublishAgentProfile` command**

Add near the top of `main.rs` (alongside `mod events;`):

```rust
mod agent_events;
```

Add to `Command`:

```rust
    /// Publish the agent's own kind:0 profile, embedding a pre-computed auth tag.
    PublishAgentProfile {
        #[arg(long)]
        relay: String,
        #[arg(long)]
        agent_nsec: String,
        #[arg(long)]
        display_name: String,
        #[arg(long)]
        auth_tag: String,
    },
```

Add to `match cli.command`:

```rust
        Command::PublishAgentProfile { relay, agent_nsec, display_name, auth_tag } => {
            let builder = agent_events::build_agent_profile(&display_name, &auth_tag);
            match run_publish(&relay, &agent_nsec, builder).await {
                Ok(()) => { println!("{}", json!({"ok": true})); 0 }
                Err(e) => { println!("{}", json!({"ok": false, "error": e.to_string()})); 1 }
            }
        }
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd signer && cargo test`
Expected: all tests pass, including the two new ones.

- [ ] **Step 5: Commit**

```bash
git add signer/src/agent_events.rs signer/src/main.rs
git commit -m "signer: add publish-agent-profile (kind:0 with NIP-OA auth tag)"
```

---

## Task 4: kind:30177 managed-agent record

**Files:**
- Modify: `signer/src/agent_events.rs`
- Modify: `signer/src/main.rs`

**Interfaces:**
- Consumes: a JSON content file built by `visibility.py` (Task 9) — read verbatim, not reconstructed field-by-field in Rust.
- Produces: `agent_events::ManagedAgentContent` (a struct used only to *validate* the incoming JSON shape before publishing, not to construct it), `agent_events::build_managed_agent(agent_pubkey_hex: &str, content_json: &str) -> anyhow::Result<EventBuilder>`; CLI `publish-managed-agent --relay <url> --owner-nsec <nsec> --agent-pubkey <hex> --content-file <path>`.

- [ ] **Step 1: Add the failing test to `agent_events.rs`**

This and the other `#[test] fn ...` snippets in this task append into the `#[cfg(test)] mod tests { use super::*; use nostr::Keys; ... }` block Task 3 already created at the bottom of `agent_events.rs` — do not create a second `mod tests` block in the same file (Rust rejects two modules with the same name in one scope).

```rust
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd signer && cargo test build_managed_agent`
Expected: FAIL (`build_managed_agent` doesn't exist yet).

- [ ] **Step 3: Implement in `agent_events.rs`**

`ManagedAgentContent` is the first thing in this file to derive `Serialize`/`Deserialize` — Task 3's `build_agent_profile` only ever used `serde_json::json!` (already available transitively), never plain `serde`'s derive macros. Add `serde` as a direct dependency in `signer/Cargo.toml` (it isn't one yet):

```toml
serde = { version = "1", features = ["derive"] }
```

Add the import to the top of `agent_events.rs`, alongside the existing `use nostr::{EventBuilder, Kind, Tag};`:

```rust
use serde::{Deserialize, Serialize};
```

```rust
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
```

- [ ] **Step 4: Add the `PublishManagedAgent` command to `main.rs`**

```rust
    /// Publish (owner-signed) the kind:30177 managed-agent record.
    PublishManagedAgent {
        #[arg(long)]
        relay: String,
        #[arg(long)]
        owner_nsec: String,
        #[arg(long)]
        agent_pubkey: String,
        #[arg(long)]
        content_file: std::path::PathBuf,
    },
```

```rust
        Command::PublishManagedAgent { relay, owner_nsec, agent_pubkey, content_file } => {
            let builder = std::fs::read_to_string(&content_file)
                .map_err(anyhow::Error::from)
                .and_then(|content| agent_events::build_managed_agent(&agent_pubkey, &content));
            match run_publish(&relay, &owner_nsec, builder).await {
                Ok(()) => { println!("{}", json!({"ok": true})); 0 }
                Err(e) => { println!("{}", json!({"ok": false, "error": e.to_string()})); 1 }
            }
        }
```

- [ ] **Step 5: Run all tests**

Run: `cd signer && cargo test`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add signer/src/agent_events.rs signer/src/main.rs
git commit -m "signer: add publish-managed-agent (kind:30177)"
```

---

## Task 5: retract kind:30177 (kind:5 via `buzz-sdk`)

**Files:**
- Modify: `signer/src/main.rs`

**Interfaces:**
- Consumes: `buzz_sdk::builders::build_delete_addressable(kind: u32, pubkey: &str, d: &str) -> Result<EventBuilder, SdkError>` (already in `buzz-sdk`, confirmed at `crates/buzz-sdk/src/builders.rs:2330-2348` — this is the exact generic builder Desktop's own `agent_events.rs::build_agent_delete` is a hand-rolled duplicate of, so reusing it here is strictly better than hand-rolling a third copy).
- Produces: CLI `retract-managed-agent --relay <url> --owner-nsec <nsec> --agent-pubkey <hex>`.

- [ ] **Step 1: Write the failing test in `main.rs`**

This appends into the `#[cfg(test)] mod tests { ... }` block Task 1 already created at the bottom of `main.rs` — do not create a second `mod tests` block in the same file (Rust rejects two modules with the same name in one scope).

```rust
    #[test]
    fn retract_managed_agent_builds_correct_coordinate() {
        let owner = Keys::generate();
        let agent_pubkey = "d".repeat(64);
        let event = buzz_sdk::builders::build_delete_addressable(30177, &owner.public_key().to_hex(), &agent_pubkey)
            .unwrap()
            .sign_with_keys(&owner)
            .unwrap();
        assert_eq!(event.kind, Kind::Custom(5));
        let expected_coord = format!("30177:{}:{}", owner.public_key().to_hex(), agent_pubkey);
        assert!(event.tags.iter().any(|t| {
            let v: Vec<&str> = t.as_slice().iter().map(String::as_str).collect();
            v == ["a", expected_coord.as_str()]
        }));
    }
```

This confirms the exact call this task's handler makes; `build_delete_addressable`'s own correctness is already covered by `buzz-sdk`'s own test suite.

- [ ] **Step 2: Run to verify it fails**

Run: `cd signer && cargo test retract_managed_agent`
Expected: PASS immediately if `buzz_sdk` is already a dependency (Task 1) — this task has no new production code to fail against, so this step doubles as confirming the test itself is well-formed. If it fails to compile, fix the import/path before continuing.

- [ ] **Step 3: Add the `RetractManagedAgent` command**

```rust
    /// Retract (NIP-09 kind:5) the kind:30177 managed-agent record.
    RetractManagedAgent {
        #[arg(long)]
        relay: String,
        #[arg(long)]
        owner_nsec: String,
        #[arg(long)]
        agent_pubkey: String,
    },
```

```rust
        Command::RetractManagedAgent { relay, owner_nsec, agent_pubkey } => {
            let builder = Keys::parse(&owner_nsec)
                .map_err(anyhow::Error::from)
                .and_then(|owner_keys| {
                    buzz_sdk::builders::build_delete_addressable(
                        30177,
                        &owner_keys.public_key().to_hex(),
                        &agent_pubkey,
                    )
                    .map_err(|e| anyhow::anyhow!(e))
                });
            match run_publish(&relay, &owner_nsec, builder).await {
                Ok(()) => { println!("{}", json!({"ok": true})); 0 }
                Err(e) => { println!("{}", json!({"ok": false, "error": e.to_string()})); 1 }
            }
        }
```

- [ ] **Step 4: Run all tests**

Run: `cd signer && cargo test`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add signer/src/main.rs
git commit -m "signer: add retract-managed-agent (kind:5 via buzz-sdk)"
```

---

## Task 6: kind:10100 add-policy

**Files:**
- Modify: `signer/src/agent_events.rs`
- Modify: `signer/src/main.rs`

**Interfaces:**
- Produces: `agent_events::build_agent_add_policy(policy: &str) -> anyhow::Result<EventBuilder>`; CLI `publish-agent-add-policy --relay <url> --agent-nsec <nsec> --policy <anyone|owner_only|nobody>`.

- [ ] **Step 1: Write the failing test in `agent_events.rs`**

This and the other `#[test] fn ...` snippets in this task append into the `#[cfg(test)] mod tests { use super::*; use nostr::Keys; ... }` block Task 3 already created at the bottom of `agent_events.rs` — do not create a second `mod tests` block in the same file (Rust rejects two modules with the same name in one scope).

```rust
    #[test]
    fn build_agent_add_policy_sets_content() {
        let keys = Keys::generate();
        let event = build_agent_add_policy("owner_only").unwrap().sign_with_keys(&keys).unwrap();
        assert_eq!(event.kind, Kind::Custom(10100));
        assert_eq!(event.content, r#"{"channel_add_policy":"owner_only"}"#);
    }

    #[test]
    fn build_agent_add_policy_rejects_unknown_value() {
        assert!(build_agent_add_policy("sometimes").is_err());
    }
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd signer && cargo test build_agent_add_policy`
Expected: FAIL (function doesn't exist).

- [ ] **Step 3: Implement**

```rust
pub const KIND_AGENT_PROFILE: u16 = 10100;

/// Mirrors the one field the relay actually reads from kind:10100
/// (`crates/buzz-relay/src/handlers/side_effects.rs:1246-1278`) — no legacy
/// directory fields (`status`/`capabilities`/`channels`); buzz-fleet has no
/// concept of any of those to publish.
pub fn build_agent_add_policy(policy: &str) -> anyhow::Result<EventBuilder> {
    if !matches!(policy, "anyone" | "owner_only" | "nobody") {
        anyhow::bail!("invalid channel_add_policy {policy:?} (must be anyone, owner_only, or nobody)");
    }
    let content = serde_json::json!({"channel_add_policy": policy}).to_string();
    Ok(EventBuilder::new(Kind::Custom(KIND_AGENT_PROFILE), content))
}
```

- [ ] **Step 4: Add the `PublishAgentAddPolicy` command**

```rust
    /// Publish (agent-signed) the kind:10100 add-policy record.
    PublishAgentAddPolicy {
        #[arg(long)]
        relay: String,
        #[arg(long)]
        agent_nsec: String,
        #[arg(long)]
        policy: String,
    },
```

```rust
        Command::PublishAgentAddPolicy { relay, agent_nsec, policy } => {
            let builder = agent_events::build_agent_add_policy(&policy);
            match run_publish(&relay, &agent_nsec, builder).await {
                Ok(()) => { println!("{}", json!({"ok": true})); 0 }
                Err(e) => { println!("{}", json!({"ok": false, "error": e.to_string()})); 1 }
            }
        }
```

- [ ] **Step 5: Run all tests, then commit**

Run: `cd signer && cargo test`

```bash
git add signer/src/agent_events.rs signer/src/main.rs
git commit -m "signer: add publish-agent-add-policy (kind:10100)"
```

---

## Task 7: kind:9035 archive request (delete-time only)

**Files:**
- Modify: `signer/src/agent_events.rs`
- Modify: `signer/src/main.rs`

**Interfaces:**
- Consumes: Task 2's `compute-auth-tag` output (called by `manager.py` before this, with the *owner's* nsec and the agent's pubkey — same as Task 3, reused).
- Produces: `agent_events::build_archive_agent(agent_pubkey_hex: &str, reason: &str, auth_tag_json: &str) -> anyhow::Result<EventBuilder>`; CLI `archive-agent --relay <url> --owner-nsec <nsec> --agent-pubkey <hex> --reason <text> --auth-tag <json>`.

- [ ] **Step 1: Write the failing test in `agent_events.rs`**

This and the other `#[test] fn ...` snippets in this task append into the `#[cfg(test)] mod tests { use super::*; use nostr::Keys; ... }` block Task 3 already created at the bottom of `agent_events.rs` — do not create a second `mod tests` block in the same file (Rust rejects two modules with the same name in one scope).

```rust
    #[test]
    fn build_archive_agent_sets_expected_tags() {
        let owner = Keys::generate();
        let agent_pubkey = "e".repeat(64);
        let event = build_archive_agent(&agent_pubkey, "retired", &sample_auth_tag())
            .unwrap()
            .sign_with_keys(&owner)
            .unwrap();
        assert_eq!(event.kind, Kind::Custom(9035));
        assert_eq!(event.content, "");
        let tag_values: Vec<Vec<&str>> = event
            .tags
            .iter()
            .map(|t| t.as_slice().iter().map(String::as_str).collect())
            .collect();
        assert!(tag_values.contains(&vec!["-"]));
        assert!(tag_values.contains(&vec!["p", agent_pubkey.as_str()]));
        assert!(tag_values.contains(&vec!["reason", "retired"]));
        assert!(tag_values.iter().any(|v| v[0] == "auth"));
    }
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd signer && cargo test build_archive_agent`
Expected: FAIL (function doesn't exist).

- [ ] **Step 3: Implement**

```rust
pub const KIND_IA_ARCHIVE_REQUEST: u16 = 9035;

/// Mirrors `desktop/src-tauri/src/events.rs`'s `identity_archive_tags` +
/// `build_archive_identity_request` exactly: a NIP-70 protected marker
/// (`["-"]`), the target `p` tag, a `reason` tag, and the owner's NIP-OA
/// `auth` tag. Always owner-signed here — confirmed from
/// `agents_pending.rs:188-224`'s real call site, which signs with the
/// caller's (owner's) keys and only omits the auth tag when signer == target,
/// never the case for buzz-fleet's delete flow.
pub fn build_archive_agent(agent_pubkey_hex: &str, reason: &str, auth_tag_json: &str) -> anyhow::Result<EventBuilder> {
    let parts: Vec<String> = serde_json::from_str(auth_tag_json)?;
    if parts.len() != 4 || parts[0] != "auth" {
        anyhow::bail!("invalid auth tag: expected a 4-element JSON array starting with \"auth\"");
    }
    let tags = vec![
        Tag::parse(["-"])?,
        Tag::parse(["p", agent_pubkey_hex])?,
        Tag::parse(["reason", reason])?,
        Tag::parse(parts)?,
    ];
    Ok(EventBuilder::new(Kind::Custom(KIND_IA_ARCHIVE_REQUEST), "").tags(tags))
}
```

- [ ] **Step 4: Add the `ArchiveAgent` command**

```rust
    /// File a NIP-IA archive request (used only at delete time, owner-signed).
    ArchiveAgent {
        #[arg(long)]
        relay: String,
        #[arg(long)]
        owner_nsec: String,
        #[arg(long)]
        agent_pubkey: String,
        #[arg(long)]
        reason: String,
        #[arg(long)]
        auth_tag: String,
    },
```

```rust
        Command::ArchiveAgent { relay, owner_nsec, agent_pubkey, reason, auth_tag } => {
            let builder = agent_events::build_archive_agent(&agent_pubkey, &reason, &auth_tag);
            match run_publish(&relay, &owner_nsec, builder).await {
                Ok(()) => { println!("{}", json!({"ok": true})); 0 }
                Err(e) => { println!("{}", json!({"ok": false, "error": e.to_string()})); 1 }
            }
        }
```

- [ ] **Step 5: Run all tests, then commit**

Run: `cd signer && cargo test`

```bash
git add signer/src/agent_events.rs signer/src/main.rs
git commit -m "signer: add archive-agent (kind:9035, owner-signed)"
```

This completes the Rust side — all 8 new subcommands (`compute-auth-tag`, `publish-agent-profile`, `publish-managed-agent`, `retract-managed-agent`, `publish-agent-add-policy`, `join-channel`, `leave-channel`, `archive-agent`) exist and are unit-tested.

---

## Task 8: `Agent` model fields + `AgentVisibilityState`

**Files:**
- Modify: `src/buzz_fleet/models.py`
- Test: `tests/test_models.py` (new file — `models.py` has no dedicated test file today; round-tripping is otherwise only exercised indirectly via `test_manager.py`/`test_state.py`)

**Interfaces:**
- Produces: `Agent.channel_ids: list[str] | None`, `Agent.channel_add_policy: Literal["anyone", "owner_only", "nobody"] | None`, `Agent.visibility_managed: bool`, `Agent.visibility_state: AgentVisibilityState`; the `AgentVisibilityState` class itself, consumed by every later task.

- [ ] **Step 1: Write the failing test**

Create `tests/test_models.py`:

```python
from buzz_fleet.models import Agent, AgentVisibilityState, SystemPromptSource


def _agent(**overrides) -> Agent:
    from datetime import UTC, datetime

    defaults = dict(
        id="test-agent",
        community_id="eltahir",
        display_name="Test Agent",
        harness="claude",
        private_key="nsec1x",
        public_key="a" * 64,
        system_prompt_source=SystemPromptSource(kind="inline", text="hi"),
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return Agent(**defaults)


def test_new_agent_defaults_to_not_visibility_managed() -> None:
    agent = _agent()
    assert agent.visibility_managed is False
    assert agent.visibility_state == AgentVisibilityState()


def test_agent_loaded_from_json_without_visibility_fields_defaults_safely() -> None:
    """Regression test for the old-agent exemption: an Agent record saved
    before this feature existed has no `visibility_managed`/`visibility_state`
    keys in its JSON at all — it must load with the safe defaults, not raise.
    """
    agent = _agent()
    old_json = agent.model_dump_json(exclude={"visibility_managed", "visibility_state"})
    reloaded = Agent.model_validate_json(old_json)
    assert reloaded.visibility_managed is False
    assert reloaded.visibility_state.profile_published is False


def test_visibility_state_tracks_channel_outcomes_independently() -> None:
    state = AgentVisibilityState(channels={"c1": "joined", "c2": "error"}, channel_errors={"c2": "invalid: channel not found"})
    assert state.channels["c1"] == "joined"
    assert state.channel_errors["c2"] == "invalid: channel not found"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL with `ImportError: cannot import name 'AgentVisibilityState'`.

- [ ] **Step 3: Implement in `models.py`**

Add after the `SystemPromptSource` class and before `Agent`:

```python
class AgentVisibilityState(BaseModel):
    """Per-sub-publish status for the Desktop-visibility feature, tracked so
    `AgentManager._sync_visibility` retries only what's actually missing/
    failed, and so a permanently-broken input (e.g. a nonexistent channel
    UUID) is distinguished from one still genuinely pending. See the design
    spec's "Permanent vs. transient failures" section.
    """

    profile_published: bool = False
    managed_agent_published: bool = False
    add_policy_published: bool = False
    channels: dict[str, Literal["pending", "joined", "error"]] = Field(default_factory=dict)
    profile_error: str | None = None
    managed_agent_error: str | None = None
    add_policy_error: str | None = None
    channel_errors: dict[str, str] = Field(default_factory=dict)
```

Add to `Agent` (after `respond_to_allowlist`, before `created_at`):

```python
    channel_ids: list[str] | None = None
    channel_add_policy: Literal["anyone", "owner_only", "nobody"] | None = None
    visibility_managed: bool = False
    visibility_state: AgentVisibilityState = Field(default_factory=AgentVisibilityState)
```

Add `Field` to the existing `from pydantic import BaseModel, SecretStr` import line, making it `from pydantic import BaseModel, Field, SecretStr`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_models.py -v`
Expected: all 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/buzz_fleet/models.py tests/test_models.py
git commit -m "models: add AgentVisibilityState and agent visibility fields"
```

---

## Task 9: `signer_client.py` wrappers + `visibility.py`

**Files:**
- Modify: `src/buzz_fleet/signer_client.py`
- Create: `src/buzz_fleet/visibility.py`
- Modify: `tests/test_signer_client.py`
- Create: `tests/test_visibility.py`

**Interfaces:**
- Consumes: `Agent`, `AgentVisibilityState` (Task 8); `systemd.resolve_prompt_text` (existing).
- Produces: `signer_client.compute_auth_tag/publish_agent_profile/publish_managed_agent/retract_managed_agent/publish_agent_add_policy/join_channel/leave_channel/archive_agent`; `visibility.classify_signer_error(exc: Exception) -> Literal["transient", "permanent"]`, `visibility.resolved_channel_add_policy(agent: Agent) -> str`, `visibility.managed_agent_content(agent: Agent) -> dict`, `visibility.visibility_status_text(agent: Agent) -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_signer_client.py`:

```python
from pathlib import Path

from buzz_fleet.signer_client import (
    archive_agent,
    compute_auth_tag,
    join_channel,
    leave_channel,
    publish_agent_add_policy,
    publish_agent_profile,
    publish_managed_agent,
    retract_managed_agent,
)


def test_compute_auth_tag_returns_the_tag_string() -> None:
    runner = FakeRunner(json.dumps({"ok": True, "auth_tag": '["auth","a","",  "b"]'}))
    assert compute_auth_tag(runner, "nsec1owner", "c" * 64) == '["auth","a","",  "b"]'
    assert runner.calls == [
        ["buzz-fleet-signer", "compute-auth-tag", "--owner-nsec", "nsec1owner", "--agent-pubkey", "c" * 64]
    ]


def test_publish_agent_profile_passes_all_flags() -> None:
    runner = FakeRunner(json.dumps({"ok": True}))
    publish_agent_profile(runner, "wss://r", "nsec1agent", "Display Name", '["auth","a","","b"]')
    assert runner.calls == [
        [
            "buzz-fleet-signer",
            "publish-agent-profile",
            "--relay",
            "wss://r",
            "--agent-nsec",
            "nsec1agent",
            "--display-name",
            "Display Name",
            "--auth-tag",
            '["auth","a","","b"]',
        ]
    ]


def test_publish_managed_agent_passes_content_file_path() -> None:
    runner = FakeRunner(json.dumps({"ok": True}))
    publish_managed_agent(runner, "wss://r", "nsec1owner", "c" * 64, Path("/tmp/content.json"))
    assert runner.calls == [
        [
            "buzz-fleet-signer",
            "publish-managed-agent",
            "--relay",
            "wss://r",
            "--owner-nsec",
            "nsec1owner",
            "--agent-pubkey",
            "c" * 64,
            "--content-file",
            "/tmp/content.json",
        ]
    ]


def test_retract_managed_agent_raises_on_failure() -> None:
    runner = FakeRunner(json.dumps({"ok": False, "error": "invalid: not found"}))
    try:
        retract_managed_agent(runner, "wss://r", "nsec1owner", "c" * 64)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "invalid: not found" in str(e)


def test_publish_agent_add_policy_passes_policy() -> None:
    runner = FakeRunner(json.dumps({"ok": True}))
    publish_agent_add_policy(runner, "wss://r", "nsec1agent", "owner_only")
    assert "--policy" in runner.calls[0] and "owner_only" in runner.calls[0]


def test_join_channel_and_leave_channel_pass_channel_id() -> None:
    runner = FakeRunner(json.dumps({"ok": True}))
    join_channel(runner, "wss://r", "nsec1agent", "11111111-1111-1111-1111-111111111111")
    leave_channel(runner, "wss://r", "nsec1agent", "11111111-1111-1111-1111-111111111111")
    assert runner.calls[0][1] == "join-channel"
    assert runner.calls[1][1] == "leave-channel"


def test_archive_agent_passes_owner_nsec_and_reason() -> None:
    runner = FakeRunner(json.dumps({"ok": True}))
    archive_agent(runner, "wss://r", "nsec1owner", "c" * 64, "retired", '["auth","a","","b"]')
    call = runner.calls[0]
    assert "--owner-nsec" in call and "nsec1owner" in call
    assert "--reason" in call and "retired" in call
```

Create `tests/test_visibility.py`:

```python
from datetime import UTC, datetime

from buzz_fleet.models import Agent, AgentVisibilityState, SystemPromptSource
from buzz_fleet.visibility import (
    classify_signer_error,
    managed_agent_content,
    resolved_channel_add_policy,
    visibility_status_text,
)


def _agent(**overrides) -> Agent:
    defaults = dict(
        id="test-agent",
        community_id="eltahir",
        display_name="Test Agent",
        harness="claude",
        private_key="nsec1x",
        public_key="a" * 64,
        system_prompt_source=SystemPromptSource(kind="inline", text="You are a test agent."),
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return Agent(**defaults)


def test_classify_signer_error_permanent_on_invalid_prefix() -> None:
    assert classify_signer_error(RuntimeError("join-channel failed: invalid: channel not found")) == "permanent"


def test_classify_signer_error_transient_otherwise() -> None:
    assert classify_signer_error(RuntimeError("join-channel failed: connection refused")) == "transient"
    assert classify_signer_error(ValueError("Expecting value: line 1 column 1")) == "transient"


def test_resolved_channel_add_policy_defaults_to_owner_only() -> None:
    assert resolved_channel_add_policy(_agent()) == "owner_only"
    assert resolved_channel_add_policy(_agent(channel_add_policy="anyone")) == "anyone"


def test_managed_agent_content_maps_fields_and_derives_respond_to() -> None:
    agent = _agent(model="claude-sonnet-5", parallelism=3, respond_to_allowlist=["b" * 64])
    content = managed_agent_content(agent)
    assert content["name"] == "Test Agent"
    assert content["model"] == "claude-sonnet-5"
    assert content["parallelism"] == 3
    assert content["respond_to"] == "allowlist"
    assert content["respond_to_allowlist"] == ["b" * 64]
    assert content["persona_id"] is None
    assert content["provider"] is None


def test_managed_agent_content_defaults_parallelism_and_respond_to() -> None:
    content = managed_agent_content(_agent())
    assert content["parallelism"] == 1
    assert content["respond_to"] == "owner-only"


def test_managed_agent_content_prepends_team_instructions() -> None:
    agent = _agent(team_instructions="Test-first. Strict typing.")
    content = managed_agent_content(agent)
    assert content["system_prompt"] == "Test-first. Strict typing.\n\nYou are a test agent."


def test_visibility_status_text_old_agent_shows_dash() -> None:
    assert visibility_status_text(_agent()) == "—"


def test_visibility_status_text_synced_when_everything_done() -> None:
    agent = _agent(
        visibility_managed=True,
        visibility_state=AgentVisibilityState(
            profile_published=True, managed_agent_published=True, add_policy_published=True
        ),
    )
    assert visibility_status_text(agent) == "synced"


def test_visibility_status_text_pending_when_something_incomplete() -> None:
    agent = _agent(visibility_managed=True)
    assert visibility_status_text(agent) == "pending"


def test_visibility_status_text_surfaces_permanent_error() -> None:
    agent = _agent(
        visibility_managed=True,
        visibility_state=AgentVisibilityState(channel_errors={"c1": "invalid: channel not found"}),
    )
    assert visibility_status_text(agent) == "error: invalid: channel not found"
```

- [ ] **Step 2: Run to verify all new tests fail**

Run: `uv run pytest tests/test_signer_client.py tests/test_visibility.py -v`
Expected: FAIL (nothing implemented yet).

- [ ] **Step 3: Implement `signer_client.py` additions**

Append to `src/buzz_fleet/signer_client.py`:

```python
from pathlib import Path


def compute_auth_tag(runner: CommandRunner, owner_nsec: str, agent_pubkey: str) -> str:
    args = [BINARY, "compute-auth-tag", "--owner-nsec", owner_nsec, "--agent-pubkey", agent_pubkey]
    result = runner.run(args)
    payload = json.loads(result.stdout)
    if not payload["ok"]:
        raise RuntimeError(f"compute-auth-tag failed: {payload.get('error')}")
    auth_tag: str = payload["auth_tag"]
    return auth_tag


def publish_agent_profile(
    runner: CommandRunner, relay_url: str, agent_nsec: str, display_name: str, auth_tag: str
) -> None:
    args = [
        BINARY,
        "publish-agent-profile",
        "--relay",
        relay_url,
        "--agent-nsec",
        agent_nsec,
        "--display-name",
        display_name,
        "--auth-tag",
        auth_tag,
    ]
    result = runner.run(args)
    payload = json.loads(result.stdout)
    if not payload["ok"]:
        raise RuntimeError(f"publish-agent-profile failed: {payload.get('error')}")


def publish_managed_agent(
    runner: CommandRunner, relay_url: str, owner_nsec: str, agent_pubkey: str, content_file: Path
) -> None:
    args = [
        BINARY,
        "publish-managed-agent",
        "--relay",
        relay_url,
        "--owner-nsec",
        owner_nsec,
        "--agent-pubkey",
        agent_pubkey,
        "--content-file",
        str(content_file),
    ]
    result = runner.run(args)
    payload = json.loads(result.stdout)
    if not payload["ok"]:
        raise RuntimeError(f"publish-managed-agent failed: {payload.get('error')}")


def retract_managed_agent(runner: CommandRunner, relay_url: str, owner_nsec: str, agent_pubkey: str) -> None:
    args = [
        BINARY,
        "retract-managed-agent",
        "--relay",
        relay_url,
        "--owner-nsec",
        owner_nsec,
        "--agent-pubkey",
        agent_pubkey,
    ]
    result = runner.run(args)
    payload = json.loads(result.stdout)
    if not payload["ok"]:
        raise RuntimeError(f"retract-managed-agent failed: {payload.get('error')}")


def publish_agent_add_policy(runner: CommandRunner, relay_url: str, agent_nsec: str, policy: str) -> None:
    args = [BINARY, "publish-agent-add-policy", "--relay", relay_url, "--agent-nsec", agent_nsec, "--policy", policy]
    result = runner.run(args)
    payload = json.loads(result.stdout)
    if not payload["ok"]:
        raise RuntimeError(f"publish-agent-add-policy failed: {payload.get('error')}")


def join_channel(runner: CommandRunner, relay_url: str, agent_nsec: str, channel_id: str) -> None:
    args = [BINARY, "join-channel", "--relay", relay_url, "--agent-nsec", agent_nsec, "--channel-id", channel_id]
    result = runner.run(args)
    payload = json.loads(result.stdout)
    if not payload["ok"]:
        raise RuntimeError(f"join-channel failed: {payload.get('error')}")


def leave_channel(runner: CommandRunner, relay_url: str, agent_nsec: str, channel_id: str) -> None:
    args = [BINARY, "leave-channel", "--relay", relay_url, "--agent-nsec", agent_nsec, "--channel-id", channel_id]
    result = runner.run(args)
    payload = json.loads(result.stdout)
    if not payload["ok"]:
        raise RuntimeError(f"leave-channel failed: {payload.get('error')}")


def archive_agent(
    runner: CommandRunner, relay_url: str, owner_nsec: str, agent_pubkey: str, reason: str, auth_tag: str
) -> None:
    args = [
        BINARY,
        "archive-agent",
        "--relay",
        relay_url,
        "--owner-nsec",
        owner_nsec,
        "--agent-pubkey",
        agent_pubkey,
        "--reason",
        reason,
        "--auth-tag",
        auth_tag,
    ]
    result = runner.run(args)
    payload = json.loads(result.stdout)
    if not payload["ok"]:
        raise RuntimeError(f"archive-agent failed: {payload.get('error')}")
```

- [ ] **Step 4: Implement `visibility.py`**

Create `src/buzz_fleet/visibility.py`:

```python
"""Content-mapping, failure classification, and status text for the agent
Desktop-visibility feature — the one place `manager.py`, `cli/app.py`, and
`tui/screens/dashboard.py` all import from, instead of three duplicated
copies of the same logic. See the design spec for the field-mapping table
and status-column rules this implements.
"""

from __future__ import annotations

from typing import Literal

from buzz_fleet import systemd
from buzz_fleet.models import Agent


def classify_signer_error(exc: Exception) -> Literal["transient", "permanent"]:
    """A relay rejection with the "invalid: ..." NIP-01 prefix is permanent
    (the input itself is wrong and retrying changes nothing); anything else
    — a connection failure, a timeout, unparseable signer output — is
    transient and safe to retry on the next `ensure_runtime_ready` pass.
    """
    return "permanent" if "invalid:" in str(exc) else "transient"


def resolved_channel_add_policy(agent: Agent) -> str:
    return agent.channel_add_policy or "owner_only"


def managed_agent_content(agent: Agent) -> dict:
    """Build the exact JSON content for kind:30177, matching
    `ManagedAgentEventContent`'s snake_case field set. `team_instructions`
    has no dedicated wire slot — it's concatenated before the resolved
    system prompt instead of silently dropped.
    """
    prompt = systemd.resolve_prompt_text(agent)
    if agent.team_instructions:
        prompt = f"{agent.team_instructions}\n\n{prompt}"
    return {
        "name": agent.display_name,
        "persona_id": None,
        "system_prompt": prompt,
        "model": agent.model,
        "provider": None,
        "persona_source_version": None,
        "parallelism": agent.parallelism if agent.parallelism is not None else 1,
        "respond_to": "allowlist" if agent.respond_to_allowlist else "owner-only",
        "respond_to_allowlist": agent.respond_to_allowlist or [],
    }


def visibility_status_text(agent: Agent) -> str:
    """"—" for an old, unmanaged agent; "synced" once every mandatory step
    and channel join succeeded; "error: <reason>" for the first recorded
    permanent failure; "pending" otherwise.
    """
    if not agent.visibility_managed:
        return "—"
    state = agent.visibility_state
    errors = [e for e in (state.profile_error, state.managed_agent_error, state.add_policy_error) if e]
    errors.extend(state.channel_errors.values())
    if errors:
        return f"error: {errors[0]}"
    channels_joined = all(status == "joined" for status in state.channels.values())
    if state.profile_published and state.managed_agent_published and state.add_policy_published and channels_joined:
        return "synced"
    return "pending"
```

- [ ] **Step 5: Run to verify everything passes**

Run: `uv run pytest tests/test_signer_client.py tests/test_visibility.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/buzz_fleet/signer_client.py src/buzz_fleet/visibility.py tests/test_signer_client.py tests/test_visibility.py
git commit -m "manager: add signer_client wrappers and visibility helper module"
```

---

## Task 10: wire visibility publishing into `create_agent`

**Files:**
- Modify: `src/buzz_fleet/manager.py`
- Modify: `tests/test_manager.py` (extend `FakeRunner` to handle the 8 new subcommands)

**Interfaces:**
- Consumes: everything from Tasks 8-9.
- Produces: `AgentManager._sync_visibility(self, agent: Agent) -> Agent` — the single reusable per-agent step function every later task (11, 12, 13) also calls.

- [ ] **Step 1: Extend `FakeRunner` in `test_manager.py`**

Replace the `FakeRunner.run` method's body with (adding branches for the 8 new subcommands before the final `else`):

```python
    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:2] == ["buzz-fleet-signer", "generate-key"]:
            stdout = json.dumps({"public_key": "ab" * 32, "secret_key": "nsec1agent"})
        elif args[:2] == ["buzz-fleet-signer", "pubkey-from-nsec"]:
            stdout = json.dumps({"ok": True, "public_key": "c" * 64})
        elif args[:2] == ["buzz-fleet-signer", "compute-auth-tag"]:
            stdout = json.dumps({"ok": True, "auth_tag": json.dumps(["auth", "d" * 64, "", "e" * 128])})
        elif "add-member" in args or "remove-member" in args:
            stdout = json.dumps({"ok": True})
        elif args[:2] == ["buzz-fleet-signer", "publish-agent-profile"]:
            stdout = json.dumps({"ok": True})
        elif args[:2] == ["buzz-fleet-signer", "publish-managed-agent"]:
            stdout = json.dumps({"ok": True})
        elif args[:2] == ["buzz-fleet-signer", "retract-managed-agent"]:
            stdout = json.dumps({"ok": True})
        elif args[:2] == ["buzz-fleet-signer", "publish-agent-add-policy"]:
            stdout = json.dumps({"ok": True})
        elif args[:2] == ["buzz-fleet-signer", "join-channel"]:
            stdout = json.dumps({"ok": True})
        elif args[:2] == ["buzz-fleet-signer", "leave-channel"]:
            stdout = json.dumps({"ok": True})
        elif args[:2] == ["buzz-fleet-signer", "archive-agent"]:
            stdout = json.dumps({"ok": True})
        elif args[:2] == ["loginctl", "show-user"]:
            stdout = "yes"  # already lingering — the common case in these tests
        elif args[:2] == ["loginctl", "enable-linger"]:
            stdout = ""
        else:
            stdout = "active\n"
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
```

- [ ] **Step 2: Write the failing test**

Add to `tests/test_manager.py`:

```python
def test_create_agent_publishes_visibility_events_in_order(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())

    agent = manager.create_agent(
        display_name="Visible Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="You are visible."),
        channel_ids=["11111111-1111-1111-1111-111111111111"],
    )

    assert agent.visibility_managed is True
    assert agent.visibility_state.profile_published is True
    assert agent.visibility_state.managed_agent_published is True
    assert agent.visibility_state.add_policy_published is True
    assert agent.visibility_state.channels["11111111-1111-1111-1111-111111111111"] == "joined"
    subcommands = [c[1] for c in runner.calls if c[0] == "buzz-fleet-signer"]
    assert subcommands.index("compute-auth-tag") < subcommands.index("publish-agent-profile")
    assert "join-channel" in subcommands


def test_create_agent_records_permanent_channel_error_without_failing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")

    class BadChannelRunner(FakeRunner):
        def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:2] == ["buzz-fleet-signer", "join-channel"]:
                self.calls.append(args)
                return subprocess.CompletedProcess(
                    args, 0, stdout=json.dumps({"ok": False, "error": "invalid: channel not found"}), stderr=""
                )
            return super().run(args)

    runner = BadChannelRunner()
    manager = AgentManager(runner, _community())

    agent = manager.create_agent(
        display_name="Bad Channel Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
        channel_ids=["22222222-2222-2222-2222-222222222222"],
    )

    # create_agent must not raise despite the channel join failing.
    assert agent.visibility_state.channel_errors["22222222-2222-2222-2222-222222222222"] == (
        "join-channel failed: invalid: channel not found"
    )
    assert agent.visibility_state.channels["22222222-2222-2222-2222-222222222222"] == "error"
    assert agent.visibility_state.profile_published is True  # unrelated steps still succeeded
```

- [ ] **Step 3: Run to verify both fail**

Run: `uv run pytest tests/test_manager.py -k visibility -v`
Expected: FAIL (`create_agent` doesn't accept `channel_ids` yet, `_sync_visibility` doesn't exist).

- [ ] **Step 4: Implement in `manager.py`**

Add imports at the top:

```python
import json
import tempfile
from pathlib import Path

from buzz_fleet import buzz_acp, harnesses, signer_client, state, systemctl_client, systemd, visibility
```

(replacing the existing `from buzz_fleet import buzz_acp, harnesses, signer_client, state, systemctl_client, systemd` line — adds `json`, `tempfile`, `Path`, and `visibility`.)

Add `channel_ids` and `channel_add_policy` to `create_agent`'s signature (after `respond_to_allowlist`):

```python
        respond_to_allowlist: list[str] | None = None,
        channel_ids: list[str] | None = None,
        channel_add_policy: str | None = None,
        role: str | None = None,
```

Add both to the `Agent(...)` constructor call (after `respond_to_allowlist=respond_to_allowlist,`), plus `visibility_managed=True` (always, per the Global Constraints):

```python
            respond_to_allowlist=respond_to_allowlist,
            channel_ids=channel_ids,
            channel_add_policy=channel_add_policy,
            visibility_managed=True,
            created_at=datetime.now(UTC),
```

Add the new `_sync_visibility` method to `AgentManager` (place it right before `create_agent`):

```python
    def _sync_visibility(self, agent: Agent) -> Agent:
        """Publish whatever's missing from `agent.visibility_state` and
        return an updated copy — the single per-step function `create_agent`,
        `ensure_runtime_ready`, and `update_agent` all call, so there is
        exactly one place that knows how to publish/retry each event.

        Never raises: every step's failure is caught, classified via
        `visibility.classify_signer_error`, and recorded into the returned
        agent's `visibility_state` instead of propagating — a visibility
        publish must never block or roll back agent creation/update. A step
        already marked with a permanent error is never retried; a step with
        no error and not yet published is retried every call, which is the
        entire self-healing mechanism for a merely transient failure.
        """
        if not agent.visibility_managed:
            return agent

        relay_url = self._community.relay_url
        owner_nsec = self._community.relay_admin_nsec.get_secret_value()
        agent_nsec = agent.private_key.get_secret_value()
        vs = agent.visibility_state.model_copy(deep=True)

        if not vs.profile_published and vs.profile_error is None:
            try:
                auth_tag = signer_client.compute_auth_tag(self._runner, owner_nsec, agent.public_key)
                signer_client.publish_agent_profile(self._runner, relay_url, agent_nsec, agent.display_name, auth_tag)
                vs.profile_published = True
            except (RuntimeError, json.JSONDecodeError, KeyError) as e:
                if visibility.classify_signer_error(e) == "permanent":
                    vs.profile_error = str(e)

        if not vs.managed_agent_published and vs.managed_agent_error is None:
            try:
                content = visibility.managed_agent_content(agent)
                with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                    json.dump(content, f)
                    content_path = Path(f.name)
                try:
                    signer_client.publish_managed_agent(self._runner, relay_url, owner_nsec, agent.public_key, content_path)
                finally:
                    content_path.unlink(missing_ok=True)
                vs.managed_agent_published = True
            except (RuntimeError, json.JSONDecodeError, KeyError) as e:
                if visibility.classify_signer_error(e) == "permanent":
                    vs.managed_agent_error = str(e)

        if not vs.add_policy_published and vs.add_policy_error is None:
            try:
                policy = visibility.resolved_channel_add_policy(agent)
                signer_client.publish_agent_add_policy(self._runner, relay_url, agent_nsec, policy)
                vs.add_policy_published = True
            except (RuntimeError, json.JSONDecodeError, KeyError) as e:
                if visibility.classify_signer_error(e) == "permanent":
                    vs.add_policy_error = str(e)

        for channel_id in agent.channel_ids or []:
            if vs.channels.get(channel_id) == "joined" or channel_id in vs.channel_errors:
                continue
            try:
                signer_client.join_channel(self._runner, relay_url, agent_nsec, channel_id)
                vs.channels[channel_id] = "joined"
            except (RuntimeError, json.JSONDecodeError, KeyError) as e:
                if visibility.classify_signer_error(e) == "permanent":
                    vs.channel_errors[channel_id] = str(e)
                    vs.channels[channel_id] = "error"
                else:
                    vs.channels[channel_id] = "pending"

        return agent.model_copy(update={"visibility_state": vs})
```

Wire it into `create_agent`, right after the existing `state.save_agent(agent)` call and before `systemctl_client.enable_now(self._runner, agent.id)`:

```python
        state.save_agent(agent)
        agent = self._sync_visibility(agent)
        state.save_agent(agent)
        systemctl_client.enable_now(self._runner, agent.id)
        return agent
```

- [ ] **Step 5: Run to verify both tests pass**

Run: `uv run pytest tests/test_manager.py -k visibility -v`
Expected: both pass.

- [ ] **Step 6: Run the full existing test_manager.py suite to confirm no regressions**

Run: `uv run pytest tests/test_manager.py -v`
Expected: all pass, including every pre-existing test (they exercise `create_agent` without `channel_ids`, which is `None` by default — `_sync_visibility` still runs the three mandatory steps for them since `visibility_managed=True` is now always set).

- [ ] **Step 7: Commit**

```bash
git add src/buzz_fleet/manager.py tests/test_manager.py
git commit -m "manager: publish visibility events on agent creation"
```

---

## Task 11: fourth self-healing concern in `ensure_runtime_ready`

**Files:**
- Modify: `src/buzz_fleet/manager.py`
- Modify: `tests/test_manager.py`

**Interfaces:**
- Consumes: `AgentManager._sync_visibility` (Task 10).

- [ ] **Step 1: Write the failing tests**

```python
def test_ensure_runtime_ready_never_touches_agent_with_visibility_managed_false(tmp_path: Path, monkeypatch) -> None:
    """Regression test for the old-agent exemption — the single most
    important invariant in this feature. An agent created before this
    feature existed (visibility_managed=False) must never have any
    visibility signer subcommand invoked against it, ever.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Old Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    # Simulate a pre-feature record: flip visibility_managed back to False
    # and re-save, as if this agent had been loaded from disk before the
    # field existed (Pydantic's own default, never explicitly True).
    from buzz_fleet import state as state_module

    old_style = agent.model_copy(update={"visibility_managed": False})
    state_module.save_agent(old_style)
    runner.calls.clear()

    manager.ensure_runtime_ready()

    # archive-agent is delete-only (never called by ensure_runtime_ready/
    # _sync_visibility) and correctly excluded; retract-managed-agent is a
    # real subcommand a future unpublish path could call and must be
    # included so this test still catches that regression if it ever
    # happens.
    visibility_subcommands = {
        "compute-auth-tag",
        "publish-agent-profile",
        "publish-managed-agent",
        "retract-managed-agent",
        "publish-agent-add-policy",
        "join-channel",
        "leave-channel",
    }
    assert not any(len(c) > 1 and c[1] in visibility_subcommands for c in runner.calls)


def test_ensure_runtime_ready_retries_a_still_pending_visibility_step(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")

    class FlakyProfileRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.profile_calls = 0

        def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:2] == ["buzz-fleet-signer", "publish-agent-profile"]:
                self.profile_calls += 1
                self.calls.append(args)
                if self.profile_calls == 1:
                    return subprocess.CompletedProcess(args, 1, stdout="", stderr="connection refused")
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"ok": True}), stderr="")
            return super().run(args)

    runner = FlakyProfileRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Flaky Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    assert agent.visibility_state.profile_published is False  # first attempt failed transiently

    manager.ensure_runtime_ready()

    reloaded = next(a for a in manager.list_agents() if a.id == agent.id)
    assert reloaded.visibility_state.profile_published is True
```

- [ ] **Step 2: Run to verify both fail**

Run: `uv run pytest tests/test_manager.py -k "visibility_managed_false or retries_a_still_pending" -v`
Expected: FAIL (`ensure_runtime_ready` doesn't call `_sync_visibility` yet — the first test would trivially pass without the feature, but is written now so it fails once wired incorrectly; the second test fails because nothing retries yet).

- [ ] **Step 3: Implement**

In `ensure_runtime_ready`'s existing `for agent in self.list_agents():` loop, add the visibility sync as its own independent step (it doesn't depend on `resolved_command`/`needs_full_refresh`, so it runs unconditionally on every pass, same as the other three concerns):

```python
        for agent in self.list_agents():
            synced = self._sync_visibility(agent)
            if synced.visibility_state != agent.visibility_state:
                state.save_agent(synced)
            agent = synced

            resolved_command = harnesses.resolve_adapter_command(agent.harness)
            if not needs_full_refresh and _read_agent_command(agent.id) == resolved_command:
                continue
            existing_keys = _read_existing_env_keys(agent.id)
            systemd.write_agent_files(
                agent,
                self._community,
                anthropic_api_key=existing_keys.get("ANTHROPIC_API_KEY"),
                openai_api_key=existing_keys.get("OPENAI_API_KEY"),
            )
            try:
                systemctl_client.restart(self._runner, agent.id)
            except RuntimeError:
                pass
```

(Only the first four new lines are added; the rest of the loop body is unchanged — shown in full so the insertion point is unambiguous. Note there is deliberately no `if agent.visibility_managed:` guard here: `_sync_visibility` already checks that itself as its own first line — see Task 10 — and this is the single most safety-critical check in the whole feature, so it has exactly one source of truth rather than two copies of the same condition that could silently drift apart if either one is ever edited without the other.)

- [ ] **Step 4: Run to verify both pass**

Run: `uv run pytest tests/test_manager.py -k "visibility_managed_false or retries_a_still_pending" -v`
Expected: both pass.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest tests/test_manager.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/buzz_fleet/manager.py tests/test_manager.py
git commit -m "manager: retry visibility publishing in ensure_runtime_ready, exempt old agents"
```

---

## Task 12: `update_agent` reconciliation

**Files:**
- Modify: `src/buzz_fleet/manager.py`
- Modify: `tests/test_manager.py`

**Interfaces:**
- Consumes: `AgentManager._sync_visibility` (Task 10).

- [ ] **Step 1: Write the failing tests**

```python
def test_update_agent_republishes_managed_agent_on_display_name_change(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Old Name",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    runner.calls.clear()

    updated = manager.update_agent(agent.id, display_name="New Name")

    assert updated.visibility_state.profile_published is True
    assert updated.visibility_state.managed_agent_published is True
    subcommands = [c[1] for c in runner.calls if c[0] == "buzz-fleet-signer"]
    assert "publish-agent-profile" in subcommands
    assert "publish-managed-agent" in subcommands


def test_update_agent_joins_new_channel_and_leaves_removed_one(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Channel Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
        channel_ids=["11111111-1111-1111-1111-111111111111"],
    )
    runner.calls.clear()

    updated = manager.update_agent(
        agent.id, channel_ids=["22222222-2222-2222-2222-222222222222"]
    )

    leave_calls = [c for c in runner.calls if c[:2] == ["buzz-fleet-signer", "leave-channel"]]
    join_calls = [c for c in runner.calls if c[:2] == ["buzz-fleet-signer", "join-channel"]]
    assert any("11111111-1111-1111-1111-111111111111" in c for c in leave_calls)
    assert any("22222222-2222-2222-2222-222222222222" in c for c in join_calls)
    assert "11111111-1111-1111-1111-111111111111" not in updated.visibility_state.channels
    assert updated.visibility_state.channels["22222222-2222-2222-2222-222222222222"] == "joined"


def test_update_agent_does_not_touch_visibility_for_old_agent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Old Name",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    from buzz_fleet import state as state_module

    old_style = agent.model_copy(update={"visibility_managed": False})
    state_module.save_agent(old_style)
    runner.calls.clear()

    manager.update_agent(agent.id, display_name="New Name")

    visibility_subcommands = {"compute-auth-tag", "publish-agent-profile", "publish-managed-agent"}
    assert not any(len(c) > 1 and c[1] in visibility_subcommands for c in runner.calls)
```

- [ ] **Step 2: Run to verify all three fail**

Run: `uv run pytest tests/test_manager.py -k "republishes_managed_agent or joins_new_channel or does_not_touch_visibility" -v`
Expected: FAIL — `update_agent` doesn't do any of this yet.

- [ ] **Step 3: Implement in `update_agent`**

Replace the body of `update_agent` (currently just diffing/writing files/restarting/saving) with:

```python
    def update_agent(self, agent_id: str, **changes: object) -> Agent:
        self._ensure_owner_pubkey()
        agents = {a.id: a for a in self.list_agents()}
        current = agents[agent_id]
        updated = current.model_copy(update=changes)

        if current.visibility_managed:
            content_fields = {
                "display_name",
                "system_prompt_source",
                "team_instructions",
                "model",
                "parallelism",
                "respond_to_allowlist",
            }
            vs = updated.visibility_state.model_copy(deep=True)
            if any(f in changes for f in content_fields):
                vs.managed_agent_published = False
                vs.managed_agent_error = None
            if "display_name" in changes:
                vs.profile_published = False
                vs.profile_error = None
            if "channel_add_policy" in changes:
                vs.add_policy_published = False
                vs.add_policy_error = None
            if "channel_ids" in changes:
                old_ids = set(current.channel_ids or [])
                new_ids = set(updated.channel_ids or [])
                agent_nsec = updated.private_key.get_secret_value()
                for channel_id in old_ids - new_ids:
                    try:
                        signer_client.leave_channel(
                            self._runner, self._community.relay_url, agent_nsec, channel_id
                        )
                    except (RuntimeError, json.JSONDecodeError, KeyError):
                        # Best-effort — a failed leave here isn't retried by
                        # ensure_runtime_ready (that only retries joins); the
                        # relay's own state simply lags until the next edit.
                        pass
                    vs.channels.pop(channel_id, None)
                    vs.channel_errors.pop(channel_id, None)
                for channel_id in new_ids - old_ids:
                    vs.channels.setdefault(channel_id, "pending")
            updated = updated.model_copy(update={"visibility_state": vs})
            updated = self._sync_visibility(updated)

        # Preserve any previously-set API keys instead of wiping them on every
        # update — write_agent_files takes explicit key args rather than
        # merging, so we read the existing .env file forward here (Fix 9).
        existing_keys = _read_existing_env_keys(agent_id)
        systemd.write_agent_files(
            updated,
            self._community,
            anthropic_api_key=existing_keys.get("ANTHROPIC_API_KEY"),
            openai_api_key=existing_keys.get("OPENAI_API_KEY"),
        )
        systemctl_client.restart(self._runner, agent_id)
        state.save_agent(updated)
        return updated
```

- [ ] **Step 4: Run to verify all three pass**

Run: `uv run pytest tests/test_manager.py -k "republishes_managed_agent or joins_new_channel or does_not_touch_visibility" -v`
Expected: all pass.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest tests/test_manager.py -v`
Expected: all pass (including the pre-existing `test_update_agent_restarts_without_re_registering` and `test_update_agent_preserves_previously_set_api_keys`, neither of which touches visibility-relevant fields — for them `current.visibility_managed` is `True` from Task 10's default but no `content_fields`/`channel_ids` key appears in `changes`, so the `if` blocks are all skipped and `_sync_visibility` runs its normal idempotent no-op-when-already-done pass).

- [ ] **Step 6: Commit**

```bash
git add src/buzz_fleet/manager.py tests/test_manager.py
git commit -m "manager: reconcile visibility publishes on agent update"
```

---

## Task 13: `delete_agent` teardown

**Files:**
- Modify: `src/buzz_fleet/manager.py`
- Modify: `tests/test_manager.py`

- [ ] **Step 1: Write the failing test**

```python
def test_delete_agent_leaves_channels_retracts_and_archives(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Doomed Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
        channel_ids=[
            "33333333-3333-3333-3333-333333333333",
            "44444444-4444-4444-4444-444444444444",
        ],
    )
    runner.calls.clear()

    manager.delete_agent(agent.id)

    leave_calls = [c for c in runner.calls if c[:2] == ["buzz-fleet-signer", "leave-channel"]]
    # Both channels must be left, not just the first — proves the loop
    # actually iterates every channel_id rather than leaving one and
    # stopping (the exact regression a single-channel test can't catch).
    assert any("33333333-3333-3333-3333-333333333333" in c for c in leave_calls)
    assert any("44444444-4444-4444-4444-444444444444" in c for c in leave_calls)
    subcommands = [c[1] for c in runner.calls if c[0] == "buzz-fleet-signer"]
    assert "retract-managed-agent" in subcommands
    assert "archive-agent" in subcommands
    archive_call = next(c for c in runner.calls if c[:2] == ["buzz-fleet-signer", "archive-agent"])
    assert "--owner-nsec" in archive_call
    assert "retired" in archive_call


def test_delete_agent_continues_leaving_other_channels_if_one_leave_fails(tmp_path: Path, monkeypatch) -> None:
    """Regression test: a failure leaving one channel must not stop the
    loop before it reaches the others, and must not block retract/archive
    afterward — this is the specific fault-isolation behavior a
    single-channel test cannot exercise.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")

    class FlakyLeaveRunner(FakeRunner):
        def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:2] == ["buzz-fleet-signer", "leave-channel"] and "33333333-3333-3333-3333-333333333333" in args:
                self.calls.append(args)
                return subprocess.CompletedProcess(
                    args, 0, stdout=json.dumps({"ok": False, "error": "invalid: channel not found"}), stderr=""
                )
            return super().run(args)

    runner = FlakyLeaveRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Doomed Agent Two",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
        channel_ids=[
            "33333333-3333-3333-3333-333333333333",
            "44444444-4444-4444-4444-444444444444",
        ],
    )
    runner.calls.clear()

    manager.delete_agent(agent.id)  # must not raise despite the first leave-channel failing

    leave_calls = [c for c in runner.calls if c[:2] == ["buzz-fleet-signer", "leave-channel"]]
    assert any("44444444-4444-4444-4444-444444444444" in c for c in leave_calls)
    subcommands = [c[1] for c in runner.calls if c[0] == "buzz-fleet-signer"]
    assert "retract-managed-agent" in subcommands
    assert "archive-agent" in subcommands


def test_delete_agent_skips_visibility_teardown_for_old_agent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Old Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    from buzz_fleet import state as state_module

    old_style = agent.model_copy(update={"visibility_managed": False})
    state_module.save_agent(old_style)
    runner.calls.clear()

    manager.delete_agent(agent.id)

    visibility_subcommands = {"retract-managed-agent", "archive-agent", "leave-channel"}
    assert not any(len(c) > 1 and c[1] in visibility_subcommands for c in runner.calls)
```

- [ ] **Step 2: Run to verify both fail**

Run: `uv run pytest tests/test_manager.py -k "leaves_channels_retracts_and_archives or skips_visibility_teardown" -v`
Expected: FAIL — `delete_agent` does none of this yet.

- [ ] **Step 3: Implement in `delete_agent`**

Replace the body of `delete_agent` with:

```python
    def delete_agent(self, agent_id: str) -> None:
        agents = {a.id: a for a in self.list_agents()}
        agent = agents[agent_id]
        systemctl_client.disable_now(self._runner, agent_id)

        if agent.visibility_managed:
            owner_nsec = self._community.relay_admin_nsec.get_secret_value()
            agent_nsec = agent.private_key.get_secret_value()
            for channel_id in agent.channel_ids or []:
                try:
                    signer_client.leave_channel(self._runner, self._community.relay_url, agent_nsec, channel_id)
                except (RuntimeError, json.JSONDecodeError, KeyError):
                    # Best-effort, matches Desktop's own delete flow — a
                    # channel leave failing must not block the rest of
                    # deletion (the relay membership below still gets
                    # revoked regardless).
                    pass
            try:
                signer_client.retract_managed_agent(self._runner, self._community.relay_url, owner_nsec, agent.public_key)
            except (RuntimeError, json.JSONDecodeError, KeyError):
                pass
            try:
                auth_tag = signer_client.compute_auth_tag(self._runner, owner_nsec, agent.public_key)
                signer_client.archive_agent(
                    self._runner, self._community.relay_url, owner_nsec, agent.public_key, "retired", auth_tag
                )
            except (RuntimeError, json.JSONDecodeError, KeyError):
                pass

        signer_client.remove_member(
            self._runner,
            self._community.relay_url,
            self._community.relay_admin_nsec.get_secret_value(),
            agent.public_key,
        )
        state.delete_agent(self._community.id, agent_id)
        systemd.agent_env_path(agent_id).unlink(missing_ok=True)
        systemd.agent_prompt_path(agent_id).unlink(missing_ok=True)
```

- [ ] **Step 4: Run to verify both pass**

Run: `uv run pytest tests/test_manager.py -k "leaves_channels_retracts_and_archives or skips_visibility_teardown" -v`
Expected: both pass.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest tests/test_manager.py -v`
Expected: all pass, including `test_delete_agent_removes_member_and_stops_unit` and `test_delete_agent_removes_env_and_prompt_files`.

- [ ] **Step 6: Commit**

```bash
git add src/buzz_fleet/manager.py tests/test_manager.py
git commit -m "manager: mirror Desktop's real delete flow (leave channels, retract, archive)"
```

This completes `manager.py` — every lifecycle method now correctly publishes, retries, reconciles, and tears down the visibility events, gated entirely on `visibility_managed`.

---

## Task 14: CLI flags + `agent list` status column

**Files:**
- Modify: `src/buzz_fleet/cli/app.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `visibility.visibility_status_text` (Task 9).
- Produces: a shared `_parse_channel_ids(raw: str | None) -> list[str] | None` helper (used by both `agent_create` and `agent_update`).

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py` already has an established isolation pattern for every `agent create`/`agent update` test: `monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))` plus `monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)` (a small per-test fake class), invoked via the module-level `runner_cli = CliRunner()` and `runner_cli.invoke(app, [...])` — never a real `state.load_community` call and never a real `AgentManager`/signer/relay round trip. Use this exact pattern below; do not invoke `app` without both monkeypatches, or the test would try to load a real `~/.config/buzz-fleet/communities/<id>.json` and construct a real `AgentManager` against whatever community happens to be configured on the machine running the tests — a real, not hypothetical, test-isolation hazard.

Add to `tests/test_cli.py`:

```python
def test_agent_create_rejects_malformed_channel_id(tmp_path, monkeypatch) -> None:
    class FakeAgentManager:
        def __init__(self, runner: object, community: object) -> None:
            pass

        def create_agent(self, **kwargs: object) -> object:
            raise AssertionError("create_agent should not be called for an invalid channel id")

    monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)

    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("You are a test agent.")

    result = runner_cli.invoke(
        app,
        [
            "agent", "create",
            "--community", "eltahir",
            "--display-name", "Bad Channel",
            "--harness", "claude",
            "--prompt-file", str(prompt_file),
            "--channel-ids", "not-a-uuid",
        ],
    )

    assert result.exit_code != 0
    assert "channel" in result.output.lower()


def test_agent_create_accepts_channel_add_policy_choice(tmp_path, monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeAgentManager:
        def __init__(self, runner: object, community: object) -> None:
            pass

        def create_agent(self, **kwargs: object) -> object:
            calls.update(kwargs)
            return SimpleNamespace(id="test-agent", public_key="ab" * 32)

    monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)

    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("You are a test agent.")

    result = runner_cli.invoke(
        app,
        [
            "agent", "create",
            "--community", "eltahir",
            "--display-name", "Policy Agent",
            "--harness", "claude",
            "--prompt-file", str(prompt_file),
            "--channel-add-policy", "nobody",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["channel_add_policy"] == "nobody"


def test_agent_update_rejects_invalid_channel_add_policy(monkeypatch) -> None:
    class FakeAgentManager:
        def __init__(self, runner: object, community: object) -> None:
            pass

        def update_agent(self, agent_id: str, **changes: object) -> object:
            raise AssertionError("update_agent should not be called for an invalid policy")

    monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)

    result = runner_cli.invoke(
        app,
        ["agent", "update", "--community", "eltahir", "agent-1", "--channel-add-policy", "everyone"],
    )

    assert result.exit_code == 1
    assert "channel-add-policy" in result.output


def test_agent_update_parses_channel_ids(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeAgentManager:
        def __init__(self, runner: object, community: object) -> None:
            pass

        def update_agent(self, agent_id: str, **changes: object) -> object:
            calls["changes"] = changes
            return SimpleNamespace(id=agent_id)

    monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)

    channel_id = "12345678-1234-5678-1234-567812345678"
    result = runner_cli.invoke(
        app,
        ["agent", "update", "--community", "eltahir", "agent-1", "--channel-ids", channel_id],
    )

    assert result.exit_code == 0, result.output
    assert calls["changes"] == {"channel_ids": [channel_id]}


def test_agent_list_shows_visibility_status(monkeypatch) -> None:
    """Two agents in different visibility states are listed to prove the
    per-row loop renders a status for each one, not just the first."""
    unmanaged = _agent(id="agent-unmanaged", display_name="Unmanaged")
    synced = _agent(
        id="agent-synced",
        display_name="Synced",
        visibility_managed=True,
        visibility_state=AgentVisibilityState(
            profile_published=True, managed_agent_published=True, add_policy_published=True
        ),
    )

    class FakeAgentManager:
        def __init__(self, runner: object, community: object) -> None:
            pass

        def ensure_runtime_ready(self) -> None:
            pass

        def list_agents(self) -> list[object]:
            return [unmanaged, synced]

    monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)

    result = runner_cli.invoke(app, ["agent", "list", "--community", "eltahir"])

    assert result.exit_code == 0, result.output
    assert "agent-unmanaged\tUnmanaged\tclaude\t—" in result.output
    assert "agent-synced\tSynced\tclaude\tsynced" in result.output
```

This uses `tests/test_cli.py`'s existing `_agent(**overrides) -> Agent` helper (already present in the file, building a minimal `Agent` with sensible defaults) and its `AgentVisibilityState` import — both already available at the top of the file; add `AgentVisibilityState` to the existing `from buzz_fleet.models import Agent, SystemPromptSource` import line if it isn't already there.

- [ ] **Step 2: Run to verify the new tests fail**

Run: `uv run pytest tests/test_cli.py -k "channel_id or channel_add_policy or shows_visibility_status" -v`
Expected: FAIL — neither flag nor the status column exists yet.

- [ ] **Step 3: Implement in `cli/app.py`**

Add near the top, after the existing imports:

```python
import uuid


def _parse_channel_ids(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    ids = [entry.strip() for entry in raw.split(",") if entry.strip()]
    for entry in ids:
        try:
            uuid.UUID(entry)
        except ValueError as e:
            typer.echo(f"Invalid channel id {entry!r} — must be a UUID.", err=True)
            raise typer.Exit(code=1) from e
    return ids or None
```

Add two parameters to `agent_create` (after `respond_to_allowlist`):

```python
    channel_ids: Annotated[
        str | None, typer.Option(help="Comma-separated NIP-29 channel UUIDs to join")
    ] = None,
    channel_add_policy: Annotated[
        str | None, typer.Option(help="Who may add this agent to a new channel: anyone, owner_only, nobody")
    ] = None,
```

Add validation and pass-through in `agent_create`'s body (before the `manager.create_agent(...)` call):

```python
    parsed_channel_ids = _parse_channel_ids(channel_ids)
    if channel_add_policy is not None and channel_add_policy not in ("anyone", "owner_only", "nobody"):
        typer.echo("--channel-add-policy must be one of: anyone, owner_only, nobody", err=True)
        raise typer.Exit(code=1)
```

Add `channel_ids=parsed_channel_ids, channel_add_policy=channel_add_policy,` to the `manager.create_agent(...)` call's keyword arguments (after `respond_to_allowlist=...`).

Add the same two parameters and the same validation to `agent_update`, and add to its `changes` dict:

```python
    if channel_ids is not None:
        changes["channel_ids"] = _parse_channel_ids(channel_ids)
    if channel_add_policy is not None:
        if channel_add_policy not in ("anyone", "owner_only", "nobody"):
            typer.echo("--channel-add-policy must be one of: anyone, owner_only, nobody", err=True)
            raise typer.Exit(code=1)
        changes["channel_add_policy"] = channel_add_policy
```

Update `agent_list` to show the visibility status:

```python
@agent_app.command("list")
def agent_list(community: Annotated[str, typer.Option()]) -> None:
    from buzz_fleet import visibility

    manager = _load_manager(community)
    manager.ensure_runtime_ready()
    for agent in manager.list_agents():
        status = visibility.visibility_status_text(agent)
        typer.echo(f"{agent.id}\t{agent.display_name}\t{agent.harness}\t{status}")
```

- [ ] **Step 4: Run to verify all five pass**

Run: `uv run pytest tests/test_cli.py -k "channel_id or channel_add_policy or shows_visibility_status" -v`
Expected: all pass.

- [ ] **Step 5: Run the full CLI test suite**

Run: `uv run pytest tests/test_cli.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/buzz_fleet/cli/app.py tests/test_cli.py
git commit -m "cli: add --channel-ids/--channel-add-policy, show visibility status in agent list"
```

---

## Task 15: TUI form fields

**Files:**
- Modify: `src/buzz_fleet/tui/screens/agent_form.py`
- Modify: `tests/tui/test_agent_form.py`

**Interfaces:**
- Consumes: `_parse_channel_ids`-equivalent validation (mirrored locally, since `agent_form.py` doesn't import from `cli/app.py`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/tui/test_agent_form.py`:

```python
async def test_submitting_form_with_malformed_channel_id_notifies_instead_of_crashing() -> None:
    manager = FakeManager()
    app = BuzzFleetApp()

    async with app.run_test(size=(80, 50)) as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()
        app.screen.query_one("#display-name-input", Input).value = "Test Agent"
        app.screen.query_one("#prompt-input", TextArea).text = "You are a test agent."
        app.screen.query_one("#channel-ids-input", Input).value = "not-a-uuid"
        await pilot.click("#submit-button")
        await pilot.pause()

    assert manager.created == []


async def test_submitting_form_passes_channel_ids_and_add_policy_to_create_agent() -> None:
    manager = FakeManager()
    app = BuzzFleetApp()

    async with app.run_test(size=(80, 50)) as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()
        app.screen.query_one("#display-name-input", Input).value = "Test Agent"
        app.screen.query_one("#prompt-input", TextArea).text = "You are a test agent."
        app.screen.query_one("#channel-ids-input", Input).value = "11111111-1111-1111-1111-111111111111"
        app.screen.query_one("#channel-add-policy-select", Select).value = "nobody"
        await pilot.click("#submit-button")
        await pilot.pause()

    assert manager.created[0]["channel_ids"] == ["11111111-1111-1111-1111-111111111111"]
    assert manager.created[0]["channel_add_policy"] == "nobody"
```

- [ ] **Step 2: Run to verify both fail**

Run: `uv run pytest tests/tui/test_agent_form.py -k "malformed_channel_id or channel_ids_and_add_policy" -v`
Expected: FAIL — neither field exists in the form yet.

- [ ] **Step 3: Implement in `agent_form.py`**

Add `import uuid` near the top.

Add the two new fields to the "Access" section in `compose()` (after the existing `respond-to-allowlist-input` block):

```python
        with _section("Access"):
            yield Input(
                value=(
                    ", ".join(self._agent.respond_to_allowlist)
                    if self._agent and self._agent.respond_to_allowlist
                    else ""
                ),
                placeholder="Respond-to allowlist pubkeys, comma-separated (optional)",
                id="respond-to-allowlist-input",
            )
            yield Input(
                value=(", ".join(self._agent.channel_ids) if self._agent and self._agent.channel_ids else ""),
                placeholder="Channel IDs, comma-separated (optional)",
                id="channel-ids-input",
            )
            yield Static("Channel add policy:")
            yield Select(
                [("anyone", "anyone"), ("owner_only", "owner_only"), ("nobody", "nobody")],
                value=(self._agent.channel_add_policy if self._agent and self._agent.channel_add_policy else "owner_only"),
                allow_blank=False,
                id="channel-add-policy-select",
            )
```

Add a validation helper method to `AgentFormScreen` (alongside the existing `_parse_optional_int`):

```python
    def _parse_optional_uuid_list(self, input_id: str) -> list[str] | None:
        raw = self.query_one(input_id, Input).value.strip()
        if not raw:
            return None
        ids = [entry.strip() for entry in raw.split(",") if entry.strip()]
        for entry in ids:
            uuid.UUID(entry)  # raises ValueError on malformed input
        return ids or None
```

In `on_button_pressed`, extend the existing `try`/`except ValueError` block that already wraps `_parse_optional_int` calls to also parse channel ids, and read the new Select's value:

```python
        try:
            parallelism = self._parse_optional_int("#parallelism-input")
            idle_timeout_seconds = self._parse_optional_int("#idle-timeout-input")
            max_turn_duration_seconds = self._parse_optional_int("#max-turn-duration-input")
            channel_ids = self._parse_optional_uuid_list("#channel-ids-input")
        except ValueError:
            self.notify(
                "Parallelism, idle timeout, max turn duration must be whole numbers, "
                "and channel IDs must be valid UUIDs.",
                severity="error",
            )
            return

        channel_add_policy = self.query_one("#channel-add-policy-select", Select).value
```

Add `channel_ids` and `channel_add_policy` to both the `changes` dict (edit-mode branch) and the `manager.create_agent(...)` call (create-mode branch) in `on_button_pressed`, alongside the other new fields already there:

```python
                changes: dict[str, object] = {
                    "display_name": display_name,
                    "harness": harness,
                    "team_instructions": team_instructions,
                    "model": model,
                    "parallelism": parallelism,
                    "idle_timeout_seconds": idle_timeout_seconds,
                    "max_turn_duration_seconds": max_turn_duration_seconds,
                    "respond_to_allowlist": respond_to_allowlist,
                    "channel_ids": channel_ids,
                    "channel_add_policy": channel_add_policy,
                }
```

```python
                self._manager.create_agent(
                    display_name=display_name,
                    harness=harness,
                    system_prompt_source=prompt_source,
                    team_instructions=team_instructions,
                    model=model,
                    parallelism=parallelism,
                    idle_timeout_seconds=idle_timeout_seconds,
                    max_turn_duration_seconds=max_turn_duration_seconds,
                    respond_to_allowlist=respond_to_allowlist,
                    channel_ids=channel_ids,
                    channel_add_policy=channel_add_policy,
                )
```

- [ ] **Step 4: Run to verify both pass**

Run: `uv run pytest tests/tui/test_agent_form.py -k "malformed_channel_id or channel_ids_and_add_policy" -v`
Expected: both pass.

- [ ] **Step 5: Run the full TUI form test suite**

Run: `uv run pytest tests/tui/test_agent_form.py -v`
Expected: all pass (the new fields add height to the "Access" section — re-run the visual repro from this session's earlier `TextArea` fix if anything looks cramped; the `.form-section { height: auto }` change already made means the screen simply grows/scrolls, not clips).

- [ ] **Step 6: Commit**

```bash
git add src/buzz_fleet/tui/screens/agent_form.py tests/tui/test_agent_form.py
git commit -m "tui: add channel-ids and channel-add-policy fields to agent form"
```

---

## Task 16: dashboard "Visibility" column

**Files:**
- Modify: `src/buzz_fleet/tui/screens/dashboard.py`
- Create: `tests/tui/test_dashboard.py` (no dashboard test file exists today)

**Interfaces:**
- Consumes: `visibility.visibility_status_text` (Task 9).

- [ ] **Step 1: Write the failing test**

Create `tests/tui/test_dashboard.py`:

```python
import pytest

from buzz_fleet.models import AgentVisibilityState
from buzz_fleet.tui.screens.dashboard import _visibility_display


def test_visibility_display_old_agent_is_inactive_colored_dash() -> None:
    text, color = _visibility_display(False, AgentVisibilityState())
    assert text == "—"


def test_visibility_display_synced_is_success_colored() -> None:
    text, color = _visibility_display(
        True,
        AgentVisibilityState(profile_published=True, managed_agent_published=True, add_policy_published=True),
    )
    assert text == "synced"


def test_visibility_display_pending_is_warning_colored() -> None:
    text, color = _visibility_display(True, AgentVisibilityState())
    assert text == "pending"


def test_visibility_display_error_is_error_colored() -> None:
    text, color = _visibility_display(
        True, AgentVisibilityState(profile_error="invalid: bad thing")
    )
    assert text.startswith("error:")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/tui/test_dashboard.py -v`
Expected: FAIL — `_visibility_display` doesn't exist.

- [ ] **Step 3: Implement in `dashboard.py`**

Add the import (alongside the existing ones):

```python
from buzz_fleet import visibility
from buzz_fleet.models import Agent, AgentVisibilityState
```

(`Agent` isn't used by Step 3's version of `_visibility_display` yet — it's needed by Step 5's refactor below. Importing it now avoids a second import-line edit later.)

Add a helper function near `_STATUS_DISPLAY` (same file-level placement, same "text, color" tuple convention as the existing systemd-status column):

```python
def _visibility_display(visibility_managed: bool, state: AgentVisibilityState) -> tuple[str, str]:
    # Mirrors visibility_status_text's own branching directly rather than
    # calling it — that function takes a whole Agent, and this dashboard
    # helper also needs a color per state, not just text. Step 5 below
    # removes this duplication once the tests pass.
    if not visibility_managed:
        return "—", STATUS_INACTIVE
    errors = [e for e in (state.profile_error, state.managed_agent_error, state.add_policy_error) if e]
    errors.extend(state.channel_errors.values())
    if errors:
        return f"error: {errors[0]}", "#C1553A"
    channels_joined = all(status == "joined" for status in state.channels.values())
    if state.profile_published and state.managed_agent_published and state.add_policy_published and channels_joined:
        return "synced", "#7FB069"
    return "pending", "#C98A2C"
```

Update `compose()`'s column headers:

```python
        table.add_columns("id", "display name", "harness", "status", "visibility")
```

Update `refresh_agents()`'s row-building loop:

```python
        for agent in list_agents():
            text, color = _STATUS_DISPLAY[agent_status(agent.id)]
            vis_text, vis_color = _visibility_display(agent.visibility_managed, agent.visibility_state)
            table.add_row(
                agent.id,
                agent.display_name,
                agent.harness,
                Text(text, style=f"bold {color}"),
                Text(vis_text, style=f"bold {vis_color}"),
            )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/tui/test_dashboard.py -v`
Expected: all 4 pass.

- [ ] **Step 5: Fix `visibility.py` duplication**

Task 16's `_visibility_display` duplicates `visibility.visibility_status_text`'s branching logic (needed because the dashboard also wants a color, which the shared text-only helper doesn't carry). Reduce the duplication by having `_visibility_display` call `visibility.visibility_status_text` for the text and deriving only the color locally:

```python
def _visibility_display(agent: Agent) -> tuple[str, str]:
    text = visibility.visibility_status_text(agent)
    if text == "—":
        return text, STATUS_INACTIVE
    if text == "synced":
        return text, "#7FB069"
    if text.startswith("error:"):
        return text, "#C1553A"
    return text, "#C98A2C"  # "pending"
```

Replace `tests/tui/test_dashboard.py`'s entire contents with the version below — it duplicates the small `_agent()` builder from `test_visibility.py` locally (a private, underscore-prefixed test helper isn't meant to be imported across test modules) and updates all 4 tests to build a real `Agent` instead of passing `visibility_managed`/`state` as separate arguments:

```python
from datetime import UTC, datetime

from buzz_fleet.models import Agent, AgentVisibilityState, SystemPromptSource
from buzz_fleet.tui.screens.dashboard import _visibility_display


def _agent(**overrides) -> Agent:
    defaults = dict(
        id="test-agent",
        community_id="eltahir",
        display_name="Test Agent",
        harness="claude",
        private_key="nsec1x",
        public_key="a" * 64,
        system_prompt_source=SystemPromptSource(kind="inline", text="hi"),
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return Agent(**defaults)


def test_visibility_display_old_agent_is_inactive_colored_dash() -> None:
    text, _color = _visibility_display(_agent())
    assert text == "—"


def test_visibility_display_synced_is_success_colored() -> None:
    agent = _agent(
        visibility_managed=True,
        visibility_state=AgentVisibilityState(
            profile_published=True, managed_agent_published=True, add_policy_published=True
        ),
    )
    text, _color = _visibility_display(agent)
    assert text == "synced"


def test_visibility_display_pending_is_warning_colored() -> None:
    text, _color = _visibility_display(_agent(visibility_managed=True))
    assert text == "pending"


def test_visibility_display_error_is_error_colored() -> None:
    agent = _agent(visibility_managed=True, visibility_state=AgentVisibilityState(profile_error="invalid: bad thing"))
    text, _color = _visibility_display(agent)
    assert text.startswith("error:")
```

Update `refresh_agents()`'s row-building loop (from Step 3) to call `_visibility_display(agent)` with the whole agent instead of the two separate arguments:

```python
        for agent in list_agents():
            text, color = _STATUS_DISPLAY[agent_status(agent.id)]
            vis_text, vis_color = _visibility_display(agent)
            table.add_row(
                agent.id,
                agent.display_name,
                agent.harness,
                Text(text, style=f"bold {color}"),
                Text(vis_text, style=f"bold {vis_color}"),
            )
```

- [ ] **Step 6: Run all TUI tests**

Run: `uv run pytest tests/tui/ -v`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/buzz_fleet/tui/screens/dashboard.py tests/tui/test_dashboard.py
git commit -m "dashboard: add Visibility status column"
```

---

## Final verification

- [ ] Run the full test suite: `uv run pytest -v` — expect all tests (existing + every test added by this plan) to pass.
- [ ] Run `uv run ruff check .` — expect no findings.
- [ ] Run `cd signer && cargo test` — expect all tests (existing + Tasks 1-7) to pass.
- [ ] Run `cd signer && cargo build --release` — expect a clean release build (confirms the new `buzz-sdk` git dependency and all 8 new subcommands compile together, not just individually during TDD).
- [ ] Update `README.md`'s "Persona templates"/"Manage agents" sections and `CLAUDE.md`'s "Documentation debt" list to describe the new `--channel-ids`/`--channel-add-policy` fields and the dashboard's Visibility column — following the exact pattern already used there for `team_instructions` and the other harness-config fields (this plan does not include a dedicated docs task; fold it into whichever of Tasks 14-16 the executing agent finishes last, or do it as one final small commit).
- [ ] Manual acceptance test against a real Buzz community (this feature has no automated relay-integration test, matching the existing convention for `ensure_runtime_ready`'s other three concerns): create a new agent with a real channel UUID, confirm it appears in Desktop's Agents view as owner-verified, confirm the dashboard shows `synced`, then delete it and confirm it disappears from Desktop's pickers/autocomplete.
