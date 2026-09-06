# Multi-agent orchestration — alternatives review

Reviewed: 2026-09-06. Scope: external alternatives to the accompanying
[design](2026-09-06-multi-agent-orchestration-design.md), using primary sources.
Product-fit judgments below are architectural inferences, not benchmark results.
This note does not verify the installed Buzz build or harness capabilities.

**Recommendation: keep the shell CLI, Buzz transport, and existing harnesses;
implement a smaller conductor with explicit durability guarantees.** Five machines
alone do not justify replacing the agent stack. Temporal is the strongest
alternative if reliable recovery and increasingly complex routing are worth an
additional service. LangGraph and A2A address different parts of the problem.

This recommendation applies to **Claude Code, Codex, Pi, and goose**, including
mixed-harness pipelines. Neither the shared CLI nor a replacement conductor needs
to depend on a particular model provider. Verify each installed harness separately.

| Option | What it contributes | Fit for this fleet |
|---|---|---|
| Custom conductor | Fleet-specific task accounting, deadlines, notifications, and routing using the existing relay | Best initial fit if lifecycle rules stay small and recovery is demonstrated; the team owns delivery, ordering, replay, and duplicate suppression |
| Temporal behind Buzz | Durable workflow state, asynchronous signals, and persisted timers | Best upgrade when complex recovery, parallel joins, long waits, or multiple conductor workers become requirements; keep the harnesses and adapt Buzz events |
| LangGraph behind Buzz | Checkpointed graph state, resumable graph steps, and human intervention | Useful if orchestration itself becomes an agent/graph application; less compelling when the desired change is reliable delivery between existing harnesses |
| A2A boundary adapter | Standard agent discovery, tasks, status, and artifacts | Useful when connecting agents outside this private fleet; does not choose or implement the fleet's deadline and recovery policies |

Temporal workflows can receive asynchronous Signals that change state and control
execution; durable timers survive worker/service outages. That maps naturally to
`report`, cancellation, deadlines, and escalation. A possible integration is one
workflow per run, a Buzz subscriber forwarding events as Signals, and Activities
publishing replies. This can preserve advisory routing: a workflow can record an
agent's proposed next task while enforcing valid lifecycle transitions. These are
integration proposals, not ready-made Buzz support. [Temporal message passing](https://docs.temporal.io/develop/python/workflows/message-passing),
[durable timers](https://docs.temporal.io/develop/python/workflows/timers).

Temporal does **not** make external effects exactly once: an Activity can publish
successfully and then fail before recording completion, causing a retry. Stable
operation IDs and idempotent Buzz publishing remain necessary. Self-hosting also
adds a Temporal Service and persistence administration; its development server is
distinct from the documented sustained-workload deployment. [Activity idempotency](https://docs.temporal.io/activity-definition#idempotency),
[self-hosting](https://docs.temporal.io/self-hosted-guide/deployment).

LangGraph 1.0 was announced on **2025-10-22**. Its checkpointers persist graph state
at step boundaries and support resuming after failure; the docs distinguish
in-memory experimentation from persistent SQLite/Postgres implementations and
offer different durability modes. Existing agents could be called through adapter
nodes, so adopting it need not rewrite their internal reasoning loops. However,
checkpointing the coordinating graph does not establish that another machine
received a task, fetched the right code revision, or stopped work after
cancellation. Those contracts still need adapters and explicit acknowledgements.
My judgment is to prefer Temporal over LangGraph when the main new requirement is
distributed workflow reliability, and LangGraph when graph-level agent behavior is
the main application being built. [1.0 announcement](https://www.langchain.com/blog/langchain-langgraph-1dot0),
[checkpointer semantics and durability](https://docs.langchain.com/oss/python/langgraph/checkpointers).

A2A 1.0 was released on **2026-03-12**; the release list also contains 1.0.1 from
May 2026. The protocol separates messages, tasks, task status, and output
artifacts, and supplies discovery and asynchronous interaction mechanisms. Borrow
that separation now: put repository identity, immutable commit, review target,
build outputs, and verification evidence in structured task results instead of
relying exclusively on prose summaries. Add an A2A gateway when external
interoperability becomes useful; it does not replace the conductor's scheduling
policy or supply artifact storage automatically. [A2A releases](https://github.com/a2aproject/A2A/releases),
[A2A specification](https://a2a-protocol.org/latest/specification/).

**The MCP rejection needs a narrower explanation in 2026.** The official versioning
page identifies **2026-07-28** as the current protocol. The corresponding Tasks
extension lets a tool call return a task handle and later yield results through
polling; both sides must support the extension. The earlier **2025-11-25** spec
already introduced experimental tasks. Therefore a silent, blocking
`wait_for_reply` is one problematic design, not an intrinsic limitation of MCP.
The CLI remains a reasonable choice because the spec reports one MCP slot and
unverified client support across the harnesses. Protocol availability does not
prove those harnesses expose asynchronous task handles or avoid their own idle
timers. Verify the deployed clients before changing this decision.
[Current MCP version](https://modelcontextprotocol.io/docs/2026-07-28/learn/versioning),
[2026 Tasks extension](https://tasks.extensions.modelcontextprotocol.io/specification/2026-07-28/tasks),
[2025 experimental tasks](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks).

For a custom v1, the useful boundary is **advisory routing with enforced task
accounting**. Specify acceptance, completion, cancellation, stale reports, and
handoff ownership deterministically. Give each logical dispatch a stable identity
across retries. Prove complete replay and recovery after ambiguous publication
before calling SQLite disposable; a local outbox is durable state if unpublished
intent exists only there. Start with one implementer, an independent reviewer, and
a deterministic build/test command on the build machine. Add an LLM build agent
only if it contributes diagnosis or decisions beyond running that command.

Measure whether this beats the existing workflow: accepted changes per hour,
human interventions, missed/duplicate handoffs, regression rate, and tokens or
cost per accepted change. Anthropic's **2025-06-13** engineering report found high
token overhead in its multi-agent research system and identified tightly coupled
coding work as less readily parallelizable. That is a useful caution, not a cost
prediction for these four harnesses in September 2026. Use independent review and
machine-specific execution first, then expand based on this fleet's own results.
[Anthropic's engineering report](https://www.anthropic.com/engineering/multi-agent-research-system).
