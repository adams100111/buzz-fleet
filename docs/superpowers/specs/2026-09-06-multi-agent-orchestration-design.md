# Multi-agent, multi-device orchestration — design spec

Status: revision 3, approved design, not yet built. Revision 2 adopted an
independent review (`2026-09-06-multi-agent-orchestration-review.md`,
`2026-09-06-multi-agent-orchestration-alternatives.md`). Revision 3 closes
every deferral after a grilling session with the owner: this is the complete
feature set, not a minimum. Section 13 lists the decisions from that session.
Vocabulary: `CONTEXT.md` at the repo root. Decision records: `docs/adr/`.
Date: 2026-09-06
Scope: buzz-fleet (this repo), its Rust signer, and one Compose addition in
buzz-deploy (ntfy). No changes to the public block/buzz repo are required.

## 1. Goal

Five machines (2–3 laptops, a VPS, a dedicated server) each run buzz-fleet
managed headless agents on one shared Buzz relay. The owner wants:

- A laptop agent implements something, hands it to a VPS agent for review, which
  either sends it back with findings or hands it to a dedicated-server agent to
  build, which then notifies the reviewer and/or the implementer.
- Both **predefined pipelines** and **agentic delegation**, on one mechanism.
- The result must be *more productive* than plain chat between agents:
  handoffs must not get lost, both sides must work on the same code, stalls
  must surface, the owner must be reachable and able to act from a phone, and
  the owner must be able to see what is in flight, and what it cost, from any
  machine.

Owner decisions (brainstorming and grilling):

| Question | Decision |
|---|---|
| Authoring style | Pipelines and ad-hoc delegation on one mechanism. A pipeline is a **default the agent may deviate from**. Software enforces attempt ownership, completion, duplicates, and limits. |
| Role-to-device binding | Steps name **agents** by display name, resolved to pubkeys at run start. No device concept; hostnames are shown for information only. |
| Timeouts | Never wait forever. Delivery recovery with backoff, one lateness nudge, then fallback or escalation to the owner. |
| Owner visibility and control | Live in one shared channel; the owner is notified outside Buzz too, and can act from a phone with owner commands in the run thread. |
| Channels | One long-lived `fleet` channel per community; project channels as an override. Runs are threads. |
| History | Run threads are the record. Retention is configurable; default keep forever. |
| Completeness | No "v1" deferrals. Failover, purge, recycling, metrics, budgets, notifications, parallel steps, and a TUI screen are all in scope. |

## 2. Verified facts the design rests on

Verified against source on 2026-09-06, local Buzz checkout `7a9a523`. Source
inspection does not prove what each installed binary does; section 11 lists
the live checks.

1. **Wake-up is p-tag only.** buzz-acp in `SubscribeMode::Mentions` subscribes
   with `#p = [own pubkey]` (`crates/buzz-acp/src/relay.rs` `send_subscribe`).
2. **buzz-acp never publishes the agent's reply.** The agent runs the `buzz
   messages send` CLI itself (`queue.rs` `append_reply_instruction`);
   `--reply-to` adds only `e` tags; `p` tags come from `@Name` or `--mention`.
   That membership check is client-side; the relay's ingest path does not
   validate mention membership.
3. **buzz-fleet agents cannot reply today.** The `buzz` CLI is one name of the
   Sprig multicall binary; buzz-fleet installs it only as `buzz-acp` and the
   unit sets no PATH. A symlink named `buzz` runs the full CLI and reads
   `BUZZ_PRIVATE_KEY`, `BUZZ_RELAY_URL`, `BUZZ_AUTH_TAG` from the environment,
   which the harness inherits (`acp.rs` `spawn` adds env, never clears it).
4. **Team instructions are truncated today.** The live agent received 47 of
   3,625 bytes of `BUZZ_ACP_TEAM_INSTRUCTIONS`: systemd stops an unquoted
   `EnvironmentFile` value at the first newline. Quoted values may span lines.
5. **Relay storage and retrieval.** Kind 9 keeps arbitrary tags verbatim.
   Historical REQs are clamped to 1,000 candidate rows
   (`buzz-db/src/store/event.rs` `DEFAULT_MAX_PAGE_LIMIT`); only `kinds`,
   `authors`, `ids`, `since`, `until`, `limit`, `#h`, a single `#p`, `#d` on
   NIP-33 kinds, and any `#e` are pushed into SQL before the clamp
   (`req.rs` `filter_to_query_params`). Everything else, including `#t`, is
   post-filtered. Multi-letter filter keys are silently dropped. Custom stored
   kinds are rejected; ephemeral kinds pass but are never stored.
