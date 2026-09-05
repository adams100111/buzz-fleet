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
  `goose`).
- ~~CLI had no availability check/hint on `--harness`~~ — done: added
  `buzz-fleet harness list` and `buzz-fleet harness install <name>`
  (`src/buzz_fleet/harnesses.py`'s `install_adapter`), plus a matching
  "Install adapter" button in the TUI form next to the harness dropdown
  (visible whenever the selected harness isn't `available`, hides itself on
  success). Deliberately scoped opt-in, not baked into `get.sh`/
  `install.sh` — see the ruling on this in the session that added it:
  auto-installing npm packages as part of the core binary installer would
  make Node/npm a hidden hard dependency of something that today needs
  nothing but `curl`/`sha256sum`, and would install harnesses nobody asked
  for. `goose` has no automated install path (not an npm package) — `harness
  install goose` errors clearly rather than fabricating a command.
- ~~"No templates found" empty-state undocumented~~ — done, folded into the
  same TUI section edit above.
- ~~No way to cancel the create/edit form or close the log view~~ — real,
  pre-existing bug, not just missing docs: neither `AgentFormScreen` nor
  `LogsScreen` had ANY binding to leave without acting (no escape, no cancel
  button) since the original v1 build — Textual's default `App` does not
  bind escape to "pop the screen" on its own. Fixed: both screens now bind
  `escape` (`action_cancel`/`action_close` → `self.app.pop_screen()`), shown
  automatically in each screen's `Footer` the same way the dashboard's
  `c`/`u`/`x`/`l` bindings already were. Documented in `### Manage agents
  (TUI)`.
- **No worked example of populating the templates directory.** The README
  names the two supported formats but never shows an actual example — e.g.
  copying one of `buzz-deploy`'s existing `packs/*/personas/*.persona.md`
  files in. Worth a one-line `cp`/`ln` example once that flow's been used
  for real at least once.
- **No CHANGELOG.** Three releases in (`v0.1.0`/`v0.2.0`/`v0.3.0`) with no
  changelog file — not urgent, but the "Releasing a new version" README
  section could at least point at GitHub Releases' auto-generated notes if
  nothing more structured is wanted.
