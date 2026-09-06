# Multi-agent orchestration — independent design review

Date: 2026-09-06. Review of [the draft design](2026-09-06-multi-agent-orchestration-design.md). Recommendation only; the draft and implementation are unchanged.

**Verdict:** keep the shell CLI, Buzz transport, shared channel, thread-per-run presentation, and a deterministic conductor for this five-machine fleet. Revise the execution contract and recovery design before implementation. These are a good fit for the existing harnesses; the current spec does not yet establish its promised reliable handoffs. Add advisory pipeline routing after proving a recoverable handoff between machines.

The owner's freedom to deviate from a pipeline is compatible with strict task lifecycle rules. An agent can choose a different next assignee while the software still enforces who owns an attempt, what constitutes completion, and how duplicate or stale commands are handled.

The recommendation covers **Claude Code, Codex, Pi, and goose**, including mixed-harness pipelines. Fleet commands, task payloads, and conductor rules should remain independent of the model/provider. Compatibility is an acceptance requirement for each installed harness, not an assumption established by testing Claude alone.

**Evidence and limits.** I read the draft, buzz-fleet's README and CLAUDE.md, relevant Python/install code, and the local Buzz source at commit `7a9a5233d9d755e715be0c585cf7850e935d28cf` (2026-09-03). The Buzz checkout has an unrelated untracked Compose override; inspected source files have no reported modifications. The signer separately pins `b1f6b7ef770dddbb7f33c9f5861c379a47bca1d6`, while the runtime installer fetches rolling `sprig-latest` only when its binary is missing. Source inspection does not establish which capabilities are present in each installed runtime or the deployed relay. No live agent turns, relay mutations, or end-to-end tests were performed for this review.

**Changes needed before building**

1. **Define the code artifact that moves between machines.** Sections 5.1–5.4 carry briefs and summaries, but no repository, immutable commit, artifact reference, or acceptance evidence. A reviewer can review revision A while the builder builds revision B; a commit left on a sleeping laptop is unavailable to either.

   Each handoff should carry a reachable repository/artifact location, exact commit or digest, base revision when relevant, acceptance criteria, and test/build evidence. Each result should identify its input and output revision. Reports should be validated against the assignment's artifact. Use an isolated checkout or worktree for each concurrent run, with explicit local repository mapping. Resolve tools and credentials during setup. Agent identity can stay independent of device identity; workspace setup is still required. Treat thread memory as an optimization: a fresh process must be able to resume from persisted task data.