6. **A stopped agent misses what arrived while it was down.** The first REQ
   starts at a startup watermark (`lib.rs` `startup_watermark_with_floor`).
   buzz-acp's heartbeat mode (`BUZZ_ACP_HEARTBEAT_INTERVAL`, default 0)
   runs `buzz feed get --types mentions` and `needs_action` and acts on them.
7. **Blocking inside a tool call kills the turn.** The idle timer
   (`BUZZ_ACP_IDLE_TIMEOUT`, default 1500 s) resets only on agent stdout.
8. **One MCP slot**, stdio only: the ACP `session/new` struct buzz-acp fills
   has command, args, env and no URL (`acp.rs` `McpServer`).
9. **Channels joined after startup are picked up live** via membership
   notifications (`lib.rs` `KIND_MEMBER_ADDED_NOTIFICATION` handling).
10. **Sessions.** `SessionScope` per thread under the `thread` policy.
    Sessions leave the per-process map only on rotation by that session's own
    turns or on process restart; a dormant session is never rotated.
11. **In-flight mentions.** Default `BUZZ_ACP_MULTIPLE_EVENT_HANDLING=steer`:
    a new mention is injected into the running session where the harness
    supports native steering, else the turn is cancelled and re-prompted with
    both messages. An owner-authored message whose content is exactly
    `!cancel` and mentions the agent hard-stops its turn; `!shutdown` and
    `!rotate` are the other reserved owner control words (`lib.rs`
    `is_owner_control_command`).
12. **Turn metrics.** buzz-acp publishes kind 44200 per turn: input, output,
    total tokens, `costUsd` when the provider reports it, stop reason, session
    and turn ids, no duration. NIP-44-encrypted to the owner and readable only
    with the owner's key (`P_GATED_KINDS`). Every buzz-fleet machine stores
    the owner's admin key in its community file.
13. **Push notifications cannot be self-hosted.** `buzz-push-gateway` is iOS
    and APNs only, hard-pins its delivery host to `push.buzz.xyz`
    (`crates/buzz-push-gateway/src/config.rs`), compiles in one App Attest
    profile, and the mobile app bakes the gateway URL at build time. No other
    outbound channel (email, ntfy, Telegram) exists in Buzz; buzz-workflow has
    a webhook action, gated by the non-functional WF-08 path.
14. **Sibling trust.** Agents sharing the same NIP-OA owner may trigger each
    other under `owner-only`.
15. The signer links `buzz-ws-client` and `buzz-sdk`: `build_message`,
    `build_create_channel`, `build_update_channel`, `build_delete_message`,
    authenticated connections, `send_raw`, `next_event`.

## 3. Approaches considered

**A. MCP delegation server.** Rejected narrowly: a *blocking* wait dies to the
idle timer (fact 7); the single stdio slot (fact 8) stays free for personas;
Pi's support is unverified. MCP's asynchronous Tasks extension would work; the
shell CLI is simply the one interface all four harnesses share. Any tool
surface runs locally under each agent, inheriting its identity; only the
conductor is central (ADR 0001).

**B. Shell CLI + relay-as-ledger + designated conductor with standby. Chosen.**

**C. Temporal behind Buzz.** The upgrade path if the conductor's recovery
rules outgrow one process. The boundary in 5.4 (pure reducer + actions,
adapters for relay and CLI) is what a Temporal workflow would replace.
Tripwire: if acceptance gates 2 and 3 (section 8) still fail after two
focused attempts, switch.

**D. buzz-workflow.** Blocked by WF-08.

## 4. Architecture

```
 laptop (Implementer)        VPS (Reviewer, Conductor, relay, ntfy)   dedicated (Builder, standby)
 ┌────────────────────┐      ┌────────────────────────────────┐       ┌──────────────────────┐
 │ buzz-acp → harness │      │ buzz-acp → harness             │       │ buzz-acp → harness   │
 │  runs buzz-fleet   │      │ buzz-fleet conductor run       │       │ buzz-fleet conductor │
 │  task delegate/ack │      │   reducer · outbox · timers    │       │   run --standby      │
 │  /report           │      │   notifier → ntfy/Telegram/... │       │                      │
 │ recycle timer      │      │ recycle timer                  │       │ recycle timer        │
 └─────────┬──────────┘      └──────────┬──────────▲──────────┘       └──────────┬───────────┘
           │ kind 9, p-tags recipient    │          │                               │
           │ AND the retrieval key       ▼          │ heartbeats, actions           ▼
 ════════════════════════ Buzz relay, fleet channel + project channels ═════════════════════════
           ▲                                                                       ▲
           │ buzz-fleet runs / tasks / TUI (complete paged reads)                  │
      owner's laptop                                                 Desktop / mobile + !fleet cmds
```

