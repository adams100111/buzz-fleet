# buzz-fleet — notes for AI agents

## Known gaps / future work

- **No per-agent MCP server support, and no generic per-agent env-var
  passthrough.** `.persona.md` files can declare `mcp_servers`, `triggers`,
  `thread_replies`, `subscribe`, and `broadcast_replies`, none of which have
  any equivalent in `buzz-fleet` for any agent. The official
  `buzz-agent-snapshot` format's `provider` field has the same problem from
  the other direction: `buzz-acp` has no dedicated `--provider`/env-var flag
  for it at all — provider selection happens per-harness via arbitrary
  "per-persona env vars to inject at agent spawn time" (e.g. `GOOSE_PROVIDER`),
  which is really a generic env-var-passthrough mechanism `buzz-fleet` doesn't
  have. All of these are silently dropped when importing a persona/template
  (see the persona-picker feature) rather than solved here. Not yet designed;
  see `docs/superpowers/specs/2026-09-04-agent-mcp-server-support-design.md`
  before attempting either — it needs its own brainstorming pass, not an
  incremental patch onto the persona-picker feature.
  - Fields that ARE real, wireable `buzz-acp` settings and got a proper home
    as part of the persona-picker feature instead of landing in this bucket:
    `model` (`BUZZ_ACP_MODEL`), `parallelism` (`BUZZ_ACP_AGENTS`),
    `idle_timeout_seconds` (`BUZZ_ACP_IDLE_TIMEOUT`),
    `max_turn_duration_seconds` (`BUZZ_ACP_MAX_TURN_DURATION`), and
    `respond_to_allowlist` (`BUZZ_ACP_RESPOND_TO_ALLOWLIST` — not to be
    confused with `BUZZ_ACP_ALLOWED_RESPOND_TO`, an unrelated
    deployment-level gate on which `--respond-to` *modes* are permitted at
    all, which `buzz-fleet` has no reason to expose per-agent).
  - Desktop-GUI-only fields with no `buzz-acp` backing at all, and no plan
    to add one: `avatarDataUrl`/`avatarUrl`, `sourceIsBuiltin`, `namePool`,
    `description`/`about` (dropped from `buzz-fleet`'s `Agent` model
    entirely — there's nowhere for it to live or do anything).

## Documentation debt (not yet reflected in README.md)

Running list, added to as work lands ahead of the docs catching up. Clear an
item only once README.md (or the file it names) actually reflects it — don't
delete on "I'll remember," this list exists because that doesn't survive
compaction.

- ~~Harness auto-detection undocumented~~ — done: `### Manage agents (TUI)`
  now documents the `available`/`adapter missing`/`not installed` states, the
  exact binary checked per harness, and install commands for each adapter
  (verified against `buzz`'s own `catalog.rs`/`presets.rs`/
  `runtimeAvailabilityWarning.ts`: `npm install -g
  @agentclientprotocol/claude-agent-acp`, `@agentclientprotocol/codex-acp`
  — must be 1.x — `@earendil-works/pi-coding-agent` + `pi-acp`, plain
  `goose`). CLI side still has no availability check/hint at all on
  `agent create --harness` — that's a real, separate follow-up (behavior gap,
  not just docs): should the CLI warn/print the same install hint the TUI's
  label implies, or is silently letting you pick an unavailable harness on
  the CLI acceptable since it's already the "manual, no hand-holding"
  surface? Not decided.
- ~~"No templates found" empty-state undocumented~~ — done, folded into the
  same TUI section edit above.
- **No worked example of populating the templates directory.** The README
  names the two supported formats but never shows an actual example — e.g.
  copying one of `buzz-deploy`'s existing `packs/*/personas/*.persona.md`
  files in. Worth a one-line `cp`/`ln` example once that flow's been used
  for real at least once.
- **No CHANGELOG.** Three releases in (`v0.1.0`/`v0.2.0`/`v0.3.0`) with no
  changelog file — not urgent, but the "Releasing a new version" README
  section could at least point at GitHub Releases' auto-generated notes if
  nothing more structured is wanted.
