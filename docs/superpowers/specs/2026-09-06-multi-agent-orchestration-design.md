# Multi-agent, multi-device orchestration — design spec

Status: draft for review, not yet approved, not yet built.
Date: 2026-09-06
Scope: buzz-fleet (this repo) plus its Rust signer. No changes to the public
block/buzz repo are required or assumed.

## 1. Goal

Five machines (2–3 laptops, a VPS, a dedicated server) each run buzz-fleet
managed headless agents on one shared Buzz relay. The owner wants:

- A laptop agent implements something, hands it to a VPS agent for review, which
  either sends it back with findings or hands it to a dedicated-server agent to
  build, which then notifies the reviewer and/or the implementer.
- Both **predefined pipelines** (configured once, run by name) and **agentic
  delegation** (an agent decides on its own to hand work to another agent).
- The result must be *more productive* than plain chat between agents: handoffs
  must not get lost, stalls must surface, and the owner must be able to see
  what is in flight from any machine.

Decisions already made by the owner during brainstorming:

| Question | Decision |
|---|---|
| Authoring style | Both pipelines and ad-hoc delegation, on one mechanism. A pipeline is a **default the agent may deviate from**, not an enforced state machine. |
| Role-to-device binding | Pipeline steps name **agents** (by display name). The machine is wherever buzz-fleet runs that agent. No device concept is added anywhere. |
| Timeouts | Never wait forever. Nudge the assignee once, then **escalate to the owner** by @-mention. Optional per-step fallback agent. |
| Owner visibility | Everything happens **live in one shared channel** (free with Buzz). The owner is @-mentioned only on completion, failure, or escalation. |

## 2. Verified facts the design rests on

All verified against source on 2026-09-06 (paths relative to the block/buzz
checkout at `/home/dev/repos/buzz` unless noted).

1. **Wake-up is p-tag only.** buzz-acp in `SubscribeMode::Mentions` subscribes
   with `#p = [own pubkey]` (`crates/buzz-acp/src/relay.rs` `send_subscribe`).
   There is no thread-participant wake. A reply wakes the other agent only if
   the reply event carries `["p", <their pubkey>]`.
2. **buzz-acp never publishes the agent's reply.** The agent is instructed to
   run the `buzz messages send` CLI itself (`crates/buzz-acp/src/queue.rs`
   `append_reply_instruction`). `--reply-to` adds only `e` tags; a `p` tag is
   added only for `@Name` text resolved against channel members or an explicit
   `--mention` (`crates/buzz-cli/src/commands/messages.rs`).
3. **buzz-fleet agents cannot reply today.** The `buzz` CLI is one personality
   of the Sprig multicall binary; buzz-fleet installs Sprig only under the name
   `buzz-acp` (`src/buzz_fleet/buzz_acp.py`), creates no `buzz` link, and the
   unit template sets no PATH. `which buzz` is empty on this machine. Verified
   that a symlink named `buzz` pointing at the installed Sprig binary runs the
   full Buzz CLI and reads `BUZZ_PRIVATE_KEY` / `BUZZ_RELAY_URL` /
   `BUZZ_AUTH_TAG` from the environment. The agent subprocess inherits
   buzz-acp's environment (`crates/buzz-acp/src/acp.rs` `spawn` only adds
   variables, never clears), so those variables are already present.
4. **The relay keeps arbitrary tags on kind 9 and filters on any single-letter
   tag.** Tags are stored verbatim as JSONB and cannot be stripped (signed).
   REQ filters on `#t` work (post-filtered, not indexed). Multi-letter filter
   keys such as `#fleet` are silently ignored by the `nostr` crate's filter
   deserializer, so anything we need to *query by* must live in a
   single-letter tag. Custom regular/replaceable kinds (e.g. 30xxx) are
   rejected (`crates/buzz-relay/src/handlers/ingest.rs` `required_scope_for_kind`).
   Ephemeral kinds 20000–29999 are accepted but never stored.
5. **Blocking inside a tool call kills the turn.** The idle timer
   (`BUZZ_ACP_IDLE_TIMEOUT`, default 1500 s) resets only on agent stdout; a
   long blocking MCP or shell call produces none, so the turn is cancelled. A
   design that "waits for the reply inside the agent's turn" is therefore
   unsafe. This supersedes the `wait_for_reply` idea in buzz-deploy's
   2026-09-03 comms-MCP spec.