Build order, each layer independently useful and testable:

0. Prerequisites: PATH, quoted env, instructions, workspaces, settings,
   naming, versions, self-update.
1. Recoverable task lifecycle: fleet record, protocol, CLI, complete reads,
   views.
2. Conductor: outbox, timers, delivery recovery, escalation, notifier,
   heartbeat, standby failover, metrics, budgets, recycling.
3. Pipelines: linear defaults, parallel groups, human steps, bounded rework,
   deviations, retention and purge, TUI screen.

## 5. Components

### 5.0 Prerequisites

- **`buzz` on PATH.** `ensure_buzz_acp_installed` links
  `~/.local/share/buzz-fleet/bin/buzz` to the installed binary; the unit
  template gains `Environment=PATH=<that dir>:/usr/local/bin:/usr/bin:/bin`.
  `ensure_template_unit_installed` returns whether the file changed and
  `ensure_runtime_ready` restarts units when it did.
- **Quoted env values.** `write_agent_files` double-quotes values containing a
  newline, escaping `\` and `"`. Verified live from the process environment.
- **Workspaces.** Each unit gets `WorkingDirectory=~/.local/share/buzz-fleet/work/<agent>/`.
  Convention, taught by the instructions block: one shared clone per
  repository under it, one `git worktree` per run named by run id. Private
  repositories authenticate with per-machine SSH keys; `fleet init` and
  `agent create` print the prerequisite and `fleet status` reports whether
  `git ls-remote` succeeds for each repository named in open runs
  (assumption to confirm at setup: the owner has not stated the auth method).
- **Coordination instructions.** A managed block appended to every agent's
  team instructions, delimited by `<!-- buzz-fleet:coordination v1 -->` /
  `<!-- /buzz-fleet:coordination -->`, refreshed when the version changes. It
  covers delegate, ack, report, the artifact rule (push first, hand off an
  exact commit, name the commit you reviewed), the worktree rule, that the
  recipient shares no session, that a pipeline default is advice to decline
  with a reason, and that a cancelled task is abandoned immediately.
- **Agent settings.** `Agent` gains `session_policy` (`thread` default,
  `channel` opt-out), `max_turns_per_session` (default 40, rotates active
  sessions only), `heartbeat_interval_seconds` (default 900).
- **Unique names.** `agent create` queries the fleet channel and refuses a
  display name already in use unless `--force`; `fleet status` lists
  duplicates.
- **Hostname.** The owner-signed managed-agent record buzz-fleet already
  publishes gains `host`, shown in views and notices; never used for routing.
- **Versions and self-update.** `fleet init` records tested versions in the
  fleet record. `fleet status` warns on drift. `buzz-fleet self-update`
  fetches the release the record names, verifies checksums, reinstalls, and
  restarts agents; opt-in, never automatic.

### 5.1 Wire protocol

Every orchestration event is a kind 9 channel message. Structure lives in
tags and a JSON payload; content stays readable.

**Tags on every fleet event:**

| Tag | Purpose |
|---|---|
| `["h", <channel>]` | channel scope |
| `["p", <retrieval key>]` | the pushed-down filter that selects exactly the fleet events (fact 5). The retrieval key is a keypair created at `fleet init` that **no process holds**; mentioning it wakes nothing. |
| `["p", <recipient>]` | whoever must wake: assignee, rework target, requester, owner |
| `["e", <root>, "", "root"|"reply"]` | NIP-10 thread markers |
| `["t", "fleet"]`, `["t", "fleet:task:<id>"]`, `["t", "fleet:run:<id>"]` | labels for humans and local filtering only |
| `["fleet", <json>]` | payload; `v: 1`; at most 8 KiB |

**Identities and authorization.** The verified event author is the actor;
payload `from` must equal it. Allowed authors per type are listed below. The
conductor keys and retrieval key come from the owner-signed fleet record
(5.9), never from display names. Anything not allowed becomes a note.

**Ids.** `task`, `run`, `attempt`, and `cmd` are UUIDv4 or deterministic
strings; views show 8-character prefixes; lookups accept a unique prefix.

**Payload types:**

- `delegate` — author: any fleet member (agent or owner) or an active
  conductor. `{v, type, task, attempt, run?, step?, group?, parent_task?,
  required?, from, to, deadline, rework_target?, artifact?, acceptance}`.
  `artifact` = `{repo, commit, branch?, base?}`; `commit` is required when
  `repo` is given. The CLI refuses a dirty or unpushed checkout.
