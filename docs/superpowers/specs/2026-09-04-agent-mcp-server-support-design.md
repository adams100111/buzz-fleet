# Per-agent MCP server support — future work (not yet designed)

Status: placeholder. Captured during the persona-picker feature's grill
session (2026-09-04) so the gap isn't silently dropped; needs its own
brainstorming pass before implementation.

## Problem statement

`.persona.md` files (the `buzz-persona` pack format) can declare
`mcp_servers` (e.g. the Laravel persona's `boost` MCP server), `triggers`,
`thread_replies`, `subscribe`, and `broadcast_replies` — none of which have
any equivalent anywhere in `buzz-fleet` today, for any agent, template-sourced
or not. `systemd.write_agent_files` only ever writes `BUZZ_PRIVATE_KEY`,
`BUZZ_RELAY_URL`, `BUZZ_ACP_AGENT_COMMAND`, `BUZZ_ACP_SYSTEM_PROMPT_FILE`,
`BUZZ_ACP_TEAM_INSTRUCTIONS`, and the two API-key env vars — there is no
mechanism to attach an MCP server to a `buzz-fleet`-managed headless agent
at all.

The official `buzz-agent-snapshot` format's `provider` field is the same
shape of gap from the other direction: `buzz-acp` has no dedicated
`--provider`/env-var flag for it (confirmed via a direct grep of
`crates/buzz-acp/src/config.rs` — the only `provider` hits are an unrelated
"ACP provider sessions are scoped in channels" option, and a code comment
noting providers are actually selected per-harness via arbitrary "per-persona
env vars to inject at agent spawn time," e.g. `GOOSE_PROVIDER`). That's really
a generic env-var-passthrough mechanism, not a `provider` field — `buzz-fleet`
has neither piece today.

The persona-picker feature (see the main plan/design docs) deliberately
drops `mcp_servers`/`triggers`/`thread_replies`/`subscribe`/
`broadcast_replies` when importing a `.persona.md` as a template, and drops
`provider` (along with `avatarDataUrl`/`avatarUrl`/`sourceIsBuiltin`/
`namePool`/`description`/`about`) when importing a `.agent.json` snapshot,
rather than attempting to solve either here. By contrast, `model`,
`parallelism`, `idle_timeout_seconds`, `max_turn_duration_seconds`, and
`respond_to_allowlist` (from `.agent.json`) DO get wired for real as part of
the persona-picker feature — they're genuine `buzz-acp` settings
(`BUZZ_ACP_MODEL`/`BUZZ_ACP_AGENTS`/`BUZZ_ACP_IDLE_TIMEOUT`/
`BUZZ_ACP_MAX_TURN_DURATION`/`BUZZ_ACP_RESPOND_TO_ALLOWLIST`), unlike the
fields listed above.

## Why this is a separate spec, not a checkbox on the persona-picker feature

Wiring real MCP server support into a headless `buzz-acp`-driven agent is a
meaningfully larger design question than "parse one more YAML field":

- Does `buzz-acp` itself support attaching arbitrary MCP servers to the
  agent process it spawns, or does this need to happen at the harness
  (`claude-agent-acp`/`codex-acp`) level instead? Needs verification
  against `buzz-acp`'s actual config surface (`crates/buzz-acp/src/config.rs`)
  before assuming either way.
- `Agent`'s data model has no field for this today — would need a new
  `mcp_servers: list[McpServerConfig]` (or similar) on the Pydantic model,
  with its own secret-handling story (an MCP server's own env vars, e.g. an
  API key for Laravel Boost, may themselves be secrets needing the same
  `0600`-file treatment already established for `BUZZ_PRIVATE_KEY`).
- `systemd.write_agent_files` would need to emit whatever config format the
  chosen mechanism actually reads (more env vars? a generated JSON/TOML
  config file alongside the `.env`?).
- The CLI (`agent create`/`agent update`) and TUI (`AgentFormScreen`) would
  both need a real way to express "attach these MCP servers," not just a
  silent pass-through from a persona file.

## Not addressed here

This file intentionally does not propose a design — it exists to record
that the gap is known and documented, per the persona-picker feature's own
grill session, rather than left as a silent assumption. Run this through
`superpowers:brainstorming` (or the `grilling` skill) properly before
building it.
