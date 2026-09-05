# Agent visibility in Buzz Desktop/mobile — design spec

Status: approved, ready for implementation plan. Reached via the
`brainstorming` skill (approach + sectioned design) and the `grilling`
skill across five rounds (migration scope, channel join, provider field,
add-policy inclusion/config, channel-id cardinality, create-failure
handling, connection batching, add-policy widget, update/delete channel
reconciliation, self-healing integration, mandatory-vs-optional publishes,
add-policy default, permanent-vs-transient failures, dashboard status,
old-agent exemption, team_instructions placement, `.agent.json` schema
reconciliation) plus four direct fact-finding passes against
`/home/dev/apps/buzz` (event schemas, Desktop's create/delete reference
flows, `buzz-acp`'s own runtime behavior, the full `buzz-agent-snapshot` v1
export schema).

Standing principle established during design: **Buzz Desktop is the living
reference implementation for how a well-behaved Buzz client uses the Nostr
API.** Wherever this spec had to choose between inventing new behavior and
mirroring what Desktop already does, it mirrors Desktop — including
Desktop's own inconsistencies (e.g. delete-time channel-leaving), rather
than "improving" on a working reference.

## Problem

Agents created by `buzz-fleet` (`laravel-backend-developer`, `my-cdx`) show
`active` in `buzz-fleet`'s own dashboard — which only reflects local
`systemctl --user` status — but never appear in Buzz Desktop's or mobile's
"Agents" view for the same community, even though the community's admin
key was used to create them.

Root cause, confirmed by reading `/home/dev/apps/buzz`'s actual source
(not assumption): `buzz-fleet` and Desktop's Agents view use two entirely
unrelated Nostr subsystems.

- `buzz-fleet` today (`manager.py::create_agent` →
  `signer_client.add_member`) publishes only a **kind:9030** relay-admin
  event, with no `role` tag. The relay's own default for a missing role
  tag is `"member"` (`crates/buzz-relay/src/handlers/relay_admin.rs:320`),
  so the agent lands in `relay_members` as a plain community member —
  functionally identical to a human joining. `"agent"` is not even a
  legal value there (`role != "admin" && role != "member"` is rejected,
  `relay_admin.rs:329-330`).
- Desktop's Agents view instead reads: kind:9000 (NIP-29 channel
  membership, `role="bot"`, which the relay turns into a signed kind:39002
  roster snapshot), a kind:30177 owner-signed "managed-agent" record, an
  optional kind:10100 agent-authored profile/add-policy record, and a
  kind:0 profile carrying a NIP-OA `auth` tag that lets any reader
  cryptographically verify who owns the agent. None of these four kinds
  has ever been published by `buzz-fleet`.

This spec adds the missing publish path.

## Goals

- A `buzz-fleet`-created agent becomes visible and correctly attributed
  (owner-verified) in Desktop's/mobile's Agents/owned-agent-discovery UI.
- Optionally, the agent becomes a real, functioning member of one or more
  NIP-29 channels (able to see and respond to messages there), not just
  "known but not participating anywhere."
- The whole thing self-heals the same way the existing runtime concerns do
  (`AgentManager.ensure_runtime_ready`) — a transient failure during
  create/update is retried automatically, not a one-shot "hope it worked."

## Non-goals

- **No backfill.** `laravel-backend-developer` and `my-cdx` (and any other
  agent created before this feature ships) are never retroactively
  published. See "Old-agent exemption" below for the exact mechanism.
- **No channel picker.** The user supplies channel UUIDs by hand (copied
  from Desktop or `buzz-cli`); `buzz-fleet` does not fetch/list a
  community's channels. A future enhancement, not blocking this one.
- **No `provider` support.** `buzz-fleet` has no concept of an LLM
  provider distinct from `harness` (already a documented gap in
  `CLAUDE.md`'s "Known gaps" — provider selection has no `buzz-acp`
  env-var hook at all yet). The published kind:30177's `provider` field
  is always omitted (`null`/absent), never guessed from `harness`.