- `ack` — author: assignee of `attempt`. `{task, attempt}`.
- `report` — author: assignee of `attempt`. `{task, attempt, status:
  done|blocked|failed, next: "<task id>"|"none"|"default", input_commit?,
  output?: {commit, branch?}, evidence?: [string]}`. `input_commit` must equal
  the delegation's commit when one was given, else the report is a
  `mismatch` and a re-report is requested.
- `cancel-task` — author: requester or owner. `cancel-run` — author: owner.
  Result-acceptance transitions; the conductor also posts a `cancel-notice`
  mentioning the assignee, which reaches a running turn as a steering message
  (fact 11). Hard stop remains the owner's `!cancel @Agent`.
- `run` — author: owner or any fleet member. `{run, pipeline, brief, steps:
  [...], limits, retention?, budget?, notify_on_done}`; self-contained.
- `owner-command` — author: owner, produced by the conductor from a chat
  line in a run thread that starts with `!fleet` (5.3). The original chat line
  is not state; the echoed event is.
- Conductor-only (author must be an active conductor key): `redeliver`,
  `nudge`, `escalate`, `advance` with its `delegate`, `fallback`,
  `cancel-notice`, `run-paused`, `run-done`, `run-failed`, `budget-paused`,
  `heartbeat` (`{host, role: primary|standby, last_reconciled_at}`),
  `takeover`, `yield`. Each carries a deterministic `cmd`.

**Threading.** One thread per run rooted at the `run` event. An ad-hoc
`delegate` replies in the thread named by `--thread <root>` or starts one;
its `ack` and `report` reply in the same thread. Thread memory is an
optimisation; every command works from persisted task data.

**Deletions.** `run purge` publishes owner-signed Buzz delete events for every
message in the run thread (`build_delete_message`, kind 9005). Readers and the
conductor also fetch kind 9005 and kind 5 by `authors=[owner]` in each
channel, and drop deleted events before reducing, so a fresh read and a
running conductor agree.

**Plain chat is not state**, with one exception: owner chat lines starting
with `!fleet` in a run thread, which the conductor converts into
`owner-command` events.

### 5.2 Retrieval

Pushed-down filters only (fact 5):

- All fleet events in a channel: `{"kinds":[9], "#h":[channel],
  "#p":[retrieval_key], "until": T, "limit": 1000}`, paged by `until` = the
  smallest `created_at` of the previous page, de-duplicated by id, until a
  page adds nothing; ties at the boundary are re-fetched, not lost.
- One run or task thread: `{"kinds":[9], "#h":[channel], "#e":[root]}` plus
  `{"ids":[root]}`, paged the same way.
- Deletions: `{"kinds":[9005, 5], "#h":[channel], "authors":[owner]}`, paged.
- Metrics (owner key, read-only): `{"kinds":[44200], "#p":[owner],
  "authors":[fleet agents]}` since the run's start, decrypted locally.

A reconciling read starts from `checkpoint − 15 min`; a cold read fetches
everything. CLI views, the TUI, and the conductor share the reader and the
reducer.

### 5.3 Agent- and owner-facing CLI (Python)

Identity: inside an agent, from the inherited environment; for the owner,
the community's admin key. Retrieval key and conductor keys from the cached
fleet record.

```
buzz-fleet task delegate --to <Name|pubkey> --brief <text|-> [--wait 60m]
        [--repo <url> --commit <sha> [--branch b] [--base sha]] [--accept "<criterion>"]...
        [--run <id>] [--thread <root>] [--parent <task>] [--optional] [--channel <uuid>]
    Mints task and attempt ids before publishing; on an ambiguous publish it
    re-reads by id and resends the same signed event, never a new id.
    Refuses when the requester already has 5 open ad-hoc tasks or the parent
    chain is 4 deep (fleet-record settings). Refuses a --channel that differs
    from the run's channel.
buzz-fleet task ack --task <id>
buzz-fleet task report --task <id> --status done|blocked|failed --summary <text|->
        [--next <task id>|none|default] [--input-commit sha] [--output-commit sha] [--evidence "<line>"]...
buzz-fleet task cancel <id> --reason <text>
buzz-fleet task show <id> | buzz-fleet tasks [--open|--stuck|--mine|--unacked]
buzz-fleet runs [--open] [--metrics] | buzz-fleet run <pipeline> --brief <text|-> [--channel]
buzz-fleet run cancel <id> | run resume <id> | run purge <id>
buzz-fleet fleet init [--channel <uuid>] | fleet status | self-update
buzz-fleet conductor install [--standby] | conductor move
```