6. **One MCP slot.** buzz-acp supports exactly one MCP server
   (`BUZZ_ACP_MCP_COMMAND`, a bare command, no args). Consuming it for
   orchestration would block personas that need their own MCP server later.
   Pi's MCP support is still unverified.
7. **Sibling trust.** Agents sharing the same NIP-OA owner can trigger each
   other under the default `owner-only` respond policy. All fleet agents and the
   conductor below share the owner, so no policy changes are needed.
8. **buzz-workflow's `RequestApproval` is non-functional upstream (WF-08).**
   Not used.
9. **buzz-acp discovers channel memberships once, at startup.**
   `discover_channels` (`crates/buzz-acp/src/relay.rs`) runs once in
   `lib.rs`; every later subscribe call is a reconnect or rate-limit retry
   over that fixed set. An agent added to a channel after it started never
   receives events from it until its unit restarts. Consequence: channels must
   be long-lived and joined before the agent starts; per-run disposable
   channels are ruled out.
10. buzz-fleet's signer already links `buzz-ws-client` and `buzz-sdk`
   (`signer/Cargo.toml`), which provide `build_message(channel, content,
   thread_ref, mentions, ...)` and an authenticated WebSocket connection with
   `send_raw` / `next_event`, i.e. everything needed to publish tagged kind 9
   events and to subscribe.

## 3. Approaches considered

**A. MCP delegation server (buzz-deploy 2026-09-03 spec shape).** Tools
`post_message` / `wait_for_reply` / `read_channel` exposed via the single MCP
slot. Rejected: blocking waits die to the idle timer (fact 5), it uses the only
MCP slot (fact 6), and Pi support is unknown. Nothing it offers is unavailable
through a shell CLI, which every supported harness already has.

**B. Shell CLI + relay-as-bus + one always-on conductor. Recommended.**
Agents hand off and report through two small `buzz-fleet` subcommands that
publish ordinary kind 9 channel messages carrying fleet tags. The relay is the
only shared state; every machine reads it. One long-lived "conductor" process
on an always-on box watches those events, keeps deadlines, nudges, escalates,
and fills pipeline gaps. Works for all four harnesses (all have shell), keeps
the MCP slot free, needs no new network paths between machines, and survives
laptop sleep because state is on the relay and timers are on the VPS.

**C. buzz-workflow pipelines.** Blocked by WF-08 and requires an executor host
outside buzz-fleet's control. Rejected for now; nothing here prevents adopting
it later for the notify half if upstream fixes it.

## 4. Architecture

```
 laptop (Implementer)          VPS (Reviewer + Conductor)        dedicated (Builder)
 ┌────────────────────┐        ┌─────────────────────────┐       ┌───────────────────┐
 │ buzz-acp → harness │        │ buzz-acp → harness      │       │ buzz-acp → harness│
 │   runs:            │        │   runs: buzz-fleet task │       │   runs: ...       │
 │   buzz-fleet task  │        │                         │       │                   │
 │   delegate/report  │        │ buzz-fleet conductor run│       │                   │
 └─────────┬──────────┘        └───────┬─────────▲───────┘       └─────────┬─────────┘
           │ kind 9 + fleet tags        │         │ kind 9 (nudge/escalate/  │
           ▼                            ▼         │  auto-delegate)          ▼
 ═══════════════════════════ Buzz relay, one shared channel ══════════════════════════
           ▲                                                                ▲
           │ buzz-fleet runs / tasks (read-only view, any machine)          │
      owner's laptop                                            Buzz Desktop / mobile
```

Three layers, in build order:

0. **Reply path fix** (prerequisite, tiny): put `buzz` on the unit PATH and
   teach agents the fleet commands through team instructions.
1. **Wire protocol + CLI + read-only views**: everything an agent or the owner
   needs to delegate, report, and see state. Works without a conductor, just
   with no timers.
2. **Conductor**: deadlines, nudges, escalation, pipeline auto-advance, run
   completion notices.
