# buzz-fleet

Manages headless Buzz agents across a small set of machines owned by one person, and coordinates work handed between those agents over the shared Buzz relay.

## Language

### Fleet

**Fleet**:
All agents, on every machine, that share one owner and one Buzz community.
_Avoid_: cluster, swarm, team

**Agent**:
One headless harness process with its own Nostr identity, run as a systemd unit by buzz-fleet on one machine.
_Avoid_: bot, worker, persona (a persona is the template an agent is created from)

**Owner**:
The single human whose key attests every agent and who receives escalations.
_Avoid_: admin, user, operator

**Fleet channel**:
The one long-lived Buzz channel per community where orchestration traffic lives by default. Every agent is a member.
_Avoid_: ops channel, control channel

**Fleet record**:
The owner-signed description of the fleet stored in the fleet channel's metadata: the retrieval key, the primary and standby conductor keys and hosts, fleet-wide limits, budget, retention, and the tested versions.
_Avoid_: config record, manifest

**Conductor**:
The always-on process, with its own identity, that watches fleet events, keeps deadlines, recovers delivery, advances pipelines, notifies, and escalates. One primary and one standby exist; exactly one is active.
_Avoid_: orchestrator, scheduler, index

**Primary / Standby**:
The two conductor hosts. The primary is active while its heartbeats are fresh; the standby takes over after ten minutes of silence and yields when the primary returns.
_Avoid_: master/slave, leader/follower

**Retrieval key**:
A keypair no process holds, mentioned on every fleet event so a single indexed relay filter selects exactly the fleet's events.
_Avoid_: index key, conductor key

**Notifier**:
The conductor's outbound channel to the owner outside Buzz: ntfy, Telegram, email, or a webhook.
_Avoid_: alerting, push

**Planner**:
An agent whose persona plans work instead of doing it: it reads the requirement and the code, consults the directory, and publishes a proposal.
_Avoid_: PM bot, orchestrator agent

**Directory**:
The list of fleet agents with their role, capabilities, description, host, and online state, read from the relay.
_Avoid_: roster, registry

**Capability**:
A short label on an agent saying what it can do, used to choose agents for steps.
_Avoid_: skill (a skill is a file a harness loads), tag

**Owner command**:
A chat line by the owner in a run thread starting with `!fleet`, which the conductor turns into a fleet event and applies.
_Avoid_: chat command, bang command

### Work

**Task**:
One unit of work handed to one agent, identified by a stable id for its whole life.
_Avoid_: job, ticket, assignment (an assignment is one attempt of a task)

**Attempt**:
One assignment of a task to one agent. A task gets a new attempt when it is redelegated to a fallback; the old attempt is superseded.
_Avoid_: retry, try

**Delegation**:
The act of handing a task to an agent, and the fleet event that records it.
_Avoid_: mention, ping, handoff (handoff is the informal name for the whole exchange)

**Requester**:
The identity that delegated a task: an agent, the owner, or the conductor.
_Avoid_: delegator, sender, caller

**Assignee**:
The agent a task's current attempt is assigned to.
_Avoid_: recipient, worker

**Rework target**:
The agent whose earlier work a failed task refers back to; the previous pipeline step's assignee, never the requester.
_Avoid_: author, original agent

**Ack**:
The assignee's fleet event saying it has received a delegation. Distinguishes an undelivered handoff from slow work.
_Avoid_: accept, claim

**Report**:
The assignee's fleet event closing an attempt with a status of done, blocked, or failed and a self-contained summary. The only way work is completed; chat never completes a task.
_Avoid_: reply, result, response

**Artifact**:
The exact code revision a task is about: a reachable repository and an immutable commit, carried on every delegation and named again in every report.
_Avoid_: branch, changes, diff

**Workspace**:
An agent's per-run git worktree under its working directory, so concurrent runs never share a checkout.
_Avoid_: sandbox, checkout

**Budget**:
A per-run, per-pipeline, or fleet-wide ceiling in USD, or in turns when cost is unreported, beyond which the run pauses.
_Avoid_: quota, limit (limits are counts such as max rework)

### Pipelines

**Pipeline**:
A named, linear list of steps, each naming an agent and a timeout, configured once by the owner.
_Avoid_: workflow (Buzz has its own unrelated "workflow" feature), flow

**Run**:
One execution of a pipeline, with its own id, thread, limits, and current step.
_Avoid_: job, execution, instance

**Step**:
One position in a pipeline: a role, the agent that fills it, and its timeout.
_Avoid_: stage, phase

**Step group**:
Several steps delegated at once; the run continues when every required member is done.
_Avoid_: fan-out, parallel stage

**Human step**:
A step whose agent is the owner, completed with owner commands from any device.
_Avoid_: approval gate, manual step

**Proposal**:
A planner's suggested run, with steps, agents, timeouts, budget, and rationale, waiting in its thread for the owner to approve, edit, or reject.
_Avoid_: plan, draft run

**Workspace files**:
Skills and rules a persona ships into an agent's working directory so the harness loads them.
_Avoid_: dotfiles, config

**Deviation**:
An agent choosing a different next assignee than the pipeline's default, recorded as part of the run.
_Avoid_: override, detour

**Pipeline default**:
The next assignee a pipeline suggests to an agent; advice the agent may decline with a reason.
_Avoid_: rule, next hop

### Recovery

**Redelivery**:
The conductor re-mentioning an assignee that has not acked, with backoff. Delivery recovery, not lateness.
_Avoid_: retry, resend

**Nudge**:
The single reminder the conductor sends when an acked task passes its deadline.
_Avoid_: reminder, ping

**Escalation**:
The conductor mentioning the owner because a task or run cannot proceed on its own.
_Avoid_: alert, page

**Fallback**:
The agent a pipeline step names to take over a task when the assignee does not report after the nudge.
_Avoid_: backup, substitute

**Cancel notice**:
The conductor's mention telling an assignee its task was cancelled; it steers a running turn but does not hard-stop it.
_Avoid_: kill, abort

### Housekeeping

**Recycling**:
Restarting an idle agent unit so dormant harness sessions are released.
_Avoid_: cleanup, GC

**Retention**:
How long a closed run's thread is kept before the conductor purges it; "keep" means forever.
_Avoid_: TTL, expiry

**Purge**:
Owner-signed deletion of every message in a run thread, consumed by every reader.
_Avoid_: delete, archive