**Owner commands from a phone**, typed in the run thread by the owner:
`!fleet resume`, `!fleet cancel [reason]`, `!fleet fallback <Name>`,
`!fleet ack`, `!fleet done <summary>`, `!fleet failed <summary>`,
`!fleet blocked <question>`. The prefix avoids buzz-acp's reserved `!cancel`,
`!shutdown`, `!rotate`. The conductor validates the author, echoes an
`owner-command` event, and applies it; unknown commands get a one-line
reply. `!fleet ack/done/failed/blocked` act on the task assigned to the owner
in that thread (5.5 human steps).

The reducer is pure: `reduce(events, record) -> State`. Tasks carry
`requester`, `assignee`, `attempt`, `parent_task`, `required`, `run`,
`step`, `group`, `rework_target`, `artifact`, `acceptance`, `deadline`,
`acked_at`, `status`, `report`, `notes`. Statuses: `open`, `acked`, `done`,
`blocked`, `failed`, `cancelled`, `superseded`; derived flags `late`,
`nudged`, `escalated`, `unacked`. Runs carry `current_step`,
`attempts_per_step`, `rework_count`, `task_count`, `status` (`running`,
`paused`, `budget-paused`, `done`, `failed`, `cancelled`), and metrics.
Terminal states are final; later contradicting events become notes.

### 5.4 Conductor (Python, long-lived, primary plus standby)

**Keys and fencing.** The fleet record lists the retrieval key, the primary
conductor key, and the standby conductor key. Each conductor secret exists
only on its own host (`~/.config/buzz-fleet/conductor/<community>.env`,
`flock`ed while running). Agents' events mention the retrieval key, so a
takeover changes nothing for agents.

**Failover.** Both conductors run the same code. The primary publishes a
`heartbeat` every 5 minutes. The standby reduces the same events but takes
no action while it sees a primary heartbeat younger than 10 minutes. After
10 minutes of silence it publishes `takeover` and becomes active. When a
primary heartbeat reappears the standby publishes `yield` and goes passive;
the primary, on start, waits one heartbeat interval and yields to an active
standby's `takeover` newer than its own last heartbeat, then reclaims after
its next two heartbeats. Overlap actions are harmless: task, attempt, and
`cmd` ids are deterministic and the reducer ignores repeats. `conductor move`
rotates keys when the owner wants a different pair of hosts.

**State.** SQLite per conductor: event cache, reduced state, checkpoint, and
the **outbox**, which is authoritative for pending publications: insert the
signed event with its `cmd` before sending, mark `sent` on relay OK,
reconcile on restart by reading the relay for that `cmd`. Losing the
database costs at most one repeated in-flight action.

**Command ids.** `redeliver:<task>:<attempt>:<n>`, `nudge:<task>:<attempt>`,
`escalate:<task|run>:<reason>`, `advance:<run>:<step>:<attempt>`,
`fallback:<task>:<attempt>`, `cancel-notice:<task>:<attempt>`,
`run-done:<run>`, `run-failed:<run>`, `run-paused:<run>:<step>`,
`budget-paused:<run>`, `purge:<run>`.

**Loop.** Start: take the lock, reconcile outbox, paged read, reduce, then
subscribe per joined channel (`kinds [9, 9005, 5]`, `#h`, and `#p`
retrieval key or `authors` owner) and reduce live events. Tick every 30 s;
timer actions gated until catch-up completes. Heartbeat every 5 minutes.
Views mark a conductor stale after 15 minutes and show which one is active.

**Notifier.** Every `escalate`, `run-paused`, `budget-paused`, `run-done`,
`run-failed`, `takeover`, and `yield` is also sent through the notifier:
ntfy (primary, hosted on the VPS via buzz-deploy), Telegram bot, SMTP email,
generic webhook. One interface, per-target config in the conductor env,
retries with backoff, and a delivery log in SQLite shown by `fleet status`.
The notifier is the path that works when the relay or the app is what broke.

**Metrics and budgets.** The conductor reads kind 44200 with the locally
stored owner key, read-only (ADR 0002), joins turns to tasks by agent and
time window, and records per step and per run: elapsed (delegate to report),
time to ack, turns, tokens, cost where reported. `budget` in a pipeline or
run (`usd` or `turns`) pauses the run with `budget-paused` and notifies the
owner when exceeded; a fleet-wide daily budget does the same for ad-hoc
tasks by refusing new delegations from the CLI.

**Rules** (times from event timestamps):