3. **Pipelines**: a YAML file resolved into a self-contained run record.

Each layer is independently useful and testable.

## 5. Components

### 5.0 Prerequisite: agents can reply

- `buzz_acp.ensure_buzz_acp_installed` also creates
  `~/.local/share/buzz-fleet/bin/buzz` as a symlink to the installed Sprig
  binary (idempotent; recreated if missing or dangling).
- `systemd.TEMPLATE_UNIT` gains
  `Environment=PATH=%h/.local/share/buzz-fleet/bin:/usr/local/bin:/usr/bin:/bin`.
  Changing the template already triggers a rewrite + `daemon-reload` through
  `ensure_template_unit_installed`; `ensure_runtime_ready` restarts units whose
  template changed (same mechanism as the auth-tag backfill).
- The `buzz-fleet` binary itself (installed at the same bin dir by `get.sh`)
  is thereby also on the agents' PATH, which is what makes `buzz-fleet task
  ...` callable from inside a harness.
- **Fleet coordination instructions.** buzz-fleet appends a managed block to
  every agent's `BUZZ_ACP_TEAM_INSTRUCTIONS` (bundled template
  `src/buzz_fleet/orchestration/instructions.md`, delimited by
  `<!-- buzz-fleet:coordination v1 -->` markers so it can be updated in place
  without touching the operator's own text). It tells the agent, in under 40
  lines: what a delegation message looks like, to run `buzz-fleet task report`
  when it finishes delegated work, to use `buzz-fleet task delegate` instead
  of a bare @-mention when it wants another agent to do something, that a
  pipeline default is advice it may override if it says why, and to keep
  channel replies short because the report carries the summary.
  `ensure_runtime_ready` refreshes the block when the template hash changes.

This layer alone makes today's plain mention chains work reliably for
reply-and-wake, since `buzz-fleet task report` always p-tags the delegator.

### 5.1 Wire protocol (kind 9 messages with fleet tags)

Every orchestration event is an ordinary channel message so humans see it in
Desktop/mobile and the relay stores it. Structure lives in tags; content stays
readable.

Common tags on every fleet event:

| Tag | Purpose |
|---|---|
| `["h", <channel>]` | channel scope (required by relay) |
| `["t", "fleet"]` | the one queryable marker; all fleet reads use `#t=["fleet"]` + `kinds=[9]` + `#h` |
| `["t", "fleet:task:<task_id>"]` | per-task lookup |
| `["t", "fleet:run:<run_id>"]` | per-run lookup (only for events belonging to a run) |
| `["fleet", <json>]` | machine-readable payload (not filterable, which is fine; we always fetch by `#t` first) |
| `["p", ...]` | recipients who must wake |
| `["e", ...]` | NIP-10 thread markers; see "Threading" below |

Payload types (`"type"` field of the `fleet` tag JSON):

- `delegate` — `{type, task, run?, step?, from, to, deadline, brief_sha}`.
  Content: `@<To> ▶ task <id>` header, the brief, the deadline, and the exact
  report command to run. Posted by the delegator (agent, owner, or conductor).
- `report` — `{type, task, status: done|blocked|failed, next?: task_id|"none"}`.
  Content: `@<From> ✅|⛔|❌ task <id>: <summary>`. Posted by the assignee,
  p-tags the delegator, replies in the task thread.
- `run` — `{type, run, pipeline, steps:[{role, agent, pubkey, timeout, on_fail, fallback?}], brief}`.
  Posted by whoever starts the run (usually the owner from any machine). Self-
  contained: the conductor needs no local copy of the pipeline YAML.
- `nudge`, `escalate`, `run-done`, `run-failed`, `cancel` — posted by the
  conductor (or the owner for `cancel`). Human-readable content, p-tags the
  relevant party (assignee for nudge, owner for escalate/run-done/run-failed).

Identifiers: `task_id` is 6 random hex chars; `run_id` is `<pipeline>-<4 hex>`.

