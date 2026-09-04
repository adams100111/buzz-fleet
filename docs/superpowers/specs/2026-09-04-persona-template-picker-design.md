# Persona/agent-snapshot template picker — design spec

Status: approved, ready for implementation plan. Reached via the `grilling`
skill across four rounds (formats, dependencies, defaulting, directory
bootstrap, overwrite semantics, model field, MCP scope, edit-mode boundary,
unsupported-file visibility, format validation, official-schema field
mapping, form layout) plus two direct fact-finding passes (env-var mapping
against `buzz-acp`, `respond_to`/`respond_to_allowlist` coupling).

## Problem

`buzz-fleet`'s create-agent form (TUI) always starts blank. `buzz-deploy`
already has hand-written `.persona.md` packs (Laravel/Inertia+React/.NET/RN/
iOS/Android developer personas) and Buzz Desktop can export/import
`.agent.json` "agent snapshots" — neither is usable as a starting point for
a new `buzz-fleet`-managed headless agent today. Separately, `Agent.model`
exists on the Pydantic model but is unreachable from any CLI flag or TUI
input, and is never written to the agent's env file at all — a pre-existing
gap this feature also closes.

## Template sources

A configurable directory, default `~/.config/buzz-fleet/personas` (created
automatically if missing). Recursively globbed for two real formats:

- **`.persona.md`** — the `buzz-persona` pack format (YAML frontmatter +
  markdown body). Required: `display_name`. Consumed: `display_name`,
  `runtime` (→ harness), `model`, the markdown body (→ prompt).
- **`.agent.json`** — Buzz Desktop's `buzz-agent-snapshot` v1 format (plain
  JSON; the PNG-embedded `.agent.png` variant is deliberately NOT parsed —
  see below). Validated strictly: `format == "buzz-agent-snapshot"` and
  `version == 1`, or the file is skipped. Consumed:
  `profile.displayName` (→ display_name — not `definition.name`, which is
  more like an internal template id), `definition.runtime` (→ harness),
  `definition.model`, `definition.systemPrompt` (→ prompt),
  `definition.parallelism`, `definition.idleTimeoutSeconds`,
  `definition.maxTurnDurationSeconds`.

`.agent.png` files are counted, not parsed, and folded into a single "N
unsupported/unparseable files found" notice alongside any `.persona.md`/
`.agent.json` file that fails to parse — never a hard error, never silently
invisible either.

## Fields explicitly dropped (documented in `CLAUDE.md` + the MCP-support
spec stub, not silently lost)

- `.persona.md`: `mcp_servers`, `triggers`, `thread_replies`, `subscribe`,
  `broadcast_replies` — no attachment mechanism exists in `buzz-fleet` at
  all yet; separate future spec.
- `.agent.json`: `definition.provider` (no `buzz-acp` env var exists for
  it — provider selection happens per-harness via an arbitrary env-var
  passthrough mechanism `buzz-fleet` doesn't have), `definition.
  sourceIsBuiltin`, `definition.namePool`, `profile.avatarDataUrl`/
  `avatarUrl`, `profile.about` (Desktop-GUI-only, no `buzz-acp` backing,
  and `Agent` has no `description`-shaped field to put it in — greenfield
  app, deliberately not adding one for a field nothing reads).
- `.agent.json`'s `definition.respondToAllowlist` is parsed by nothing —
  deliberately never carried into `PersonaTemplate` at all, matching Buzz
  Desktop's own import dialog, which defaults `keepAllowlist` to `false`
  ("safe default per spec": imported pubkeys are from a different
  community/relay and are meaningless, or actively dangerous, in a new
  one). The **field itself** exists on `Agent` (see below) — only
  *pre-filling it from a template* is refused.

## New real `Agent` fields (wired to genuine `buzz-acp` settings, verified
against `crates/buzz-acp/src/config.rs`)

| `Agent` field | env var | notes |
|---|---|---|
| `model: str \| None` (already existed) | `BUZZ_ACP_MODEL` | pre-existing gap: field existed, was never emitted — fixed here |
| `parallelism: int \| None` | `BUZZ_ACP_AGENTS` | `buzz-acp` default: 1 |
| `idle_timeout_seconds: int \| None` | `BUZZ_ACP_IDLE_TIMEOUT` | `buzz-acp` default: unset |
| `max_turn_duration_seconds: int \| None` | `BUZZ_ACP_MAX_TURN_DURATION` | `buzz-acp` has its own internal default when unset |
| `respond_to_allowlist: list[str] \| None` | `BUZZ_ACP_RESPOND_TO_ALLOWLIST` + `BUZZ_ACP_RESPOND_TO` | see ruling below |

All four are optional; blank/`None` means "don't write the env var, let
`buzz-acp` use its own default" — never hardcode `buzz-acp`'s own defaults
into `buzz-fleet`.

**Ruling (fact, not a user decision — ruled here rather than adding a 6th
form field):** `buzz-acp`'s `respond_to_allowlist` is only consulted "when
respond_to == Allowlist" (`BUZZ_ACP_RESPOND_TO`, default `owner-only`) — a
separate env var from the allowlist itself. `buzz-fleet` does not expose a
`respond_to` mode selector at all (out of scope — nothing asked for it).
Instead: `write_agent_files` writes `BUZZ_ACP_RESPOND_TO=allowlist`
automatically whenever `Agent.respond_to_allowlist` is a non-empty list,
and omits both env vars when it's `None`/empty. One form field, correct
behavior, no silently-inert allowlist.

## TUI

`AgentFormScreen`, **create mode only** (the template picker does not
appear in edit mode — editing an existing agent already has its own
narrower, settled semantics). One scrollable screen (Textual screens
scroll natively; splitting into an "advanced" second screen was considered
and rejected — needless navigation for fields most agents leave blank):

- Persona/template `Select` (blank = no template, the prior "always blank"
  behavior unchanged)
- Display name `Input`
- Harness `Select` (fixes the current hardcoded `"claude"`)
- Prompt `Input`
- Model `Input`
- Parallelism `Input` (blank or integer)
- Idle timeout seconds `Input` (blank or integer)
- Max turn duration seconds `Input` (blank or integer)
- Respond-to allowlist `Input` (blank, or comma-separated pubkeys — never
  pre-filled from a template, see above)

Selecting a template **overwrites** every pre-fillable field's current
value (display name, harness, prompt, model, parallelism, idle timeout,
max turn duration) — not a merge. Re-selecting a different template
overwrites again. This mirrors "prefilled with the selected template with
ability to directly submit or make some changes" — the template is a
one-time prefill convenience, not a live binding; submitting always
creates an **inline** `SystemPromptSource` (matching the TUI form's
existing behavior — it has never created `persona_file`-sourced agents;
only the CLI's `--prompt-file` does that).

## Pre-existing bug fixed in the same pass

`systemd.resolve_prompt_text` currently returns a `persona_file` source's
**entire raw file content, including YAML frontmatter**, as the system
prompt text — this leaks frontmatter into every `--prompt-file`-created
agent's actual prompt sent to `buzz-acp`. Fixed to strip a leading
`---...---` block before returning the body, independent of the new
`.persona.md` parser in `personas.py` (a different code path: CLI
`--prompt-file` vs. the TUI template picker's own parse-then-prefill flow).

## CLI

`agent create`/`agent update` gain `--model`, `--parallelism`,
`--idle-timeout-seconds`, `--max-turn-duration-seconds`,
`--respond-to-allowlist` (comma-separated) — matching the TUI's fields,
manual entry only (no template-picker equivalent on the CLI; the picker
was explicitly asked for as a TUI feature).