| Condition | Action |
|---|---|
| `open`, no `ack`, ack window passed (min(15 min, deadline/4)) | `redeliver` n+1 with backoff 15, 30, 60 min then hourly; after 6, `escalate(undelivered)`, or `fallback` if configured. Delivery recovery is separate from lateness. The agent side recovers through its heartbeat feed check (fact 6). |
| live and past `deadline`, no nudge this attempt | one `nudge` |
| still unreported `grace` (15 min) after the nudge | `fallback` (new attempt; old attempt `superseded`, its late report becomes a note) else `escalate` |
| `report` with `mismatch` | one `nudge` asking for a re-report; counts as rework |
| `report done`, `next = default`, step k not last | `advance` immediately: delegate step k+1 (or every member of a parallel group), requester = conductor, `rework_target` = step k assignee, artifact = report output commit |
| `report done`, `next = <task>` | that task is step k+1; no action |
| `report done`, `next = none`, step not last | `run-paused`; owner resumes with `!fleet resume` or the CLI |
| parallel group: all `required` members `done` | advance past the group; optional members still open are left to finish and tracked |
| parallel group: a required member `failed` | rework to the group's `rework_target` (the step before the group); on `done`, re-delegate the whole group as new attempts; other members' late reports become notes |
| last step `done`, no required child live | `run-done`, notify `notify_on_done`, include metrics |
| `report failed` on step k > 1 | rework task for `rework_target` with the failure summary and the step's `timeout`; `rework_count += 1`; when `done`, re-delegate step k |
| `failed` on step 1, rework `failed`, `rework_count > max_rework` (3), `task_count > max_tasks` (20), or `run_deadline` passed (2 × sum of timeouts) | `run-failed` |
| `report blocked` | route the question to the requester if an agent, else the owner; the run pauses on that task |
| budget exceeded | `budget-paused` |
| `cancel-task` / `cancel-run` | mark cancelled, post `cancel-notice` to live assignees |
| retention elapsed for a closed run | `purge` (owner-key delete events, ADR 0002 scope) |
| owner `!fleet ...` chat line in a run thread | validate, echo `owner-command`, apply |

Ad-hoc tasks get delivery recovery, lateness, escalation, cancel notices, and
the daily budget.

**Recycling.** On every machine, buzz-fleet installs a `--user` timer
(`buzz-fleet agent recycle`, hourly) that restarts an agent unit when that
agent has no live task, no turn in the last 6 hours (from metrics, owner key
read-only), and is not the assignee of any open run thread. This is the only
cleanup buzz-acp's session model allows (fact 10).

### 5.5 Pipeline configuration

```yaml
name: impl-review-build
channel: null                 # defaults to the fleet channel
steps:
  - role: implement
    agent: Implementer
    timeout: 90m
  - parallel:                 # a step group; the run continues when all required members are done
      - role: review
        agent: Reviewer
        timeout: 45m
        fallback: Reviewer-2
      - role: security-review
        agent: SecReviewer
        timeout: 45m
        required: false
  - role: owner-gate          # a human step: the owner acts with !fleet ack/done/failed from a phone
    agent: owner
    timeout: 12h
  - role: build
    agent: Builder
    timeout: 60m
limits: {max_rework: 3, max_tasks: 20, run_deadline: 8h}
budget: {usd: 15}            # or {turns: 60}
retention: keep              # or 30d
notify_on_done: [owner, implement]
```

Names resolve once at run start; `owner` resolves to the owner pubkey. Step
1's artifact comes from `buzz-fleet run --repo/--commit` or the current
checkout. Conditional branches are out of scope; agents deviate instead.

### 5.6 What the owner sees

- Live: the channel in Desktop or mobile; each run one thread.
- Phone: notifier messages for every escalation, pause, done, failed, and
  failover, and `!fleet` commands in the thread.
- Any machine: `buzz-fleet runs --metrics`, `tasks --stuck`, `tasks
  --unacked`, `fleet status` (record, conductors and which is active,
  notifier delivery log, version drift, repo access), and a **Runs screen in
  the TUI** with the same data.

### 5.7 Deployment

- All layers ship in the normal `buzz-fleet` release to all five machines;
  `self-update` brings a machine level with the record.
- `fleet init` runs once on the VPS: creates the channel, the retrieval key,
  the primary conductor key, the fleet record, and installs
  `buzz-fleet-conductor@<community>`. `conductor install --standby` on the
  dedicated server creates the standby key and registers it in the record.
- ntfy is added to buzz-deploy's Compose next to the relay; its URL and token
  go into the conductor env.
- Every machine's `ensure_runtime_ready` installs the recycle timer.