**Threading.** One thread per run: the `run` event is the thread root and
every `delegate`, `report`, `nudge`, and `escalate` for that run is a reply in
it. An ad-hoc `delegate` replies in the thread the delegator is currently in
when `--thread <root>` is given (the root id appears on the `Thread root:`
line of the agent's prompt), otherwise it starts a new thread; its `report`
always replies in the same thread. Under the thread session policy (5.8) this
means an agent woken later in the same run, for rework or a final notice,
wakes in the session that already holds its memory of that run, and a
delegator gets the report back in the session it delegated from. Separate
threads per task would start every wake-up cold. Waking is still driven by
`p` tags (fact 1); threads only carry context.

Why not a separate state store: the relay already is one, it is reachable from
all five machines, it is durable, and it is what the owner is already looking
at. SQLite appears only as a private cache inside the conductor.

### 5.2 Agent- and owner-facing CLI (Python, in `buzz-fleet`)

New module `src/buzz_fleet/orchestration/` with a Typer group `task`, plus
`runs` and `run`.

Identity: `buzz-fleet task ...` reads `BUZZ_PRIVATE_KEY`, `BUZZ_RELAY_URL`,
`BUZZ_AUTH_TAG` from the environment (inherited from the unit through
buzz-acp). When run by the owner on a machine with local buzz-fleet state and
no such env, it uses the community's admin key from local state. Name
resolution goes through the signer (5.3).

```
buzz-fleet task delegate --to <Name|pubkey> --brief <text|-> [--wait 45m]
                         [--run <run_id>] [--thread <root_event_id>] [--channel <uuid>]
    → publishes `delegate`; prints task id and the event id.
    With --run, looks up the run record and appends the pipeline default for the
    recipient's step ("Default when done: delegate to @Builder (step 3)").
    --channel defaults to the run's channel when --run is given, else to
    BUZZ_FLEET_CHANNEL (see 5.9); an explicit value overrides both.
    Thread: with --run, the run's root event; with --thread, that root;
    otherwise a new thread rooted at this delegate event.

buzz-fleet task report --task <id> --status done|blocked|failed --summary <text|->
                       [--next <task_id>|none]
    → publishes `report` p-tagging the delegator, in the task thread.
    --next records that the reporter already delegated onward (so the conductor
    does not auto-advance); `none` explicitly declines the pipeline default.

buzz-fleet task show <id>      → thread + status, from the relay.
buzz-fleet tasks [--open|--stuck|--mine]   → table, from the relay.
buzz-fleet runs [--open]       → table of runs with current step and age.
buzz-fleet run <pipeline> --brief <text|-> [--channel <uuid>]
    → resolves the YAML (5.5), publishes `run`, then `delegate` for step 1.
buzz-fleet run cancel <run_id> → publishes `cancel`.
buzz-fleet run purge <run_id>  → owner only; deletes the run thread (see 5.8 lifecycle).
```

The read commands are pure: fetch fleet events (signer `query`, all
accessible channels), feed them to the reducer, print. The same reducer runs in the
conductor, so what the owner sees on a laptop is exactly the conductor's view.

`orchestration/reducer.py`: a pure function `reduce(events) -> State` with
`State = {runs: {run_id: Run}, tasks: {task_id: Task}}`. Task status is
derived: `open` (delegate seen, no report), `nudged`, `escalated`, `done`,
`blocked`, `failed`, `cancelled`, plus `answered-unstructured` when the
assignee replied in the task thread p-tagging the delegator without a
`report` payload. Runs track `current_step` and the ordered list of tasks.

### 5.3 Signer additions (Rust, `signer/`)

Seven subcommands, all following the existing `run_publish` / auth-tag
conventions, JSON on stdout (`create-channel` and `find-channel` are described
in 5.9, `delete-message` in 5.8):

- `post-message --relay --nsec --auth-tag? --channel --content --mention <hex>...
  --reply-to <id>? --root <id>? --tag <name>=<value>...` — wraps
  `buzz_sdk::build_message` and appends extra tags (`t` and `fleet`). Returns
  the event id.
- `query --relay --nsec --auth-tag? --filter <json>` — one-shot REQ, prints
  each event as one JSON line, exits at EOSE.
- `subscribe --relay --nsec --auth-tag? --filter <json>` — like `query` but
  stays open after EOSE (prints a `{"eose":true}` line first) and streams live
  events until stdin closes or the socket drops (non-zero exit so the
  supervisor reconnects with an updated `since`).
- `channel-members --relay --nsec --auth-tag? --channel` — kind 39002
  membership + kind 0 profiles → `[{pubkey, display_name}]`, the same
  resolution `buzz messages send` uses for `@Name`.

Everything Nostr-specific stays in Rust where the working NIP-42/NIP-OA code
already lives; Python never signs or speaks WebSocket.

### 5.4 Conductor (Python, long-lived, one instance per channel)

`buzz-fleet conductor install` on the chosen always-on box:

- creates a keypair, computes its NIP-OA auth tag, publishes a kind 0 profile
  ("Fleet Conductor") and joins the fleet channel, reusing the `AgentManager`
  visibility helpers; records it in local state as a `Conductor` model (not an
  `Agent`: it has no harness);
- writes `~/.config/buzz-fleet/conductor.env` and installs a
  `buzz-fleet-conductor.service` `--user` unit whose `ExecStart` is
  `buzz-fleet conductor run` (`Restart=always`).

`buzz-fleet conductor run`:

1. Replays: `query` fleet events across all its channels since (last checkpoint − 1 h),
   reduces into state, persists the checkpoint in
   `~/.local/share/buzz-fleet/conductor.sqlite` (cache only; deleting
   it is safe).
2. Streams: `subscribe` from the checkpoint; each event goes through the
   reducer. On subprocess exit, backs off and reconnects with `since = last
   event time`. Duplicate events are idempotent by event id.
3. Ticks every 30 s and applies the rules below. Every action it takes is itself
   a fleet event on the relay, so it is visible, replayable, and never taken
   twice (the reducer sees the conductor's own `nudge`/`escalate` events).

Rules:

| Condition | Action |
|---|---|
| Task open past `deadline` and no `nudge` yet | post `nudge` (p-tags assignee): "task <id> is past its deadline; report with …" |
| Task still open `grace` (default 15 min) after the nudge | if the run step has `fallback`: post a new `delegate` to the fallback agent and `cancel` the old task; else post `escalate` (p-tags owner) |
| Task `answered-unstructured` for 5 min | post one `nudge`: "please close task <id> with `buzz-fleet task report`" (never repeated) |
| Report `done` on run step k, no `--next`, and no `delegate` with `--run` from the reporter within 2 min | conductor posts `delegate` for step k+1 (brief = run brief + previous step summary), assigning the pipeline agent |
| Report `done` with `--next <task>` | record that task as step k+1; no action |
| Report `done` with `--next none`, or a `delegate --run` to an agent not in the pipeline | record as a deviation; the run continues from that task; when *that* task is done and has no onward delegation, the pipeline default for the *original* step k+1 applies |
| Report `blocked` or `failed` on step k (k > 1) | open a rework task for the delegator (the step k−1 agent) with the failure summary and the step's `on_fail` timeout; the reporter's p-tag already woke them. When the rework task is reported `done`, the conductor re-delegates step k unless the delegator already did |
| Report `blocked`/`failed` on step 1, or a rework task itself reported `failed` | post `run-failed` (p-tags owner) |
| Last step reported `done` | post `run-done` (p-tags owner and the step-1 agent) with all step summaries |
| `cancel` seen | mark run and open tasks cancelled; no further actions for them |

Ad-hoc tasks (no run) get only the deadline rules. Nothing else is
auto-advanced, because there is no pipeline to advance.

The conductor never edits or deletes anything and never posts as another
identity. If it is down, agents keep working through plain mentions; only
timers and auto-advance pause, and they catch up on replay.

### 5.5 Pipeline configuration

`~/.config/buzz-fleet/pipelines/<name>.yaml` on whichever machine the owner
runs `buzz-fleet run` from (a git repo synced across laptops is fine; the run
record on the relay is self-contained, so nothing else needs the file).

```yaml
name: impl-review-build
channel: 6f1c…            # optional; defaults to the fleet channel (5.9)
steps:
  - role: implement
    agent: Implementer     # display name resolved via channel-members at run time
    timeout: 90m
  - role: review
    agent: Reviewer
    timeout: 45m
    on_fail: back          # default; the only value in v1 — send findings to the previous step
    fallback: Reviewer-2   # optional
  - role: build
    agent: Builder
    timeout: 60m
notify_on_done: [owner, implement]   # who gets p-tagged in run-done
```

Resolution fails fast on an unknown or ambiguous name. Agents are resolved once
at run start; the run record stores pubkeys.

### 5.6 What the owner sees

- Live: the channel in Buzz Desktop or mobile. Each run is one thread whose
  root is the run event; delegations, reports, nudges, and the final notice
  are its replies. A finished run is a collapsed thread, so nothing needs
  archiving.
- On demand from any machine: `buzz-fleet runs`, `buzz-fleet tasks --stuck`.
- Pushed: an @-mention only on `escalate`, `run-done`, `run-failed`.

### 5.7 Deployment

- Layers 0–1 ship in the normal `buzz-fleet` release to all five machines
  (`get.sh` already installs `buzz-fleet` and the signer; nothing new to
  install).
- The conductor is installed on exactly one always-on box (VPS or dedicated
  server) with one command. Running two conductors for the same channel would
  double-post; documented, and `conductor install` refuses if a member of the
  fleet channel already has the kind 0 display name `Fleet Conductor`.

### 5.8 Sessions and parallelism (shared task sessions, parallel use)

buzz-acp keys every provider session, queue partition, and in-flight turn on
a `SessionScope` (`crates/buzz-acp/src/scope.rs`). Two policies exist:
`channel` (default: the whole channel is one session) and `thread` (each
canonical thread is an isolated session; DMs are their own conversation
session). `BUZZ_ACP_AGENTS` sets how many agent subprocesses serve those
sessions concurrently.

Because every run is one thread and every ad-hoc delegation lives in a
thread (5.1), the `thread` policy makes a run a shared, memory-keeping session
for the agents working it, while the owner can DM the same agent or open
another thread at the same time with separate context.
With parallelism ≥ 2 those sessions also run concurrently instead of queueing.

Design decisions:

- buzz-fleet writes `BUZZ_ACP_SESSION_POLICY=thread` for every managed agent by
  default. `Agent` gains `session_policy: Literal["thread", "channel"] | None`
  (None = thread) exposed in the CLI and TUI form, so an agent that should
  keep one channel-wide memory can opt out.
- Recommended `parallelism`: 2 on laptops, 3–4 on the VPS and dedicated server.
  The README documents the trade-off (each slot is a live harness process with
  its own API usage).
- The fleet coordination instructions (5.0) tell agents that each thread is
  its own session, so a report must carry the full summary rather than assume
  the recipient shares context.

Lifecycle, verified against source: nothing disposes of a thread or a session
when a run ends.

- Relay side: a thread is only messages linked by reply tags. There is no
  close or archive event for threads (`buzz-core/src/kind.rs` has a
  relay-synthesized thread summary overlay, nothing stored). Run threads are
  therefore kept forever and serve as the audit log; the conductor's
  `run-done` / `run-failed` / `cancel` event is what marks a run closed in the
  reducer. Deletion is a deliberate owner action, never automatic and never a
  per-run question: the `run-done` notice ends with the exact command
  `buzz-fleet run purge <run_id>`, which deletes every message in the run
  thread as the owner (Buzz-native kind 9005 delete with moderation metadata,
  `buzz_sdk::build_delete_message_with_options`, via a new signer
  `delete-message` subcommand) and posts nothing afterwards. The reducer
  drops a run whose root has been deleted. A retention policy in pipeline
  files (auto-purge closed runs after N days) is deferred until real usage
  shows it is wanted.
- Agent side: buzz-acp keeps a `SessionScope → session_id` map per agent
  process (`crates/buzz-acp/src/pool.rs` `SessionState`). Entries leave it
  only on rotation (max tokens, or `BUZZ_ACP_MAX_TURNS_PER_SESSION` reached)
  or process restart; there is no idle eviction. buzz-fleet does not set the
  cap today. Decision: `Agent` gains `max_turns_per_session: int | None`
  (default 40, written as `BUZZ_ACP_MAX_TURNS_PER_SESSION`) so dormant run
  sessions rotate out instead of accumulating in the harness process. No
  scheduled restarts in v1; if growth still matters, a nightly unit restart
  is a one-line follow-up.
- The thread session policy is marked in source as shipped dark for
  canarying with a rollback to `channel`. It works in the current build but is
  the newer path; the per-agent opt-out above is the rollback.

### 5.9 The fleet channel

One dedicated channel per community carries all orchestration traffic by
default. Rationale: the relay rejects a mention of a non-member, so a channel
every managed agent has joined is what makes "delegate to any fleet agent"
always valid; it keeps nudges and escalations out of human project channels;
and it lets buzz-fleet hand agents their default channel without any prompt
parsing.

Lifecycle (create once, discover everywhere, never auto-create):

- `buzz-fleet fleet init [--channel <uuid>]`, run once on any machine. Without
  `--channel` it creates a channel named `fleet` (owner key, via a new signer
  `create-channel` subcommand wrapping `buzz_sdk::build_create_channel`). With
  `--channel` it adopts an existing one. Either way it saves
  `Community.fleet_channel_id` locally. It refuses to create if a channel named
  `fleet` already exists on the relay, to prevent duplicates across machines.
- Every other machine discovers it: `ensure_runtime_ready` (already called on
  every list/create/update/dashboard refresh) looks up the channel named
  `fleet` through a new signer `find-channel --name` when
  `fleet_channel_id` is unset, and persists it. No manual step per machine.
- Every managed agent auto-joins it: `_sync_visibility` treats the fleet
  channel as an implicit member of `channel_ids` (shown in the TUI as
  "fleet (auto)"). Because buzz-acp discovers memberships only at startup
  (fact 9), `ensure_runtime_ready` restarts a running unit once after joining
  it to a channel it was not a member of at start. The same applies to
  joining an agent to a project channel: one restart per channel, never per
  run. The conductor joins on install. The owner is a member
  because the owner created it, or must be for an adopted channel (validated
  at init).
- buzz-fleet writes `BUZZ_FLEET_CHANNEL=<uuid>` into every agent env file.
  Because the harness inherits buzz-acp's environment (fact 3),
  `buzz-fleet task delegate` defaults `--channel` to it. Precedence: explicit
  `--channel` > the run's channel when `--run` is given > `BUZZ_FLEET_CHANNEL`.
  This resolves risk 10.3.

Scope of the conductor: it subscribes without an `h` filter; the relay scopes
such a REQ to every channel the subscriber can access
(`crates/buzz-relay/src/handlers/req.rs`, "community-wide channel scope"), so
one conductor covers the fleet channel plus any project channel it is joined
to. `buzz-fleet run --channel <project channel>` validates at start that the
conductor and every step agent are members of that channel.

`conductor install` therefore no longer takes `--channel`; the unit is
`buzz-fleet-conductor.service` (one per machine, one per community). To add a
project channel to its scope, join the conductor to it with
`buzz-fleet conductor join <uuid>`.

## 6. Walkthroughs

**Pipeline run.** Owner on a laptop: `buzz-fleet run impl-review-build --brief
"Add CSV export to reports"`. buzz-fleet resolves three names, publishes
`run`, publishes `delegate` (task a1b2c3, step 1) mentioning Implementer.
Implementer wakes, works, runs `buzz-fleet task delegate --to Reviewer --run
impl-review-build-9f3e --brief "…"` (the CLI appends "default when done:
delegate to @Builder"), then `buzz-fleet task report --task a1b2c3 --status
done --summary "…" --next d4e5f6`. Reviewer wakes, finds a bug, runs `task
report --task d4e5f6 --status failed --summary "…"`. That p-tags Implementer,
who wakes in the same run thread, and therefore in the session that built the
feature; the conductor opens a rework task with the failure text. Implementer
fixes, reports `done`; the conductor re-delegates review. Reviewer passes,
reports `done` with no `--next`; two minutes later the conductor delegates
step 3 to Builder. Builder reports `done`; conductor posts `run-done`
mentioning the owner and Implementer.

**Agentic delegation.** Any agent, mid-task, runs `buzz-fleet task delegate
--to Builder --brief "…" --wait 30m` because it decided it needs a build. No
run, no pipeline. Same deadline/nudge/escalate handling. The reply wakes it
because `report` p-tags it.

**Deviation.** Reviewer, instead of returning to Implementer, delegates to a
different agent with `--run`. Recorded as a deviation; the run continues from
there; the pipeline default resumes when that task completes without an onward
hop.

**Stall.** Builder's machine is off. Deadline passes: nudge. Grace passes: no
fallback configured, so `escalate` mentions the owner with run id, task id,
and age. The owner starts the machine or cancels the run.

## 7. Error handling and edge cases

- **Relay down or auth failure** in `task delegate`/`report`: the CLI exits
  non-zero with a one-line JSON error so the agent can retry or say so in
  chat; it never fakes success.
- **Name resolution ambiguity** (two agents named "Reviewer"): fail with both
  pubkeys listed; the caller may pass a pubkey instead.
- **Reporter is not the assignee**: the reducer ignores `report` events whose
  author is neither the assignee nor the delegator (delegator may cancel via
  `cancel`, not report). Owner can always cancel.
- **Duplicate reports**: first wins; later ones are recorded as notes.
- **Conductor restart mid-grace**: state replays from the relay; timers resume
  from event timestamps, not wall-clock since boot.
- **Conductor's own events failing to publish**: logged, retried next tick;
  since actions are derived from state, nothing is lost.
- **Clock skew**: deadlines are absolute unix seconds computed by the
  delegator; the relay rejects events skewed more than ±900 s, so skew is
  bounded.
- **Codex sandbox**: buzz-acp already injects `CODEX_CONFIG` with network
  access enabled for its child; verify at implementation that `buzz-fleet task`
  can reach the relay from inside a Codex turn on Linux. If not, that harness
  gets an env-file override documented in README.
- **PyInstaller start-up latency** (~1 s per `buzz-fleet task` call): acceptable
  for a handful of calls per turn; measured before release.

## 8. Testing

- **Reducer**: table-driven unit tests over synthetic event lists covering
  every rule in 5.4, deviations, duplicates, out-of-order arrival, and cancel.
- **Conductor tick**: tests with a fake clock and a recording publisher;
  assert exactly which fleet events are emitted for a given state and time.
- **CLI**: tests using the existing `CommandRunner` mock to assert the signer
  is invoked with the right tags, mentions, thread refs, and identity source
  (env vs. local admin key).
- **Signer**: Rust unit tests for `post-message` tag construction and the
  filter JSON passed to `query`/`subscribe`, mirroring existing
  `agent_events.rs` tests.
- **Instructions block**: idempotent insert/update/no-op tests on team
  instructions.
- **Live smoke script** (documented, manual): three agents on one box in one
  channel, run a 3-step pipeline end to end, then kill the builder unit and
  observe nudge → escalate. This is the test that would have caught fact 3.

## 9. Non-goals (v1)

- No device/host awareness. Steps name agents.
- No parallel steps, branching, or conditions in pipelines; linear only.
  Agents can still fan out by delegating twice; each task is tracked.
- No MCP server. Revisit only if a harness turns out to lack shell access.
- No use of buzz-workflow until WF-08 is fixed upstream.
- No cross-channel runs; one channel per run (the fleet channel by default).
- No per-run or disposable channels: buzz-acp would need a restart of every
  participant per run (fact 9). Runs are threads instead.
- No retries of the *same* agent beyond one nudge; escalation is the retry.
- No web dashboard; the channel plus two CLI tables are the UI.

## 10. Risks to verify during implementation

1. `#t` filter combined with `#h` and `kinds` on the live relay returns the
   expected events with NIP-OA-delegated auth (fact 4 was verified in code,
   not against `wss://buzz.eltahir.me`).
2. Codex network access for shell-invoked CLIs (7).
3. Resolved by 5.9: agents get `BUZZ_FLEET_CHANNEL` from their environment.
4. A REQ with `kinds=[9]`, `#t=["fleet"]` and no `#h` returns events from all
   channels the conductor is a member of under NIP-OA-delegated auth. The
   scoping was read in the search path; confirm the plain REQ path behaves
   the same on the live relay, else fall back to one `#h` per joined channel.
