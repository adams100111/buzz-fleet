# buzz-fleet — notes for AI agents

## Self-healing runtime (`AgentManager.ensure_runtime_ready`)

`ensure_runtime_ready()` is the single entry point for "make sure a
`buzz-agent@*` unit can actually run" — called from `create_agent`,
`update_agent`, the dashboard's every refresh, and `agent list`. It exists
because four real, previously-undiscovered incidents were found by
actually running a live agent end-to-end, not by code review:

1. **`buzz-acp` itself was never installed anywhere.** buzz-fleet's own
   installer (`get.sh`/`install.sh`) only ever installed `buzz-fleet`/
   `buzz-fleet-signer` — the binary every `buzz-agent@*.service` unit execs
   didn't exist on a machine that never separately built it, crash-looping
   775+ times with `status=203/EXEC`. Fixed by `buzz_acp.py`: downloads
   Sprig (`block/sprout`'s rolling `sprig-latest` GitHub release — the only
   standalone, non-Tauri-bundled distribution of `buzz-acp` that exists) to
   `~/.local/share/buzz-fleet/bin/buzz-acp`, a per-user path (not
   `/usr/local/bin`) specifically so this needs no sudo and can happen
   silently. `TEMPLATE_UNIT`'s `ExecStart` points there now.
2. **systemd's own PATH excludes version-manager install dirs.** Even
   with a harness adapter genuinely installed (`npm install -g
   @agentclientprotocol/codex-acp`, say), systemd `--user`'s fixed, minimal
   PATH (`/usr/local/bin:/usr/bin:...`) won't include wherever mise/nvm/
   asdf/volta actually put it. Fixed by `harnesses.resolve_adapter_command`:
   resolves to an absolute path via `shutil.which()` at the point
   `buzz-fleet` itself writes the env file (inheriting the *user's* PATH),
   not inside the unit file.
3. **No agent owner was ever configured.** `buzz-acp`'s own default is
   `respond_to=owner-only` — with `BUZZ_ACP_AGENT_OWNER` never set, every
   agent buzz-fleet ever created ran "successfully" while silently dropping
   100% of events, forever. Fixed by deriving the community's owner pubkey
   from its already-known admin nsec (`buzz-fleet-signer pubkey-from-nsec`,
   a new subcommand) at `connect` time, with `AgentManager.
   _ensure_owner_pubkey()` backfilling it (and persisting) for any
   `Community` saved before this field existed.
4. **Agents were invisible to Buzz Desktop's actual Agents-view
   pipeline.** `create_agent` previously only ever published a bare
   `kind:9030` relay-membership event — nothing Desktop's Agents view
   actually reads. Fixed by `AgentManager._sync_visibility()`
   (`manager.py`), called from `create_agent`, `update_agent`, and
   `ensure_runtime_ready` alike, publishing the real kind:0 profile,
   kind:9000 channel-join, kind:10100 add-policy, and kind:30177
   managed-agent record Desktop expects (content built by `visibility.py`,
   published via 7 new `buzz-fleet-signer` subcommands). The
   `visibility_managed` flag on `Agent` permanently exempts any agent
   created before this feature shipped from ever being retroactively
   backfilled — `ensure_runtime_ready()` and `update_agent()` both check it
   before touching visibility state at all.

All four heal automatically — no CLI flag, no doc a user has to know to
run. `ensure_runtime_ready()` only rewrites/restarts an agent when
something it actually needs changed (a resolved command differs from
what's on disk, or the owner pubkey was just backfilled) — never on an
already-healthy call, so it's cheap to call unconditionally and often.

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
  - `team_instructions` (`BUZZ_ACP_TEAM_INSTRUCTIONS`) is a real `Agent`
    field that existed since the original build but had zero exposure
    anywhere (no TUI input, no CLI flag) until it was wired up alongside
    bundling `personas/` — not part of `.persona.md`'s own schema at all;
    `personas.py`'s `_sibling_pack_instructions` reads it from a
    `pack_instructions.md` file living in the *same directory* as the
    `.persona.md` files (buzz-fleet's own convention — simpler than
    `buzz-deploy`'s, which nests personas one directory below
    `pack_instructions.md` instead of alongside it; see `personas/README.md`).

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
- ~~No worked example of populating the templates directory~~ — done, and
  taken further than a doc example: `personas/` is now bundled into the
  repo itself (starter templates, sourced from `buzz-deploy`'s `developers`
  pack) and auto-seeded into `~/.config/buzz-fleet/personas/` by
  `scripts/get.sh` on first install only (never overwrites an existing
  directory — see `gs_seed_personas`). The release workflow packages
  `personas/` into `personas.tar.gz`, checksummed alongside the binaries.
- ~~System prompt / Team instructions used single-line `Input` widgets~~ —
  real bug, not just missing docs: real persona content (`prompt_body`,
  `pack_instructions.md`) is routinely several paragraphs. `Input` can't
  render a newline — the system-prompt field silently truncated to its
  first line, and the team-instructions field (visually taller due to
  Textual's cursor/scroll bookkeeping for a value containing `\n`) rendered
  as a floating box that broke out of its own section and overlapped
  everything below it, which is almost certainly also what made the
  harness `Select` next to it appear unclickable (a corrupted absolute-
  positioned region intercepting clicks meant for widgets under/behind it).
  Fixed: both are now `TextArea` (`agent_form.py`), each a real scrollable
  multi-line box (height 4, own scrollbar) instead of a single line that
  either truncates or corrupts. This also required changing `.form-section`
  from the default `height: 1fr` (all four sections split the screen into
  equal shares, silently clipping whatever didn't fit — how Model/Team
  instructions could go missing from view entirely) to `height: auto` (see
  `theme.py`'s `SECTION_CSS` and `AgentFormScreen.DEFAULT_CSS`) — a longer
  form now grows and the screen scrolls, rather than fixed-share sections
  clipping their own content.
- **No CHANGELOG.** Three releases in (`v0.1.0`/`v0.2.0`/`v0.3.0`) with no
  changelog file — not urgent, but the "Releasing a new version" README
  section could at least point at GitHub Releases' auto-generated notes if
  nothing more structured is wanted.
