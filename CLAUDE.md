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