### 5.8 Sessions and lifecycle

- `thread` session policy by default; `channel` per-agent opt-out.
- Parallelism guidance: 2 on laptops, 3–4 on servers; each slot costs API
  usage.
- Reports are self-contained.
- Dormant sessions are recycled by the timer in 5.4; the turn cap rotates
  active sessions only.
- Retention: `keep` by default; a pipeline or the fleet record may set N
  days, after which the conductor purges closed runs; `run purge` purges on
  demand. Deletions are consumed by every reader (5.1).

### 5.9 The fleet channel and the fleet record

- `fleet init [--channel]` creates or adopts the channel and writes the
  record into its owner-signed metadata `about`: a readable first line, then
  JSON `{"buzz-fleet": 1, "retrieval_key", "conductors": {"primary": {pubkey,
  host}, "standby": {pubkey, host}?}, "limits": {...}, "budget": {...},
  "retention", "versions", "created_at"}`. Discovery is by that marker.
- Every machine discovers and caches it in `ensure_runtime_ready`.
- Every managed agent auto-joins the fleet channel; the retrieval key and
  both conductor keys are members; no restarts needed (fact 9).
- Agents get `BUZZ_FLEET_CHANNEL` and `BUZZ_FLEET_RETRIEVAL_KEY` in their env.
- Project channels: `run --channel` validates membership of every step agent,
  the retrieval key, and both conductors; the conductor subscribes per
  channel with explicit `#h`.

## 6. Walkthroughs

**Pipeline with review, owner gate, and build.** Owner starts the run from a
pushed commit. Implementer acks, works in a worktree, pushes, reports `done`
with its output commit. The conductor advances the parallel group: Reviewer
and SecReviewer get the commit. Reviewer reports `failed` with findings;
rework goes to Implementer, who fixes and reports; the group is re-delegated.
Both pass; the owner gate task is delegated to the owner, who gets an ntfy
notification, reads the thread on the phone, and types `!fleet done looks
good`. The conductor advances to Builder, who reports `done` with evidence;
`run-done` posts the summary with elapsed time, turns, and cost, and ntfy
delivers it.

**Stopped builder, dead primary.** Builder's machine is off; redelivery at
15, 30, 60 minutes. Meanwhile the VPS reboots; the standby on the dedicated
server takes over after 10 minutes and continues the redelivery schedule
under the same `cmd` ids. Builder's machine boots, its heartbeat feed check
surfaces the mention, it acks and works. The VPS returns, the primary sees
the `takeover`, waits, then reclaims; the standby yields.

**Lost acknowledgement and cancel.** `task delegate` times out after send;
the CLI re-reads by task id, finds the event, and returns success. Later the
owner types `!fleet cancel` in the thread; the conductor echoes the command,
cancels the run, posts cancel notices, and Builder's running turn receives it
as a steering message and stops.

## 7. Error handling and edge cases

- Relay or auth failure: JSON error, exit 1, never fake success.
- Ambiguous name: error listing candidates.
- Report from a non-assignee or on a superseded or terminal attempt: refused
  by the CLI, noted if it reaches the relay.
- Duplicates: by event id, by `cmd`, by task and attempt ids.
- Clock skew: absolute deadlines; relay rejects ±900 s; reads overlap 15 min.
- Cancellation does not undo a push; a cancel notice steers a running turn;
  the owner's `!cancel @Agent` hard-stops it.
- Conductor database lost: full re-read; at most one repeated action.
- Both conductors down: views and notifier log show stale; agents still work
  through mentions; on return, catch-up before timers.
- Notifier target down: retries with backoff, delivery log, and the message
  is still in the channel.
- Codex sandbox and `/tmp`: verified live (section 11).
- Workspaces: convention plus detection through the artifact contract.

## 8. Testing

Unit: reducer (every rule in 5.4 including groups, human steps, owner
commands, deletions, authorization, terminal precedence, duplicates,
out-of-order), conductor tick with fake clock and recording publisher
(outbox reconcile, failover state machine, budgets, retention), paged reader
against a fake relay with >1,000 events and boundary ties, notifier with a
fake transport, metrics join, recycle decision, CLI argv and refusals, signer
builders, instructions block.

Acceptance gates:

1. A run behind more than 1,000 newer messages is reconstructed identically
   by every machine and a cold conductor.
2. Lost publish ack; crash between report and handoff; restart during grace:
   one accepted successor, no duplicate accepted result.
3. Assignee stopped before delegation, started after nudge and escalation:
   resumes without a second logical task.
4. Primary conductor killed mid-run: standby takes over within 10 minutes,
   primary reclaims cleanly, no duplicate accepted action.