2. **Specify complete history retrieval; a single REQ ending at EOSE is insufficient.** Sections 5.2–5.4 assume that querying all fleet events reconstructs the same state everywhere. The inspected relay clamps each historical query to 1,000 candidate events, then applies remaining tag filters. `#t` is not pushed into that query's database constraints. A task can therefore disappear behind newer channel traffic, even if few returned events match `fleet`.

   Evidence: [`req.rs`](https://github.com/block/buzz/blob/7a9a5233d9d755e715be0c585cf7850e935d28cf/crates/buzz-relay/src/handlers/req.rs#L394) applies `filters_match` to fetched rows; [`filter_to_query_params`](https://github.com/block/buzz/blob/7a9a5233d9d755e715be0c585cf7850e935d28cf/crates/buzz-relay/src/handlers/req.rs#L949) clamps results and pushes selected tags, excluding `t`; [`event.rs`](https://github.com/block/buzz/blob/7a9a5233d9d755e715be0c585cf7850e935d28cf/crates/buzz-db/src/store/event.rs#L33) defines the 1,000-row cap. EOSE marks the end of a subscription's historical response; it does not certify a complete application history. [NIP-01](https://github.com/nostr-protocol/nips/blob/master/01.md)

   Define and demonstrate a pagination/reconciliation method, including timestamp ties and post-filter starvation. An empty filtered page cannot prove there is no older history. Options include paginating unfiltered channel history and filtering locally, or an existing supported endpoint with adequate cursors. Verify cursor support before selecting it. If this cannot be made complete within the no-upstream-changes constraint, make the conductor's database authoritative and revise the view/recovery promises accordingly.

   Persist the reduced state or complete event cache together with the checkpoint. Replaying only the last hour onto an empty state loses older open tasks. Gate timer actions until catch-up is complete. On reconnect use overlap sufficient for the supported clock-skew/delayed-arrival assumptions, rather than blindly using the newest producer timestamp; specify deterministic conflict handling.

3. **Separate event deduplication, command idempotency, and execution ownership.** Section 5.4 says actions are never taken twice because the reducer observes the conductor's events. That fails if the relay accepts a delegation but the acknowledgement is lost: a retry with a new ID can assign the same work twice. It also fails between two active conductors. Seeing one's own event later is not an atomic reservation.

   Give each logical action a stable command ID and each assignment an attempt ID. Persist pending publications and retry the same signed event when appropriate. Reconcile ambiguous publication results before creating a replacement action. Deduplicate semantic actions as well as event IDs. Add assignment acceptance and reject results from superseded attempts. Persisting an unacknowledged outbox makes that data more than a disposable cache; document its recovery contract.

   For v1, explicitly designate one conductor identity/host and take a local process lock; forbid automatic cross-host takeover. A display-name check is neither a lock nor authoritative identity. Active failover would require a shared lease/fencing mechanism. A transport that can repeat delivery still requires the executor to avoid repeating side effects. Do not claim exactly-once execution based on reducer deduplication alone.

4. **Recover outstanding work after an agent process restarts.** Keeping a task on the relay does not ensure the assignee will execute it. The local runtime's initial subscription starts around process startup, with a five-second overlap. Its reconnect cursor is held in process memory. If the builder was stopped during both delegation and nudge, starting it after escalation need not replay either message. The stall walkthrough ends too early.

   Evidence: [`send_subscribe`](https://github.com/block/buzz/blob/7a9a5233d9d755e715be0c585cf7850e935d28cf/crates/buzz-acp/src/relay.rs#L3387), startup watermark setup in [`lib.rs`](https://github.com/block/buzz/blob/7a9a5233d9d755e715be0c585cf7850e935d28cf/crates/buzz-acp/src/lib.rs#L2539).

   Provide startup reconciliation or an explicit resume operation that discovers still-open assignments and sends a fresh wake-up tied to the existing task/attempt. Distinguish delivery recovery from retrying a failed task, preserving the owner's one-nudge policy. Add an accepted/running state so the owner can distinguish an unreceived handoff from slow work. If this depends on an upstream lifecycle hook, confirm the hook exists or implement a fleet-owned recovery path.

5. **Make routing relationships explicit.** Section 5.4 conflates message publisher, delegator, previous pipeline agent, and rework recipient. When the conductor automatically assigns Builder, the task's delegator is the conductor. On build failure, the rule to assign rework to the delegator can assign work to a process with no harness. Store separate requester, assignee, parent task, step ID, and rework target.

   Several rules also need a defined outcome:

   - `--next none` supplies no task, but its rule says the run continues from “that task.” Choose an explicit meaning such as pause/escalate or finish-with-deviation; do not infer a nonexistent successor.
   - Agents publish onward delegation and completion separately. A crash between them or a delayed report races the conductor's two-minute auto-advance. Publish completion plus a successor intent in one event, or define an acknowledged transition protocol. A grace period alone is not coordination.
   - Fallback cancels an old task, while the general `cancel` rule cancels the entire run. Define task cancellation and run cancellation separately.
   - Multiple side delegations need parent links and a required/optional designation. A last-step report cannot complete a run while required child work remains open.
   - `on_fail` is a routing value (`back`) in YAML but a timeout in the rework rule. Separate these fields. Decide whether `blocked` means waiting for input or completed with findings; it should not automatically be treated as a defective previous implementation.
   - Successful fixes followed by repeated failed reviews can loop indefinitely. Add a maximum rework count, delegation depth/task count, and total run deadline; escalate when exhausted. Optional usage budgets can follow if reliable usage data is available.

6. **Keep machine state independent of ambiguous chat.** The reducer cannot detect `answered-unstructured` from an input stream filtered to `#t=fleet`: ordinary replies do not carry that tag. A thread shared by multiple tasks also does not identify which task a plain reply addresses. Either explicitly fetch ordinary messages and require unambiguous task references, or treat these replies as notes and retain the structured-report requirement.

   Define event schema versioning, payload limits, allowed authors for every transition, and terminal-state precedence. Derive identity from the verified event author, validating payload `from` and referenced tasks. The report-author rule currently admits the delegator while its parenthetical says they may only cancel. Pin the conductor pubkey through owner-authorized configuration; never trust its display name. Ensure explicit channel overrides cannot create a cross-channel run, which v1 prohibits.

7. **Use durable IDs and honest cancellation semantics.** Four random hex digits give a run ID only 65,536 possibilities per pipeline; collisions become plausible after a few hundred runs. Six-digit task IDs have the same problem at larger task counts. Use full UUIDs or equivalent collision-resistant IDs in the protocol; show short prefixes only in the UI and reject ambiguous lookups.

   Cancelling the reducer's task does not interrupt an active harness or undo a Git push/build. Define cancellation as a scheduling/result-acceptance transition, specify best-effort worker stopping separately, and ignore late results from invalidated attempts. A fallback must not assume the original process has stopped. This directly affects artifact correctness.

**Factual corrections and smaller design adjustments**

- **Fact 9 is contradicted by the inspected source.** `lib.rs` subscribes to membership notifications and [dynamically subscribes to newly joined channels](https://github.com/block/buzz/blob/7a9a5233d9d755e715be0c585cf7850e935d28cf/crates/buzz-acp/src/lib.rs#L3262), subject to configured channel rules. Pin and test the installed runtime before deciding it requires restarts. A long-lived fleet channel remains a reasonable choice; it need not rest on a universal startup-only limitation.
- **A 40-turn limit does not clean up dormant sessions.** The [rotation counter](https://github.com/block/buzz/blob/7a9a5233d9d755e715be0c585cf7850e935d28cf/crates/buzz-acp/src/pool.rs#L2982) advances when a session completes turns. A finished session that used four turns never reaches 40 while dormant. Describe this as active-session rotation; actual cleanup requires supported eviction/invalidation or measured process recycling. Session state is per harness process, not pooled memory shared by agents.
- **Runtime version checks belong before protocol implementation.** The signer uses a pinned Buzz commit, the inspected checkout has another commit, and [`ensure_buzz_acp_installed`](../../../src/buzz_fleet/buzz_acp.py) leaves an existing executable untouched. Record tested runtime/relay/signer versions and verify required capabilities at setup. A date on a source inspection is not a compatibility guarantee across five installations.
- **The proposed template restart behavior is new work.** [`ensure_template_unit_installed`](../../../src/buzz_fleet/systemd.py) reloads systemd but returns no changed flag. [`ensure_runtime_ready`](../../../src/buzz_fleet/manager.py) currently restarts for other conditions, not a changed template alone. Include this explicitly in the prerequisite work. The proposed `get.sh` payload path is consistent with the actual installer.
- **MCP does not inherently require a blocking wait.** The dated [2026-07-28 Tasks extension](https://tasks.extensions.modelcontextprotocol.io/specification/2026-07-28/tasks) supports asynchronous task handles and later polling. Even a small custom submit/status tool can return immediately. Keep CLI as the practical common interface while the single MCP slot and actual client support remain constraints; narrow the rejected approach to the blocking design that was considered.
- **Deletion must invalidate cached state.** A subscriber to kind 9 fleet events does not thereby consume kind 9005 deletion events. Purging a run may make a fresh read disagree with the running conductor unless it observes deletion/tombstone information or reconciles existence. Defer purge until this behavior is specified. Keep operational state/retention semantics explicit if chat serves as the sole ledger.
- **Discovery needs stable metadata.** Adopting an arbitrary existing channel in `fleet init --channel` does not make it discoverable on other machines by the name `fleet`. Store an owner-authorized fleet configuration record using a supported event mechanism, or explicitly propagate the channel ID. Scope the conductor identity/unit by community or explicitly limit v1 to one community per host.
- **Stalls include conductor failure.** When it is down, it cannot send its own escalation. Expose its last successful reconciliation/health timestamp and stale status in the read views. If unattended failure notification is required, use an independent watcher. Avoid dashboards that present an old snapshot as current.

**Implementation recommendation**

Keep one orchestration module whose interface validates commands, derives state, and decides actions. Shell presentation and relay signing are adapters. Keep reducer/action decisions testable without a live harness. Pipeline defaults should feed the same task lifecycle used for ad-hoc delegation. Do not introduce a general workflow-engine abstraction until there is a second real implementation.

Build in this order:

1. Verify/pin runtime capabilities; fix executable discovery and managed instructions; establish repository/workspace setup.
2. Ship one recoverable task lifecycle with artifact references, stable IDs, acceptance, reports, scoped cancellation, complete reads, and a single designated conductor with persistent publication recovery.
3. Exercise Implementer → Reviewer → Builder across actual machines, including a stopped laptop and process restarts.
4. Add linear pipeline defaults, then bounded rework and explicit deviations using the same protocol. Defer inferred chat completion, automatic failover, arbitrary fan-out joins, and purge until their need and semantics are clear.

The relay can remain the shared event ledger only after its retrieval and recovery tests pass. If operating a reliable custom scheduler becomes the larger task, use a durable workflow engine for the conductor and keep Buzz as the agent transport and owner-facing channel. Temporal is the strongest alternative to evaluate for durable timers, external signals, and long-lived recovery; it still requires idempotent external work and a bridge to Buzz. Its [execution model](https://docs.temporal.io/workflow-execution) describes persisted history and recovery. See the separate [current alternatives research](2026-09-06-multi-agent-orchestration-alternatives.md) for tradeoffs and primary sources.

**Acceptance evidence before declaring the fleet more productive**

- Run the same representative coding tasks with one agent plus build tools and with the fleet. Compare accepted output quality, owner intervention, elapsed time, total usage/cost, and recovery failures. Report the sample size; do not assume more agents improve every task.
- Complete a run whose earlier events sit behind more than 1,000 newer channel messages, including ordinary untagged traffic; all machines and a cold conductor must reconstruct it consistently.
- Drop a publish acknowledgement, crash between report and next handoff, and restart during escalation grace. Verify one accepted logical successor and no duplicate accepted execution result.
- Stop the assignee before delegation; restart it after both nudge and escalation. Resume the outstanding task without creating a second logical task.
- Exercise conductor-created review/build assignments followed by failure, fallback with a late original result, run cancellation, and a clean process with no prior session memory.
- Verify reviewer and builder use the same immutable revision, concurrent runs use separate workspaces, and rework loops stop at their configured limit.
- Exercise delegate/report, task lookup, inherited identity, shell/network permissions, and fresh-session recovery in each of Claude Code, Codex, Pi, and goose, plus one pipeline that mixes harnesses.

These tests assess the owner's stated goal directly: work transfers correctly, stalls become visible, and recovery requires less intervention than plain agent chat.
