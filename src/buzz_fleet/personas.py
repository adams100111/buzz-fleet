"""Discover and parse persona/agent-snapshot template files for the create-agent form.

Two source formats, both real: `.persona.md` (the `buzz-persona` pack format —
YAML frontmatter + markdown body) and `.agent.json` (Buzz Desktop's
`buzz-agent-snapshot` v1 export). `.agent.png` embeds the same JSON in a PNG
`tEXt` chunk — deliberately not parsed here (counted as skipped instead); see
the design spec for why.

`respondToAllowlist` from a `.agent.json` file is intentionally never read
into `PersonaTemplate` at all — imported pubkeys are from a different
community/relay and are meaningless (or dangerous) in a new one, matching
Buzz Desktop's own import dialog default.

A `.persona.md` pack (a directory of personas sharing one team) may include
a sibling `pack_instructions.md` — team-wide discipline every persona in
that directory inherits, kept separate from stack-specific expertise so it's
defined once rather than repeated per persona and left to drift. When
present, its content becomes `PersonaTemplate.team_instructions`, which the
form maps onto `Agent.team_instructions` (`BUZZ_ACP_TEAM_INSTRUCTIONS`) —
not part of `.agent.json`, which has no pack concept at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError, field_validator

from buzz_fleet.models import McpServer, validate_env_key

DEFAULT_PERSONAS_DIR = Path.home() / ".config" / "buzz-fleet" / "personas"


class PersonaTemplate(BaseModel):
    display_name: str
    harness: str | None = None
    model: str | None = None
    prompt_body: str
    source_path: Path
    parallelism: int | None = None
    idle_timeout_seconds: int | None = None
    max_turn_duration_seconds: int | None = None
    team_instructions: str | None = None
    description: str | None = None
    # Generic per-agent env-var passthrough (spec 5.13) — a `.persona.md`'s
    # own `env:` frontmatter block, e.g. a harness-specific provider
    # selector (GOOSE_PROVIDER) that has no other home. Plain strings here,
    # not secrets — a persona file is not a safe place to keep a real
    # secret; Agent.env coerces each value into a SecretStr once it's
    # actually attached to an agent.
    env: dict[str, str] | None = None
    # buzz-acp supports exactly one MCP server — only the first entry of a
    # persona's `mcp_servers:` block is ever imported; see _build_mcp_server.
    mcp_server: McpServer | None = None

    @field_validator("env")
    @classmethod
    def _env_keys_are_safe(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        # Same rule as `Agent.env` (models.validate_env_key) -- a persona's
        # own `env:` frontmatter block is exactly the untrusted-pack input
        # that rule exists for. Raising here (inside `PersonaTemplate`'s own
        # construction) means `parse_persona_md`'s existing
        # `except ValidationError: return None` already surfaces this the
        # same way it surfaces every other malformed field -- the whole file
        # is skipped, not counted, never a crash reaching `discover_personas`.
        for key in value or {}:
            validate_env_key(key)
        return value


def _build_mcp_server(raw: object) -> McpServer | None:
    """Build the (single) `McpServer` a persona declares, or None if it
    declares none. Raises `ValueError` — deliberately NOT caught by
    `parse_persona_md`'s own try/except blocks, so it propagates all the way
    out to the caller — when a persona declares more than one, since
    buzz-acp itself supports exactly one stdio MCP server.
    """
    if not isinstance(raw, list) or not raw:
        return None
    if len(raw) > 1:
        raise ValueError(f"persona declares {len(raw)} MCP servers; buzz-acp supports one")
    entry = raw[0]
    if not isinstance(entry, dict):
        return None
    name, command = entry.get("name"), entry.get("command")
    if not isinstance(name, str) or not isinstance(command, str):
        return None
    args = entry.get("args") or []
    env = entry.get("env") or {}
    if not isinstance(args, list) or not isinstance(env, dict):
        return None
    return McpServer(name=name, command=command, args=list(args), env=dict(env))


def _parse_env_block(raw: object) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    parsed = {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
    return parsed or None


def _sibling_pack_instructions(path: Path) -> str | None:
    sibling = path.parent / "pack_instructions.md"
    if not sibling.is_file():
        return None
    try:
        return sibling.read_text()
    except (UnicodeDecodeError, OSError):
        return None


def parse_persona_md(path: Path) -> PersonaTemplate | None:
    try:
        raw = path.read_text()
    except (UnicodeDecodeError, OSError):
        return None
    if not raw.startswith("---\n"):
        return None
    closing = raw.find("\n---\n", 4)
    if closing == -1:
        return None
    frontmatter_text = raw[4:closing]
    body = raw[closing + len("\n---\n") :]
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError:
        return None
    if not isinstance(frontmatter, dict):
        return None
    display_name = frontmatter.get("display_name")
    if not display_name or not isinstance(display_name, str):
        return None
    # Deliberately outside the try/except ValidationError below: a persona
    # declaring more than one MCP server must raise all the way out to the
    # caller (buzz-acp supports exactly one), not be swallowed the way an
    # ordinary malformed-field problem is.
    mcp_server = _build_mcp_server(frontmatter.get("mcp_servers"))
    try:
        return PersonaTemplate(
            display_name=display_name,
            harness=frontmatter.get("runtime"),
            model=frontmatter.get("model"),
            prompt_body=body,
            source_path=path,
            team_instructions=_sibling_pack_instructions(path),
            description=frontmatter.get("description"),
            env=_parse_env_block(frontmatter.get("env")),
            mcp_server=mcp_server,
        )
    except ValidationError:
        return None


def load_persona_template(path: Path) -> PersonaTemplate | None:
    """Parse a template file by its extension — `.agent.json` (buzz-agent-
    snapshot) or `.persona.md` (buzz-persona pack), otherwise. Same
    None-on-malformed-input contract as `parse_persona_md`/`parse_agent_json`,
    except a persona declaring more than one MCP server, which raises
    `ValueError` (see `_build_mcp_server`) rather than being swallowed —
    `discover_personas` is the one caller that must not let that propagate
    past a single bad file, and it catches it itself.
    """
    if path.name.endswith(".agent.json"):
        return parse_agent_json(path)
    return parse_persona_md(path)


def parse_agent_json(path: Path) -> PersonaTemplate | None:
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("format") != "buzz-agent-snapshot" or raw.get("version") != 1:
        return None
    definition = raw.get("definition")
    profile = raw.get("profile")
    if not isinstance(definition, dict) or not isinstance(profile, dict):
        return None
    display_name = profile.get("displayName")
    if not display_name or not isinstance(display_name, str):
        return None
    try:
        return PersonaTemplate(
            display_name=display_name,
            harness=definition.get("runtime"),
            model=definition.get("model"),
            prompt_body=definition.get("systemPrompt") or "",
            source_path=path,
            parallelism=definition.get("parallelism"),
            idle_timeout_seconds=definition.get("idleTimeoutSeconds"),
            max_turn_duration_seconds=definition.get("maxTurnDurationSeconds"),
            description=profile.get("about"),
        )
    except ValidationError:
        return None


def discover_personas(root: Path) -> tuple[list[PersonaTemplate], int]:
    root.mkdir(parents=True, exist_ok=True)
    templates: list[PersonaTemplate] = []
    skipped = 0

    for path in sorted(root.glob("**/*.persona.md")):
        try:
            template = parse_persona_md(path)
        except ValueError:
            # A persona declaring more than one MCP server (buzz-acp
            # supports one) must not crash the whole directory scan —
            # AgentFormScreen.compose() calls this synchronously, so one bad
            # file in a pack would otherwise take down the entire create-
            # agent screen. Counted as skipped like any other malformed file.
            template = None
        if template is None:
            skipped += 1
        else:
            templates.append(template)

    for path in sorted(root.glob("**/*.agent.json")):
        template = parse_agent_json(path)
        if template is None:
            skipped += 1
        else:
            templates.append(template)

    skipped += len(list(root.glob("**/*.agent.png")))

    return templates, skipped