5. Conductor-created assignments, failure, fallback with a late original
   result, run cancellation reaching a running turn, clean process with no
   session memory.
6. Same immutable revision across reviewer and builder; concurrent runs in
   separate worktrees; rework stops at the limit; parallel group joins
   correctly with an optional member left open.
7. Owner gate completed from a phone with `!fleet done`; every notice
   delivered by ntfy; a purge leaves every reader consistent.
8. Budget pause fires on cost where reported and on turns otherwise; metrics
   in `run-done` match the decrypted turn metrics.
9. All of the above in each of Claude Code, Codex, Pi, and goose, plus one
   mixed-harness pipeline.
10. Same representative tasks with one agent versus the fleet: quality,
    interventions, elapsed, cost, recovery failures, sample size stated.

## 9. Out of scope

- Device-aware routing (hostnames are informational).
- Conditional branches in pipelines.
- Cross-channel runs.
- Automatic takeover across more than one standby.
- Stopping a running turn from the conductor (only the owner can).

## 10. Resolved in revisions 2 and 3

Fact 9 corrected; retrieval by retrieval-key mention plus paging; fleet
record in channel metadata; requester vs rework target; `--next`, cancel,
`blocked`, limits; UUID ids; push gateway ruled out and notifier adopted;
owner commands with the `!fleet` prefix; human steps; failover; purge with
tombstones; recycling; metrics and budgets via owner-key reads; parallel
groups; TUI screen; self-update; language validated (section 12).

## 11. Verify on the live fleet during implementation

1. Paged `#p` retrieval-key reads are complete past 1,000 rows under NIP-OA
   delegated auth.
2. `buzz-fleet task` reaches the relay from inside a Codex turn on Linux.
3. Heartbeat feed check surfaces a mention that arrived while down.
4. Membership notification subscribes the installed Sprig build to a new
   channel without restart.
5. systemd accepts quoted multi-line env values on every machine.
6. Installed versions match the record; `self-update` brings a machine level.
7. The PyInstaller CLI starts inside each harness sandbox (a read-only or
   `noexec` `/tmp` would break it; mitigation `--runtime-tmpdir` or
   `--onedir`).
8. Kind 44200 metrics decrypt with the admin key stored in the community file
   and carry `costUsd` for the providers in use.
9. A steering message reaches a running turn on each harness.
10. Per-machine SSH access to every repository named in a run.

## 12. Implementation notes (language and seams)

Keep the agent CLI and the conductor in Python (Typer + Pydantic); delegate
all relay I/O to the Rust signer; shell only in unit files, timers, and the
installer. Measured: 0.9 s per CLI call (0.55 s onefile extraction), 0.5 s
with `--onedir`. The conductor needs only the standard library (`subprocess`
streaming child, `sqlite3` WAL, a thread or `selectors` loop). Add a
`StreamingCommandRunner` seam next to `CommandRunner` in `proc.py`
(`stream(args)` → handle with `lines()`, `terminate()`, `returncode()`),
faked by a list of connections. Signer subcommands are I/O only:
`post-message`, `query` (paged), `subscribe`, `channel-members`,
`create-channel`, `find-fleet-record`/`write-fleet-record`,
`delete-message`, `decrypt-metrics`.

## 13. Grilling decisions (2026-09-06)

| # | Decision |
|---|---|
| 1 | Owner chat commands in run threads with the `!fleet` prefix |
| 2 | Human (owner) steps in pipelines, acted on from a phone |
| 3 | Per-agent working directory, shared clone, worktree per run |
| 4 | Private repos with per-machine SSH keys (assumption to confirm) |
| 5 | Any fleet member may start a run |
| 6 | Ad-hoc limits: 5 open per requester, chain depth 4 |
| 7 | Step clock starts at delegation |
| 8 | Conductor primary on the VPS, standby on the dedicated server |
| 9 | Refuse duplicate display names unless forced |
| 10 | Hostname in the managed-agent record, display only |
| 11 | Fleet record in the channel description |
| 12 | Notifier: ntfy primary, Telegram, email, webhook supported |
| 13 | Conductor failover with standby |
| 14 | Purge, retention, tombstone handling |
| 15 | Idle session recycling timer, 6 hours |
| 16 | Cancel notices; owner hard stop via `!cancel @Agent` |
| 17 | Parallel step groups with join |
| 18 | No conditional branches |
| 19 | Metrics per step and run |
| 20 | Budgets in USD with turn fallback |
| 21 | Runs screen in the TUI |
| 22 | `self-update`, opt-in |
