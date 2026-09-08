---
status: proposed
---
# Frontends talk to a long-lived core process over JSON-RPC, and never receive a secret

A user interface for buzz-fleet — the planned OpenTUI frontend, a herdr plugin action, or anything later — does not call the `buzz-fleet` binary once per operation. It spawns `buzz-fleet daemon --stdio` once and speaks newline-delimited JSON-RPC 2.0 to it for the life of the session, receiving unsolicited notifications for agent status changes rather than polling. We chose this because the shipped PyInstaller `--onefile` binary takes **1.0–1.2 s to start** (measured warm, three runs, on `--version`, which does no work) as it unpacks 25 MB on every invocation: a one-shot design costs a second per keystroke-triggered action and a status-polling list would spend its life spawning Python. JSON-RPC 2.0 rather than a bespoke envelope because MCP and the Language Server Protocol both use it over stdio, so both ends get libraries instead of hand-rolled framing. **No secret value crosses this boundary in either direction**: the core redacts on the way out, and a client expresses "leave this unchanged" by omitting the field, never by echoing a mask back — key material therefore never enters a frontend process, and cannot surface in its logs or crash dumps.

## Considered options

- One-shot CLI invocations with `--json`: the simplest design and how the CLI already works, rejected on the measured startup cost alone.
- A Unix socket in `$XDG_RUNTIME_DIR` shared by several clients: better if a herdr plugin and the TUI should share a warm cache, deferred because nothing needs it yet and it adds lifecycle, permissions, and stale-socket handling.
- The herdr `{id, result, error}` envelope: familiar to this project's author, which is not a good enough reason to design a protocol when a standard one fits.
- Sending secrets to the frontend so an edit form can round-trip them: rejected. The existing Textual form already masks values as `********` and restores them by string match (`tui/screens/agent_form.py:497`), which silently replaces any value a user genuinely sets to `********`. Across a process boundary the frontend cannot tell "unchanged" from "typed the mask", so the mask must go rather than be carried over.

## Consequences

The CLI handlers must stop writing to stdout: `cli/app.py` and `cli/fleet_commands.py` contain 49 stdout writes today, and any one of them reaching a stdio client desynchronises the protocol. A handler layer that returns values, with the CLI formatting and the daemon serialising, becomes a prerequisite rather than a cleanup — and it is what makes "one implementation, two front doors" true instead of aspirational. Diagnostics go to stderr.

The CLI stays fully usable standalone and is not routed through the daemon: agents run `buzz-fleet task ack` inside their own systemd units where no daemon exists. Two writers therefore reach local state concurrently, so writes become atomic (temp file in the same directory, `fsync`, `os.replace`, `fsync` the directory) and read-modify-write sequences take an advisory per-community lock. Neither existed before.

Daemon lifetime and orphan handling are not settled here: one daemon per frontend is assumed, and what happens when a frontend dies without closing stdin is still open.
