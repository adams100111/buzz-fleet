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
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

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
    try:
        return PersonaTemplate(
            display_name=display_name,
            harness=frontmatter.get("runtime"),
            model=frontmatter.get("model"),
            prompt_body=body,
            source_path=path,
        )
    except ValidationError:
        return None


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
        )
    except ValidationError:
        return None


def discover_personas(root: Path) -> tuple[list[PersonaTemplate], int]:
    root.mkdir(parents=True, exist_ok=True)
    templates: list[PersonaTemplate] = []
    skipped = 0

    for path in sorted(root.glob("**/*.persona.md")):
        template = parse_persona_md(path)
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