- **No `persona_id`/`persona_source_version` linkage.** `buzz-fleet`
  agents are always "standalone" instances on the wire — there is no
  local concept of a kind:30175 shared definition to link back to.
- **No agent "memory" bundles** (the `.agent.json` format's `memory`
  object — `level`/`entries`). Unrelated feature (encrypted, owner-decrypted
  memory), out of scope.
- **No `.agent.json` importer changes.** Investigated as part of this
  design (to make sure the new publish-path schema and the existing
  import-path schema didn't silently drift apart) and found to already be
  correct: `personas.py`'s deliberate omission of
  `definition.respondToAllowlist` on import matches Desktop's own import
  dialog default, and `definition.provider` has nowhere to go for the same
  reason `provider` is out of scope for publishing. Nothing to change.

## Architecture

Two new git dependencies' worth of reuse, one deliberate hand-roll.

**Reuse `buzz-sdk` (new git dependency in `signer/Cargo.toml`, same repo
and rev as the existing `buzz-ws-client` dependency)** for the two event
kinds it already has validated, dependency-light builders for:
- `buzz_sdk::builders::build_add_member(channel_id: Uuid, target_pubkey: &str, role: Option<MemberRole>)`
  → kind:9000.
- `buzz_sdk::builders::build_remove_member(channel_id: Uuid, target_pubkey: &str)`
  → kind:9001.
- `buzz_sdk::nip_oa::compute_auth_tag(owner_keys: &Keys, agent_pubkey: &PublicKey, conditions: &str)`
  → the NIP-OA `auth` tag string, `owner_keys.sign_schnorr` under the hood.
  `buzz-sdk` has no networking dependency — it is literally
  `buzz-core + nostr + uuid + serde + serde_json + thiserror`, the same
  weight class the signer already carries.

**Hand-roll in `buzz-fleet-signer` itself** the three kinds with no
externally reusable crate — their real schemas live in Desktop's
Tauri-only backend (`desktop/src-tauri/src/managed_agents/agent_events.rs`,
`desktop/src-tauri/src/events.rs`) or its binary-only CLI crate, neither
built for external reuse:
- kind:0 profile (mirroring `desktop/src-tauri/src/events.rs:428-453`'s
  `build_profile`: snake_case NIP-01 content `{"display_name", "name",
  "picture", "about", "nip05"}`, with `name`/`picture`/`nip05`/`about`
  always omitted by `buzz-fleet` — it has no avatar/about-text concept,
  matching the persona-picker spec's own established exclusion of those
  fields — plus the NIP-OA `auth` tag as an event tag).
- kind:30177 managed-agent record, mirroring
  `ManagedAgentEventContent` (`agent_events.rs:38-60`) **exactly**,
  snake_case field names and all — this is the live, persona-linked wire
  event, a genuinely different type from `.agent.json`'s camelCase,
  lineage-stripped `AgentSnapshotDefinition`. Do not copy the `.agent.json`
  shape here; they solve different problems and this was a real,
  confirmed point of confusion during design that a fact-finding pass
  resolved. Every struct field and its buzz-fleet source is enumerated
  under "kind:30177 content mapping" below.
- kind:10100 agent-profile record, mirroring the one field the relay
  actually reads (`channel_add_policy`, `crates/buzz-relay/src/handlers/
  side_effects.rs:1246-1278`) — content is `{"channel_add_policy": "anyone"
  | "owner_only" | "nobody"}`, nothing else (no legacy directory fields;
  `buzz-fleet` has no `status`/`capabilities`/`channels` concept to put
  there and inventing one is out of scope).

Each hand-rolled struct/builder in the new Rust module carries a doc
comment citing the exact upstream file and line its shape was copied from,
so a future schema drift upstream is at least detectable by diffing
against a named source, not silently invisible — directly because
"schema drifts silently" is an already-documented, already-hit bug class
in this codebase (see `CLAUDE.md`'s EF-migration-discipline-style framing
for why hand-authored things that mirror a generator need extra care).

Rejected alternatives: hand-rolling all five kinds (loses `buzz-sdk`'s free
correctness for the two it already covers); building these events in
Python via a Nostr library instead of the Rust signer (violates the
project's own stated architecture rule in `README.md`: "every other part
of `buzz-fleet` shells out to [`buzz-fleet-signer`] rather than handling
keys itself").

## Data model changes (`models.py`)

```python
class Agent(BaseModel):
    # ... existing fields unchanged ...
    channel_ids: list[str] | None = None
    channel_add_policy: Literal["anyone", "owner_only", "nobody"] | None = None
    visibility_managed: bool = False
    visibility_state: AgentVisibilityState = Field(default_factory=lambda: AgentVisibilityState())


class AgentVisibilityState(BaseModel):
    """Per-sub-publish status, tracked so `ensure_runtime_ready` retries only
    what's actually missing/failed, and so a permanently-broken input (e.g.
    a nonexistent channel UUID) is distinguished from one still pending."""

    profile_published: bool = False
    managed_agent_published: bool = False
    add_policy_published: bool = False
    # keyed by channel UUID string; value is the outcome of the last attempt
    channels: dict[str, Literal["pending", "joined", "error"]] = Field(default_factory=dict)
    # human-readable reason, set only when the corresponding *_published
    # flag is False AND the failure was classified permanent (see
    # "Permanent vs. transient failures" below) — surfaced verbatim in the
    # TUI/CLI status column.
    profile_error: str | None = None
    managed_agent_error: str | None = None
    add_policy_error: str | None = None
    channel_errors: dict[str, str] = Field(default_factory=dict)
```

### Old-agent exemption

`visibility_managed` defaults to `False`. `create_agent` sets it `True`
explicitly for every newly-created agent, always (see "Mandatory vs.
optional publishes" below) — it is not a user-facing flag, just an
internal marker. An `Agent` record written to disk before this feature
existed loads with `visibility_managed=False` via the Pydantic default
(the field is simply absent from its saved JSON). `ensure_runtime_ready`'s
new fourth self-healing concern (below) checks this flag first and returns
immediately if `False` — this is the entire mechanism that keeps
`laravel-backend-developer`/`my-cdx` permanently untouched without any
separate migration/versioning step. There is no code path, anywhere, that
sets `visibility_managed = True` on an existing agent; the only writer is
`create_agent`.

## kind:30177 content mapping

Every field of `ManagedAgentEventContent`, and where its value comes from:

| Wire field (snake_case) | Source | Notes |
|---|---|---|
| `name` | `Agent.display_name` | |
| `persona_id` | always `None` | out of scope, see Non-goals |
| `system_prompt` | `Agent.system_prompt_source` resolved text, with `Agent.team_instructions` prepended if set (see below) | never omitted for a slimming reason — `buzz-fleet` agents are always standalone (no `persona_id`), so the full quad is always published per `ManagedAgentEventContent`'s own slimming rule (`agent_events.rs:70-107`: fields are only omitted when `persona_id.is_some()`) |
| `model` | `Agent.model` | `None` if unset |
| `provider` | always `None` | out of scope, see Non-goals |
| `persona_source_version` | always `None` | out of scope, see Non-goals |
| `parallelism` | `Agent.parallelism`, defaulting to `1` if `None` | field is non-optional on the wire; `1` matches `buzz-acp`'s own default when `BUZZ_ACP_AGENTS` is unset |
| `respond_to` | derived: `"allowlist"` if `Agent.respond_to_allowlist` is set and non-empty, else `"owner-only"` | matches `buzz-acp`'s actual runtime default (`BUZZ_ACP_AGENT_OWNER`/`respond_to=owner-only` out of the box, per the self-healing runtime doc in `CLAUDE.md`) — never `"anyone"`, `buzz-fleet` has no UI path that would justify publishing that |
| `respond_to_allowlist` | `Agent.respond_to_allowlist`, or `[]` if `None` | |

**`team_instructions` placement**: kind:30177 has no dedicated slot for
it — that's a `buzz-fleet`-only convention with no wire equivalent. If
`Agent.team_instructions` is set, it is concatenated *before* the resolved
system prompt in the published `system_prompt` field, separated by a
blank line, e.g.:

```
<team_instructions>

<resolved system prompt>
```

Rationale: silently dropping it would mean anyone viewing/editing the
agent's prompt from Desktop sees something that looks complete but is
missing a real, runtime-enforced part of it. Concatenation keeps the
published prompt accurate even though the two concepts aren't separately
labeled on the wire.

## kind:10100 content

```json
{"channel_add_policy": "<Agent.channel_add_policy, defaulting to \"owner_only\" if None>"}
```

`channel_add_policy` becomes a new configurable field end-to-end (CLI flag,
TUI field, `Agent` model field) — not a fixed constant — per the explicit
instruction that this feature should not be MVP-trimmed. Default when the
field is left blank in the TUI/CLI is `"owner_only"`, matching the same
safe-by-default philosophy as `respond_to`'s default above. This value
only ever matters for a *third-party* kind:9000 add (someone other than
the agent itself adding it to a new channel) — `buzz-fleet`'s own
self-adds (below) always succeed regardless of this policy
(`side_effects.rs:419-422`, "Self-add: always allowed regardless of
policy").

## New `Agent` fields surfaced in CLI + TUI

Following the existing convention (every optional harness-config field
appears as both a CLI flag and a TUI input):

- `--channel-ids` (CLI, comma-separated) / a new TUI `Input` field
  "Channel IDs, comma-separated (optional)" in the "Access" section
  (alongside `respond_to_allowlist`, since both are relay-identifier
  lists) — parsed the same way `respond_to_allowlist` already is
  (split on comma, strip whitespace, drop empties, `None` if the result
  is empty). Client-side format validation: each entry must parse as a
  UUID (`uuid.UUID(entry)`); a malformed entry is rejected in the
  TUI/CLI with a clear error before any signer subprocess is even
  invoked — exactly like `_parse_optional_int`'s existing pattern for
  numeric fields, applied to a new `_parse_optional_uuid_list` helper.
- `--channel-add-policy {anyone,owner_only,nobody}` (CLI,
  `typer.Option` with a `click.Choice`-style constraint) / a new TUI
  `Select` dropdown (not a free-text `Input` — unlike
  `respond_to_allowlist`, which is genuinely freeform pubkeys, this field
  has exactly three legal values, and a typo becoming a silently-ignored
  side-effect field is worse than a fixed choice list) with options
  `anyone` / `owner_only` (default) / `nobody`, placed next to
  `respond_to_allowlist` in the "Access" section.

## Mandatory vs. optional publishes

kind:0, kind:30177, and kind:10100 are **always** published for every
newly-created agent, unconditionally — there is no opt-out toggle. The
entire point of this feature is fixing "my agents don't show up"; a
per-agent "stay invisible" switch nobody asked for would only add surface
area. kind:9000 (channel join) is the one genuinely optional piece,
gated purely on whether `channel_ids` is non-empty.

## Signer changes (`signer/src/`)

New module `signer/src/agent_events.rs` (hand-rolled builders, each
doc-commented with its upstream source citation per "Architecture" above)
plus new `Command` variants in `main.rs`, following the existing pattern
exactly (`run_publish(relay, nsec, builder)` already accepts an arbitrary
nsec — no plumbing changes needed, only new builders and new CLI
subcommands):

```rust
/// Compute (but do not publish) a NIP-OA auth tag. Needs only the owner's
/// nsec and the agent's public key — never the agent's own key material.
ComputeAuthTag {
    #[arg(long)] owner_nsec: String,
    #[arg(long)] agent_pubkey: String,
}
// stdout: {"ok": true, "auth_tag": "[\"auth\",\"<owner_hex>\",\"\",\"<sig_hex>\"]"}
// conditions is always "" — matches Desktop's own kind:0-embedding call
// site (`relay.rs`'s call to `compute_auth_tag(&owner, &agent, "")`).

/// Publish the agent's own kind:0 profile, signed by the agent's own key,
/// embedding a pre-computed auth tag.
PublishAgentProfile {
    #[arg(long)] relay: String,
    #[arg(long)] agent_nsec: String,
    #[arg(long)] display_name: String,
    #[arg(long)] auth_tag: String,  // the raw JSON-array string from ComputeAuthTag
}

/// Publish (owner-signed) or retract the kind:30177 managed-agent record.
PublishManagedAgent {
    #[arg(long)] relay: String,
    #[arg(long)] owner_nsec: String,
    #[arg(long)] agent_pubkey: String,
    #[arg(long)] content_file: PathBuf,  // raw JSON bytes, built by manager.py — see below
}
RetractManagedAgent {
    #[arg(long)] relay: String,
    #[arg(long)] owner_nsec: String,
    #[arg(long)] agent_pubkey: String,
}

/// Publish (agent-signed) the kind:10100 add-policy record.
PublishAgentAddPolicy {
    #[arg(long)] relay: String,
    #[arg(long)] agent_nsec: String,
    #[arg(long)] policy: String,  // "anyone" | "owner_only" | "nobody"
}

/// Self-join/leave one NIP-29 channel (agent-signed).
JoinChannel {
    #[arg(long)] relay: String,
    #[arg(long)] agent_nsec: String,
    #[arg(long)] channel_id: String,  // parsed as Uuid
}
LeaveChannel {
    #[arg(long)] relay: String,
    #[arg(long)] agent_nsec: String,
    #[arg(long)] channel_id: String,
}

/// File a NIP-IA archive request (used only at delete time).
ArchiveAgent {
    #[arg(long)] relay: String,
    #[arg(long)] agent_nsec: String,
    #[arg(long)] reason: String,  // always "retired" from buzz-fleet
}
```

`PublishManagedAgent` takes a `--content-file` rather than individual
`--system-prompt`/etc. flags: `system_prompt` can be several KB (a full
persona body plus team instructions concatenated), and passing that
through `argv` risks `ARG_MAX` on some systems for a pathological input.
`manager.py` builds the exact `ManagedAgentEventContent`-shaped JSON
(per the field mapping table above) and writes it to a short-lived temp
file; the signer reads the file's raw bytes verbatim as the event
`content` string and signs kind:30177 with `d = agent_pubkey`. This keeps
the *schema construction* in Python (easy to test against the mapping
table with plain `assert`s) while the *signing and publishing* stays in
Rust, matching the existing division of responsibility.

Each new subcommand is one connect→auth→publish→disconnect round trip,
matching every existing signer subcommand's style (`AddMember`,
`RemoveMember`, etc.) — no new "batch multiple events over one connection"
subcommand. Agent creation already costs a key-mint, a relay-membership
publish, and a systemd unit write; a few hundred more milliseconds across
up to 7 additional round trips (profile, managed-agent, add-policy, and
one join per channel) is negligible against that, and a batching
subcommand would add real Rust complexity (event ordering, partial-failure
reporting inside one process) for no measured benefit.

## Manager orchestration (`manager.py`)

### `create_agent`

After the existing key-mint + kind:9030 relay-membership + systemd-unit
steps (unchanged), if the new agent's `visibility_managed` is being set
`True` (always, per "Mandatory vs. optional publishes"):

1. `compute-auth-tag` (owner nsec, agent pubkey) — local computation, no
   network failure mode beyond the relay commands below not even applying
   here since this doesn't publish anything.
2. `publish-agent-profile` (agent nsec, display name, the auth tag from
   step 1) → sets `visibility_state.profile_published`.
3. `publish-managed-agent` (owner nsec, agent pubkey, content file built
   from the mapping table) → sets `visibility_state.managed_agent_published`.
4. `publish-agent-add-policy` (agent nsec, `channel_add_policy` or
   `"owner_only"`) → sets `visibility_state.add_policy_published`.
5. For each UUID in `channel_ids` (if any): `join-channel` (agent nsec,
   channel id) → sets `visibility_state.channels[id] = "joined"` on
   success.

Each step's outcome (success / transient failure / permanent failure — see
below) is recorded into `visibility_state` and the agent record is saved
regardless of how many steps succeeded. `create_agent` does not raise and
does not roll back systemd/relay-membership work already done if a
visibility step fails — this is the self-healing philosophy from Q1,
made concrete by step 3 below (`ensure_runtime_ready`).

### `ensure_runtime_ready` — fourth self-healing concern

Added alongside the existing three (`buzz-acp` binary, adapter path
resolution, owner pubkey backfill), run for every agent on every
`create_agent`/`update_agent`/dashboard-refresh/`agent list` call, exactly
like the other three:

```
if not agent.visibility_managed:
    return  # old agent, permanently exempt — see "Old-agent exemption"

if not agent.visibility_state.profile_published and agent.visibility_state.profile_error is None:
    retry step 2 above
if not agent.visibility_state.managed_agent_published and agent.visibility_state.managed_agent_error is None:
    retry step 3 above
if not agent.visibility_state.add_policy_published and agent.visibility_state.add_policy_error is None:
    retry step 4 above
for channel_id, status in agent.visibility_state.channels.items():
    if status == "pending":
        retry join-channel for channel_id
```

Never re-attempts a step whose `*_error` is already set — that is
precisely the "permanent failure" terminal state (below); only a `None`
error with `*_published=False` (still genuinely pending, e.g. the relay
was briefly unreachable) is retried.

### `update_agent`

- Any change to `display_name` → republish kind:0 (step 2).
- Any change to `display_name`, `system_prompt_source`, `team_instructions`,
  `model`, `parallelism`, `respond_to_allowlist` → republish kind:30177
  (step 3), since all of these feed the content mapping table above.
- Any change to `channel_add_policy` → republish kind:10100 (step 4).
- Diff old vs. new `channel_ids`: newly-added UUIDs get `join-channel`
  (same as create); UUIDs present before but no longer in the list get
  `leave-channel` (kind:9001) published immediately, not deferred to a
  self-healing pass — this was a deliberate, explicit decision (Q9):
  keeping "what's in `buzz-fleet`'s config" and "what the relay says" in
  sync is safer than a form field silently going stale, consistent with
  how every other field on `update_agent` already works (submitting a
  change actually changes reality).
- None of the above apply if `agent.visibility_managed` is `False` (old
  agent) — `update_agent` never flips `visibility_managed` to `True`
  retroactively; that flag is create-time-only, permanently.

### `delete_agent`

Mirrors Desktop's actual, complete delete flow exactly (confirmed via
`useManagedAgentActions.ts`'s `handleDelete` calling
`removeAgentFromAllChannels` *and* the backend `delete_managed_agent`
command's kind:5 + kind:9035 — the full picture required two separate
fact-finding passes to establish, since the first pass only saw the
backend half):

1. `leave-channel` (kind:9001) for every UUID in `channel_ids`.
2. `retract-managed-agent` (kind:5 NIP-09 deletion, single `a`-tag
   `30177:<owner_pubkey>:<agent_pubkey>`, no `e`-tag).
3. `archive-agent` (kind:9035, `reason="retired"`) — the specific act that
   stops the identity lingering in Desktop's member pickers/autocomplete
   after deletion, per Desktop's own code comment
   (`agents_pending.rs:76-87`).

Only for agents with `visibility_managed=True` — an old, unmanaged agent's
deletion is unchanged (no new Nostr calls), since it never published any
of this in the first place.

## Permanent vs. transient failures

Every signer subcommand above returns `{"ok": false, "error": "<relay's
literal rejection message>"}` on failure (same convention as every
existing subcommand). `manager.py` classifies the error string:

- **Transient** (retry indefinitely via `ensure_runtime_ready`): anything
  that looks like a connection/timeout failure — no distinguishing prefix
  from the relay, so classified by exception type from
  `buzz_ws_client`/the subprocess call (connection refused, timeout,
  non-zero exit with no parseable JSON) rather than by string-matching
  relay text.
- **Permanent** (never retried again, surfaced immediately): a relay
  rejection with the `"invalid: ..."` prefix Nostr's NIP-01 `OK` message
  convention uses for a message the relay parsed and explicitly refused —
  concretely, `"invalid: channel not found"` for a bad `channel_ids`
  entry is the motivating case. `manager.py` sets the corresponding
  `*_error` field to the relay's literal message the first time this
  happens and never attempts that specific sub-step again.

This classification is one shared helper used identically by
`create_agent`'s initial attempt (steps 2-5 above) and by
`ensure_runtime_ready`'s retries — there is no separate "first try" logic;
`create_agent` simply makes the first call into the same per-step function
`ensure_runtime_ready` later calls again for anything still pending.

This distinction exists specifically because a bad channel UUID typo would
otherwise cause `ensure_runtime_ready` to hammer the relay with the exact
same doomed `join-channel` call on every dashboard refresh, forever,
while giving the user no way to discover why short of reading logs —
defeating the point of a feature about discoverability.

## Dashboard status column

The TUI dashboard's agent table gains one column, "Visibility", derived
purely from `visibility_state` (no new network calls — this is a local
read of already-stored state, refreshed whenever `ensure_runtime_ready`
runs as part of the existing dashboard-refresh cycle):

- `—` (em dash) if `visibility_managed` is `False` (old, unmanaged agent
  — deliberately not "n/a" or blank, to be visually distinct from the
  other states rather than looking like missing data).
- `synced` if every mandatory step succeeded (`profile_published`,
  `managed_agent_published`, `add_policy_published` all `True`) and every
  entry in `channels` is `"joined"`.
  `pending` if nothing has hit a permanent error yet but at least one
  mandatory step or channel join hasn't succeeded.
- `error: <first non-null *_error or channel_errors value, truncated to
  fit the column>` if any permanent failure is recorded — this is the
  only way a "channel not found" typo becomes visible to the user at all,
  per "Permanent vs. transient failures" above.

The CLI's `agent list` output gains the same three-state field as plain
text (not a new flag — always shown, matching how `agent list` already
shows systemd status unconditionally).

## Testing plan

- **Rust (`signer/`)**: unit tests per new builder function (tag shape,
  content shape) mirroring the existing `events.rs` test style
  (`add_member_with_role_sets_role_tag`) — one test per new event kind
  confirming exact tag/content shape against the mapping tables above,
  plus one test that `ComputeAuthTag`'s output round-trips through
  `buzz_sdk::nip_oa::verify_auth_tag` (borrowed directly from the SDK,
  cheap and catches any accidental misuse of the builder's arguments).
- **Python (`manager.py`)**: extend the existing `FakeCommandRunner`-style
  test doubles (see current `test_manager.py`) with fake responses for
  each new signer subcommand; test the classification logic
  (transient vs. permanent) with both a simulated connection failure and
  a simulated `"invalid: channel not found"` response; test that
  `ensure_runtime_ready` never touches an agent with
  `visibility_managed=False`; test the full create → update (add channel,
  remove channel, change display name) → delete lifecycle end-to-end
  against fakes, asserting the exact sequence and arguments of signer
  calls at each stage.
- **TUI (`tests/tui/test_agent_form.py`)**: new field presence/round-trip
  tests for `channel_ids` and `channel_add_policy` (same pattern as the
  existing `respond_to_allowlist` tests), plus a UUID-format-validation
  test (malformed entry rejected with a `notify(severity="error")` before
  any signer call — assert the fake manager's `create`/`update` was never
  invoked).
- **Dashboard**: test each of the four status-column states renders from
  a hand-constructed `AgentVisibilityState`, without needing a real agent
  lifecycle to reach each state.
- **No end-to-end test against a real Buzz relay** in the automated suite
  (matches the existing project convention — `ensure_runtime_ready`'s
  three existing concerns aren't relay-tested either); manual verification
  against a real community, as was done for the earlier `team_instructions`
  and bundled-personas features, is the acceptance gate before release.
