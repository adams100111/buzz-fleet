# Persona/Agent-Snapshot Template Picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persona/agent-snapshot template picker to `buzz-fleet`'s TUI create-agent form, and wire four previously-unreachable-but-real `buzz-acp` settings (`model` fix + `parallelism`, `idle_timeout_seconds`, `max_turn_duration_seconds`, `respond_to_allowlist`) into the `Agent` model, env-file writer, CLI, and TUI.

**Architecture:** A new `src/buzz_fleet/personas.py` module parses `.persona.md` (YAML frontmatter + markdown) and `.agent.json` (Buzz Desktop's `buzz-agent-snapshot` v1) files from a configurable directory into a shared `PersonaTemplate` model, and discovers them recursively (auto-creating the directory, counting-not-parsing `.agent.png`, skipping unparseable files). `Agent` gains four new optional fields; `systemd.write_agent_files` emits their `buzz-acp` env vars only when set. `AgentFormScreen` (create mode only) gains a template `Select` that prefills the rest of the form on selection, plus new inputs for the four fields and a harness `Select` (replacing a hardcoded `"claude"`). The CLI gains matching flags. A pre-existing bug in `systemd.resolve_prompt_text` (frontmatter leaking into the live prompt sent to `buzz-acp`) is fixed in the same pass.

**Tech Stack:** Python, Pydantic, Textual, Typer, PyYAML (new dependency).

**Spec:** `docs/superpowers/specs/2026-09-04-persona-template-picker-design.md`

## Global Constraints

- New dependency: `pyyaml` (add to `pyproject.toml` `[project.dependencies]`, not a dev-only group — `personas.py` is runtime code).
- `.persona.md` required field: `display_name` (missing/blank → skip the file, count as unparseable).
- `.agent.json` strict validation: skip (count as unparseable) unless top-level `format == "buzz-agent-snapshot"` and `version == 1`.
- `.agent.png` files: counted, never parsed.
- All four new `Agent` fields are `int | None` / `list[str] | None`, default `None`. `None`/empty means "omit the env var, let `buzz-acp` default" — never hardcode a `buzz-acp` default value into `buzz-fleet`.
- `respond_to_allowlist` is NEVER pre-filled from a template (not even carried into `PersonaTemplate` at all) — this is a hard rule from the spec's Buzz-Desktop-precedent ruling, not a UI nicety to relax later.
- Selecting a persona template in the TUI **overwrites** every prefillable field's current value, every time (no merge).
- The template picker appears in **create mode only** — never rendered when `AgentFormScreen` is constructed with an existing `agent`.
- Whenever `Agent.respond_to_allowlist` is a non-empty list, `write_agent_files` must also write `BUZZ_ACP_RESPOND_TO=allowlist` (ruling: `buzz-acp` only consults the allowlist in that mode). When the list is `None`/empty, neither `BUZZ_ACP_RESPOND_TO` nor `BUZZ_ACP_RESPOND_TO_ALLOWLIST` is written.
- Env var names (exact, verbatim): `BUZZ_ACP_MODEL`, `BUZZ_ACP_AGENTS` (parallelism), `BUZZ_ACP_IDLE_TIMEOUT`, `BUZZ_ACP_MAX_TURN_DURATION`, `BUZZ_ACP_RESPOND_TO_ALLOWLIST` (comma-separated), `BUZZ_ACP_RESPOND_TO`.
- Default template directory: `~/.config/buzz-fleet/personas` (create if missing — same pattern as `AGENTS_DIR` in `systemd.py`).

---

### Task 1: `Agent` model — four new fields

**Files:**
- Modify: `src/buzz_fleet/models.py`
- Test: `tests/test_models.py` (new file)

**Interfaces:**
- Produces: `Agent.parallelism: int | None`, `Agent.idle_timeout_seconds: int | None`, `Agent.max_turn_duration_seconds: int | None`, `Agent.respond_to_allowlist: list[str] | None` — all default `None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models.py
from datetime import UTC, datetime

from buzz_fleet.models import Agent, SystemPromptSource


def _base_kwargs() -> dict:
    return dict(
        id="test-agent",
        community_id="eltahir",
        display_name="Test Agent",
        harness="claude",
        private_key="nsec1x",
        public_key="a" * 64,
        system_prompt_source=SystemPromptSource(kind="inline", text="hi"),
        created_at=datetime.now(UTC),
    )


def test_new_fields_default_to_none() -> None:
    agent = Agent(**_base_kwargs())
    assert agent.parallelism is None
    assert agent.idle_timeout_seconds is None
    assert agent.max_turn_duration_seconds is None
    assert agent.respond_to_allowlist is None


def test_new_fields_round_trip_through_json() -> None:
    agent = Agent(
        **_base_kwargs(),
        parallelism=3,
        idle_timeout_seconds=120,
        max_turn_duration_seconds=600,
        respond_to_allowlist=["a" * 64, "b" * 64],
    )
    restored = Agent.model_validate_json(agent.model_dump_json())
    assert restored.parallelism == 3
    assert restored.idle_timeout_seconds == 120
    assert restored.max_turn_duration_seconds == 600
    assert restored.respond_to_allowlist == ["a" * 64, "b" * 64]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL — `TypeError` / `AttributeError`, the fields don't exist yet.

- [ ] **Step 3: Add the fields**

In `src/buzz_fleet/models.py`, in `class Agent`, after the existing `model: str | None = None` line, add:

```python
    parallelism: int | None = None
    idle_timeout_seconds: int | None = None
    max_turn_duration_seconds: int | None = None
    respond_to_allowlist: list[str] | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/buzz_fleet/models.py tests/test_models.py
git commit -m "feat: add parallelism/idle-timeout/max-turn-duration/respond-to-allowlist fields to Agent"
```

---

### Task 2: Fix `resolve_prompt_text` frontmatter-leak bug

**Files:**
- Modify: `src/buzz_fleet/systemd.py`
- Test: `tests/test_systemd.py`

**Interfaces:**
- Consumes: `Agent.system_prompt_source` (existing `SystemPromptSource` model, unchanged).
- Produces: `resolve_prompt_text(agent: Agent) -> str` — same signature, corrected behavior for `persona_file` sources whose file has YAML frontmatter.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_systemd.py`:

```python
def test_resolve_prompt_text_strips_frontmatter_from_persona_file(tmp_path: Path) -> None:
    from buzz_fleet.systemd import resolve_prompt_text

    persona_path = tmp_path / "laravel.persona.md"
    persona_path.write_text(
        "---\n"
        "display_name: Laravel Backend Dev\n"
        "runtime: claude\n"
        "---\n"
        "You are the Laravel dev.\n"
    )
    agent = _agent().model_copy(
        update={"system_prompt_source": SystemPromptSource(kind="persona_file", path=persona_path)}
    )

    assert resolve_prompt_text(agent) == "You are the Laravel dev.\n"


def test_resolve_prompt_text_returns_whole_file_when_no_frontmatter(tmp_path: Path) -> None:
    from buzz_fleet.systemd import resolve_prompt_text

    plain_path = tmp_path / "plain.md"
    plain_path.write_text("Just a plain prompt, no frontmatter.\n")
    agent = _agent().model_copy(
        update={"system_prompt_source": SystemPromptSource(kind="persona_file", path=plain_path)}
    )

    assert resolve_prompt_text(agent) == "Just a plain prompt, no frontmatter.\n"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_systemd.py -k frontmatter -v`
Expected: FAIL — the first test fails because the frontmatter block is still present in the returned text.

- [ ] **Step 3: Fix `resolve_prompt_text`**

In `src/buzz_fleet/systemd.py`, replace:

```python
    assert source.path is not None
    return source.path.read_text()
```

with:

```python
    assert source.path is not None
    raw = source.path.read_text()
    if raw.startswith("---\n"):
        closing = raw.find("\n---\n", 4)
        if closing != -1:
            return raw[closing + len("\n---\n") :]
    return raw
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_systemd.py -k frontmatter -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add src/buzz_fleet/systemd.py tests/test_systemd.py
git commit -m "fix: strip YAML frontmatter from persona_file prompts before sending to buzz-acp"
```

---

### Task 3: `write_agent_files` — emit the five new env vars

**Files:**
- Modify: `src/buzz_fleet/systemd.py`
- Test: `tests/test_systemd.py`

**Interfaces:**
- Consumes: `Agent.model`, `Agent.parallelism`, `Agent.idle_timeout_seconds`, `Agent.max_turn_duration_seconds`, `Agent.respond_to_allowlist` (from Task 1).
- Produces: no signature change to `write_agent_files` — it already receives the full `agent` object.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_systemd.py`:

```python
def test_write_agent_files_emits_model_when_set(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path)
    agent = _agent().model_copy(update={"model": "claude-sonnet-5"})

    write_agent_files(agent, _community(), anthropic_api_key=None, openai_api_key=None)

    env_content = agent_env_path(agent.id).read_text()
    assert "BUZZ_ACP_MODEL=claude-sonnet-5" in env_content


def test_write_agent_files_omits_optional_fields_when_unset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path)
    agent = _agent()

    write_agent_files(agent, _community(), anthropic_api_key=None, openai_api_key=None)

    env_content = agent_env_path(agent.id).read_text()
    for key in (
        "BUZZ_ACP_MODEL",
        "BUZZ_ACP_AGENTS",
        "BUZZ_ACP_IDLE_TIMEOUT",
        "BUZZ_ACP_MAX_TURN_DURATION",
        "BUZZ_ACP_RESPOND_TO",
        "BUZZ_ACP_RESPOND_TO_ALLOWLIST",
    ):
        assert key not in env_content


def test_write_agent_files_emits_parallelism_idle_timeout_max_turn_duration(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path)
    agent = _agent().model_copy(
        update={"parallelism": 3, "idle_timeout_seconds": 120, "max_turn_duration_seconds": 600}
    )

    write_agent_files(agent, _community(), anthropic_api_key=None, openai_api_key=None)

    env_content = agent_env_path(agent.id).read_text()
    assert "BUZZ_ACP_AGENTS=3" in env_content
    assert "BUZZ_ACP_IDLE_TIMEOUT=120" in env_content
    assert "BUZZ_ACP_MAX_TURN_DURATION=600" in env_content


def test_write_agent_files_sets_respond_to_allowlist_mode_when_list_non_empty(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path)
    agent = _agent().model_copy(update={"respond_to_allowlist": ["a" * 64, "b" * 64]})

    write_agent_files(agent, _community(), anthropic_api_key=None, openai_api_key=None)

    env_content = agent_env_path(agent.id).read_text()
    assert f"BUZZ_ACP_RESPOND_TO_ALLOWLIST={'a' * 64},{'b' * 64}" in env_content
    assert "BUZZ_ACP_RESPOND_TO=allowlist" in env_content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_systemd.py -k "emits_model or omits_optional or parallelism_idle or respond_to_allowlist_mode" -v`
Expected: FAIL — none of these env vars are written yet.

- [ ] **Step 3: Implement**

In `src/buzz_fleet/systemd.py`, in `write_agent_files`, after the existing `if agent.team_instructions:` block and before the `if anthropic_api_key:` block, add:

```python
    if agent.model:
        lines.append(f"BUZZ_ACP_MODEL={agent.model}")
    if agent.parallelism is not None:
        lines.append(f"BUZZ_ACP_AGENTS={agent.parallelism}")
    if agent.idle_timeout_seconds is not None:
        lines.append(f"BUZZ_ACP_IDLE_TIMEOUT={agent.idle_timeout_seconds}")
    if agent.max_turn_duration_seconds is not None:
        lines.append(f"BUZZ_ACP_MAX_TURN_DURATION={agent.max_turn_duration_seconds}")
    if agent.respond_to_allowlist:
        # buzz-acp only consults the allowlist when respond_to == "allowlist"
        # (BUZZ_ACP_RESPOND_TO, default "owner-only") — set both together so
        # the allowlist is never silently inert.
        lines.append("BUZZ_ACP_RESPOND_TO=allowlist")
        lines.append(f"BUZZ_ACP_RESPOND_TO_ALLOWLIST={','.join(agent.respond_to_allowlist)}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_systemd.py -v`
Expected: PASS (all tests in the file, including pre-existing ones)

- [ ] **Step 5: Commit**

```bash
git add src/buzz_fleet/systemd.py tests/test_systemd.py
git commit -m "feat: wire model/parallelism/idle-timeout/max-turn-duration/respond-to-allowlist env vars"
```

---

### Task 4: `personas.py` — `PersonaTemplate` model + parsers

**Files:**
- Create: `src/buzz_fleet/personas.py`
- Test: `tests/test_personas.py`
- Modify: `pyproject.toml` (add `pyyaml` dependency)

**Interfaces:**
- Produces:
  - `class PersonaTemplate(BaseModel)`: `display_name: str`, `harness: str | None`, `model: str | None`, `prompt_body: str`, `source_path: Path`, `parallelism: int | None`, `idle_timeout_seconds: int | None`, `max_turn_duration_seconds: int | None`.
  - `parse_persona_md(path: Path) -> PersonaTemplate | None` — `None` on any parse failure (missing/blank `display_name`, invalid YAML, no frontmatter at all).
  - `parse_agent_json(path: Path) -> PersonaTemplate | None` — `None` on any parse failure (invalid JSON, wrong `format`/`version`, missing `profile.displayName`).
  - `discover_personas(root: Path) -> tuple[list[PersonaTemplate], int]` — creates `root` if missing, returns `(templates, skipped_count)`. `skipped_count` = number of `.agent.png` files found, plus number of `.persona.md`/`.agent.json` files that failed to parse.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, under `[project]` `dependencies = [...]`, add `"pyyaml>=6.0"` to the list (open the file first to match the exact existing array formatting).

Run: `uv sync`

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_personas.py
import json
from pathlib import Path

from buzz_fleet.personas import discover_personas, parse_agent_json, parse_persona_md


def _write_agent_json(path: Path, **overrides: object) -> None:
    snapshot = {
        "format": "buzz-agent-snapshot",
        "version": 1,
        "definition": {
            "name": "laravel-template",
            "sourceIsBuiltin": False,
            "systemPrompt": "You are the Laravel dev.",
            "runtime": "claude",
            "model": "claude-sonnet-5",
            "provider": "anthropic",
            "parallelism": 2,
            "respondTo": "owner-only",
            "respondToAllowlist": ["a" * 64],
            "namePool": [],
            "idleTimeoutSeconds": 90,
            "maxTurnDurationSeconds": 300,
        },
        "profile": {
            "displayName": "Laravel Backend Dev",
            "about": "Laravel expert",
            "avatarDataUrl": None,
            "avatarUrl": None,
        },
        "memory": {"level": "none", "entries": []},
    }
    for key, value in overrides.items():
        snapshot[key] = value
    path.write_text(json.dumps(snapshot))


def test_parse_persona_md_extracts_fields(tmp_path: Path) -> None:
    path = tmp_path / "laravel.persona.md"
    path.write_text(
        "---\n"
        "display_name: Laravel Backend Dev\n"
        "runtime: claude\n"
        "model: claude-sonnet-5\n"
        "---\n"
        "You are the Laravel dev.\n"
    )

    template = parse_persona_md(path)

    assert template is not None
    assert template.display_name == "Laravel Backend Dev"
    assert template.harness == "claude"
    assert template.model == "claude-sonnet-5"
    assert template.prompt_body == "You are the Laravel dev.\n"
    assert template.source_path == path
    assert template.parallelism is None


def test_parse_persona_md_returns_none_without_display_name(tmp_path: Path) -> None:
    path = tmp_path / "broken.persona.md"
    path.write_text("---\nruntime: claude\n---\nBody text.\n")

    assert parse_persona_md(path) is None


def test_parse_persona_md_returns_none_for_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "broken.persona.md"
    path.write_text("---\n[unclosed\n---\nBody text.\n")

    assert parse_persona_md(path) is None


def test_parse_agent_json_extracts_fields_and_drops_unwired_ones(tmp_path: Path) -> None:
    path = tmp_path / "laravel.agent.json"
    _write_agent_json(path)

    template = parse_agent_json(path)

    assert template is not None
    assert template.display_name == "Laravel Backend Dev"
    assert template.harness == "claude"
    assert template.model == "claude-sonnet-5"
    assert template.prompt_body == "You are the Laravel dev."
    assert template.parallelism == 2
    assert template.idle_timeout_seconds == 90
    assert template.max_turn_duration_seconds == 300
    # respondToAllowlist must never be carried into the template at all.
    assert not hasattr(template, "respond_to_allowlist")


def test_parse_agent_json_returns_none_for_wrong_format(tmp_path: Path) -> None:
    path = tmp_path / "wrong.agent.json"
    _write_agent_json(path, format="something-else")

    assert parse_agent_json(path) is None


def test_parse_agent_json_returns_none_for_wrong_version(tmp_path: Path) -> None:
    path = tmp_path / "wrong.agent.json"
    _write_agent_json(path, version=2)

    assert parse_agent_json(path) is None


def test_parse_agent_json_returns_none_for_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "broken.agent.json"
    path.write_text("{not json")

    assert parse_agent_json(path) is None


def test_discover_personas_creates_directory_if_missing(tmp_path: Path) -> None:
    root = tmp_path / "personas"
    assert not root.exists()

    templates, skipped = discover_personas(root)

    assert root.exists()
    assert templates == []
    assert skipped == 0


def test_discover_personas_finds_both_formats_recursively(tmp_path: Path) -> None:
    root = tmp_path / "personas"
    (root / "stack-a").mkdir(parents=True)
    (root / "stack-a" / "laravel.persona.md").write_text(
        "---\ndisplay_name: Laravel Backend Dev\nruntime: claude\n---\nPrompt body.\n"
    )
    _write_agent_json(root / "dotnet.agent.json", profile={"displayName": ".NET Backend Dev", "about": None})

    templates, skipped = discover_personas(root)

    assert skipped == 0
    names = {t.display_name for t in templates}
    assert names == {"Laravel Backend Dev", ".NET Backend Dev"}


def test_discover_personas_counts_png_and_unparseable_files_as_skipped(tmp_path: Path) -> None:
    root = tmp_path / "personas"
    root.mkdir(parents=True)
    (root / "exported.agent.png").write_bytes(b"\x89PNG\r\n\x1a\nfakepngdata")
    (root / "broken.persona.md").write_text("---\nruntime: claude\n---\nNo display name.\n")

    templates, skipped = discover_personas(root)

    assert templates == []
    assert skipped == 2
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_personas.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'buzz_fleet.personas'`

- [ ] **Step 4: Implement `src/buzz_fleet/personas.py`**

```python
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
    raw = path.read_text()
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
    except json.JSONDecodeError:
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_personas.py -v`
Expected: PASS (all tests)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/buzz_fleet/personas.py tests/test_personas.py
git commit -m "feat: add persona/agent-snapshot template discovery and parsing"
```

---

### Task 5: `AgentManager.create_agent` — accept the four new fields; CLI flags

**Files:**
- Modify: `src/buzz_fleet/manager.py`
- Modify: `src/buzz_fleet/cli/app.py`
- Test: `tests/test_manager.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `Agent` fields from Task 1, `write_agent_files` from Task 3.
- Produces: `AgentManager.create_agent(..., parallelism: int | None = None, idle_timeout_seconds: int | None = None, max_turn_duration_seconds: int | None = None, respond_to_allowlist: list[str] | None = None)`.

- [ ] **Step 1: Write the failing test**

`tests/test_manager.py` has no shared fixture helper — each test builds a
`FakeRunner()` (already defined at module scope in that file) and constructs
`AgentManager(runner, _community())` directly, monkeypatching
`buzz_fleet.state.CONFIG_DIR`, `buzz_fleet.systemd.AGENTS_DIR`, and
`buzz_fleet.systemd.TEMPLATE_UNIT_PATH` to `tmp_path`-based paths (see
`test_create_agent_mints_key_registers_and_starts` for the exact pattern —
copy it verbatim). Add:

```python
def test_create_agent_stores_new_optional_fields(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())

    agent = manager.create_agent(
        display_name="Test Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="hi"),
        model="claude-sonnet-5",
        parallelism=3,
        idle_timeout_seconds=120,
        max_turn_duration_seconds=600,
        respond_to_allowlist=["a" * 64],
    )

    assert agent.model == "claude-sonnet-5"
    assert agent.parallelism == 3
    assert agent.idle_timeout_seconds == 120
    assert agent.max_turn_duration_seconds == 600
    assert agent.respond_to_allowlist == ["a" * 64]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_manager.py -k new_optional_fields -v`
Expected: FAIL — `TypeError: create_agent() got an unexpected keyword argument 'parallelism'`

- [ ] **Step 3: Implement in `manager.py`**

In `src/buzz_fleet/manager.py`, change the `create_agent` signature and body:

```python
    def create_agent(
        self,
        *,
        display_name: str,
        harness: str,
        system_prompt_source: SystemPromptSource,
        team_instructions: str | None = None,
        model: str | None = None,
        parallelism: int | None = None,
        idle_timeout_seconds: int | None = None,
        max_turn_duration_seconds: int | None = None,
        respond_to_allowlist: list[str] | None = None,
        role: str | None = None,
        anthropic_api_key: str | None = None,
        openai_api_key: str | None = None,
    ) -> Agent:
        systemd.ensure_linger_enabled(self._runner)
        systemd.ensure_template_unit_installed(self._runner)
        existing_ids = {a.id for a in self.list_agents()}
        agent_id = agent_slug(display_name, existing_ids)
        public_key, secret_key = signer_client.generate_key(self._runner)

        agent = Agent(
            id=agent_id,
            community_id=self._community.id,
            display_name=display_name,
            harness=harness,  # type: ignore[arg-type]
            private_key=secret_key,
            public_key=public_key,
            system_prompt_source=system_prompt_source,
            team_instructions=team_instructions,
            model=model,
            parallelism=parallelism,
            idle_timeout_seconds=idle_timeout_seconds,
            max_turn_duration_seconds=max_turn_duration_seconds,
            respond_to_allowlist=respond_to_allowlist,
            created_at=datetime.now(UTC),
        )
```

(Everything after the `Agent(...)` construction in the existing method body is unchanged.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_manager.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Add matching CLI flags**

`tests/test_cli.py` uses `typer.testing.CliRunner` (`runner_cli` at module
scope) plus a locally-defined `FakeAgentManager` class monkeypatched onto
`buzz_fleet.cli.app.AgentManager`, and `buzz_fleet.cli.app.state.load_community`
monkeypatched to return a `SimpleNamespace(id=cid)` — see
`test_agent_update_calls_manager_with_changes` for the exact pattern. Add:

```python
def test_agent_create_passes_new_optional_fields(tmp_path, monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeAgentManager:
        def __init__(self, runner: object, community: object) -> None:
            pass

        def create_agent(self, **kwargs: object) -> object:
            calls.update(kwargs)
            return SimpleNamespace(id="test-agent", public_key="ab" * 32)

    monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)

    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("You are an agent.")

    result = runner_cli.invoke(
        app,
        [
            "agent", "create",
            "--community", "eltahir",
            "--display-name", "Test Agent",
            "--harness", "claude",
            "--prompt-file", str(prompt_file),
            "--model", "claude-sonnet-5",
            "--parallelism", "3",
            "--idle-timeout-seconds", "120",
            "--max-turn-duration-seconds", "600",
            "--respond-to-allowlist", f"{'a' * 64},{'b' * 64}",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["model"] == "claude-sonnet-5"
    assert calls["parallelism"] == 3
    assert calls["idle_timeout_seconds"] == 120
    assert calls["max_turn_duration_seconds"] == 600
    assert calls["respond_to_allowlist"] == ["a" * 64, "b" * 64]


def test_agent_update_passes_new_optional_fields(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeAgentManager:
        def __init__(self, runner: object, community: object) -> None:
            pass

        def update_agent(self, agent_id: str, **changes: object) -> object:
            calls["agent_id"] = agent_id
            calls["changes"] = changes
            return SimpleNamespace(id=agent_id)

    monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)

    result = runner_cli.invoke(
        app,
        [
            "agent", "update", "--community", "eltahir", "agent-1",
            "--model", "claude-sonnet-5",
            "--respond-to-allowlist", "a" * 64,
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["changes"] == {"model": "claude-sonnet-5", "respond_to_allowlist": ["a" * 64]}
```

Both tests need `from types import SimpleNamespace` — already imported at the
top of `tests/test_cli.py`.

Then in `src/buzz_fleet/cli/app.py`, update `agent_create`:

```python
@agent_app.command("create")
def agent_create(
    community: Annotated[str, typer.Option()],
    display_name: Annotated[str, typer.Option()],
    harness: Annotated[str, typer.Option()],
    prompt_file: Annotated[Path, typer.Option(help="Path to a persona .persona.md or plain prompt text file")],
    model: Annotated[str | None, typer.Option()] = None,
    parallelism: Annotated[int | None, typer.Option()] = None,
    idle_timeout_seconds: Annotated[int | None, typer.Option()] = None,
    max_turn_duration_seconds: Annotated[int | None, typer.Option()] = None,
    respond_to_allowlist: Annotated[
        str | None, typer.Option(help="Comma-separated pubkeys")
    ] = None,
) -> None:
    manager = _load_manager(community)
    try:
        agent = manager.create_agent(
            display_name=display_name,
            harness=harness,
            system_prompt_source=SystemPromptSource(kind="persona_file", path=prompt_file),
            model=model,
            parallelism=parallelism,
            idle_timeout_seconds=idle_timeout_seconds,
            max_turn_duration_seconds=max_turn_duration_seconds,
            respond_to_allowlist=respond_to_allowlist.split(",") if respond_to_allowlist else None,
        )
    except ValueError as e:
        # e.g. a blank/punctuation-only --display-name (agent_slug raises)
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"Created agent '{agent.id}' ({agent.public_key}).")
```

And `agent_update`:

```python
@agent_app.command("update")
def agent_update(
    community: Annotated[str, typer.Option()],
    agent_id: Annotated[str, typer.Argument()],
    display_name: Annotated[str | None, typer.Option()] = None,
    prompt_file: Annotated[
        Path | None, typer.Option(help="Replace the system prompt with this persona/prompt file")
    ] = None,
    model: Annotated[str | None, typer.Option()] = None,
    parallelism: Annotated[int | None, typer.Option()] = None,
    idle_timeout_seconds: Annotated[int | None, typer.Option()] = None,
    max_turn_duration_seconds: Annotated[int | None, typer.Option()] = None,
    respond_to_allowlist: Annotated[
        str | None, typer.Option(help="Comma-separated pubkeys")
    ] = None,
) -> None:
    manager = _load_manager(community)
    changes: dict[str, object] = {}
    if display_name is not None:
        changes["display_name"] = display_name
    if prompt_file is not None:
        changes["system_prompt_source"] = SystemPromptSource(kind="persona_file", path=prompt_file)
    if model is not None:
        changes["model"] = model
    if parallelism is not None:
        changes["parallelism"] = parallelism
    if idle_timeout_seconds is not None:
        changes["idle_timeout_seconds"] = idle_timeout_seconds
    if max_turn_duration_seconds is not None:
        changes["max_turn_duration_seconds"] = max_turn_duration_seconds
    if respond_to_allowlist is not None:
        changes["respond_to_allowlist"] = respond_to_allowlist.split(",")
    if not changes:
        typer.echo("Nothing to update — pass at least one field to change.", err=True)
        raise typer.Exit(code=1)
    updated = manager.update_agent(agent_id, **changes)
    typer.echo(f"Updated agent '{updated.id}'.")
```

- [ ] **Step 6: Run full test suite**

Run: `uv run pytest -v`
Expected: PASS (everything, including pre-existing tests)

- [ ] **Step 7: Commit**

```bash
git add src/buzz_fleet/manager.py src/buzz_fleet/cli/app.py tests/test_manager.py tests/test_cli.py
git commit -m "feat: expose model/parallelism/idle-timeout/max-turn-duration/respond-to-allowlist on create_agent and the CLI"
```

---

### Task 6: `AgentFormScreen` — template picker, harness select, new inputs

**Files:**
- Modify: `src/buzz_fleet/tui/screens/agent_form.py`
- Test: `tests/tui/test_agent_form.py`

**Interfaces:**
- Consumes: `discover_personas` and `PersonaTemplate` from Task 4; `AgentManager.create_agent`'s new kwargs from Task 5.
- Produces: no public interface change — internal widget IDs used by tests: `#template-select`, `#harness-select`, `#model-input`, `#parallelism-input`, `#idle-timeout-input`, `#max-turn-duration-input`, `#respond-to-allowlist-input` (create mode only for `#template-select`; the rest appear in both modes, matching the spec).

- [ ] **Step 1: Write the failing tests**

Add to `tests/tui/test_agent_form.py`:

```python
@pytest.mark.asyncio
async def test_template_select_present_only_in_create_mode(tmp_path, monkeypatch) -> None:
    from buzz_fleet import personas
    from textual.widgets import Select

    monkeypatch.setattr(personas, "DEFAULT_PERSONAS_DIR", tmp_path / "personas")

    manager = FakeManager()
    app = BuzzFleetApp()

    async with app.run_test() as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()
        assert app.screen.query("#template-select")

        await app.pop_screen()
        from datetime import UTC, datetime

        from buzz_fleet.models import Agent, SystemPromptSource as SPS

        existing = Agent(
            id="x",
            community_id="eltahir",
            display_name="X",
            harness="claude",
            private_key="nsec1x",
            public_key="a" * 64,
            system_prompt_source=SPS(kind="inline", text="hi"),
            created_at=datetime.now(UTC),
        )
        await app.push_screen(AgentFormScreen(manager, agent=existing))
        await pilot.pause()
        assert not app.screen.query("#template-select")


@pytest.mark.asyncio
async def test_selecting_template_prefills_and_overwrites_form_fields(tmp_path, monkeypatch) -> None:
    from buzz_fleet import personas
    from textual.widgets import Select

    personas_dir = tmp_path / "personas"
    personas_dir.mkdir()
    (personas_dir / "laravel.persona.md").write_text(
        "---\ndisplay_name: Laravel Backend Dev\nruntime: claude\nmodel: claude-sonnet-5\n---\n"
        "You are the Laravel dev.\n"
    )
    monkeypatch.setattr(personas, "DEFAULT_PERSONAS_DIR", personas_dir)

    manager = FakeManager()
    app = BuzzFleetApp()

    async with app.run_test() as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()
        app.screen.query_one("#display-name-input", Input).value = "Something Typed First"

        # Only one template on disk, so it's index 0 — discover_personas globs
        # .persona.md files (sorted) before .agent.json files, and the picker
        # builds options as enumerate(self._templates). Avoid relying on any
        # private Select attribute to read options back.
        select = app.screen.query_one("#template-select", Select)
        select.value = 0
        await pilot.pause()

        assert app.screen.query_one("#display-name-input", Input).value == "Laravel Backend Dev"
        assert app.screen.query_one("#prompt-input", Input).value == "You are the Laravel dev.\n"
        assert app.screen.query_one("#model-input", Input).value == "claude-sonnet-5"


@pytest.mark.asyncio
async def test_submitting_form_passes_new_fields_to_create_agent() -> None:
    manager = FakeManager()
    app = BuzzFleetApp()

    async with app.run_test() as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()
        app.screen.query_one("#display-name-input", Input).value = "Test Agent"
        app.screen.query_one("#prompt-input", Input).value = "You are a test agent."
        app.screen.query_one("#model-input", Input).value = "claude-sonnet-5"
        app.screen.query_one("#parallelism-input", Input).value = "3"
        app.screen.query_one("#idle-timeout-input", Input).value = "120"
        app.screen.query_one("#max-turn-duration-input", Input).value = "600"
        app.screen.query_one("#respond-to-allowlist-input", Input).value = f"{'a' * 64}, {'b' * 64}"
        await pilot.click("#submit-button")
        await pilot.pause()

    assert len(manager.created) == 1
    kwargs = manager.created[0]
    assert kwargs["model"] == "claude-sonnet-5"
    assert kwargs["parallelism"] == 3
    assert kwargs["idle_timeout_seconds"] == 120
    assert kwargs["max_turn_duration_seconds"] == 600
    assert kwargs["respond_to_allowlist"] == ["a" * 64, "b" * 64]


@pytest.mark.asyncio
async def test_submitting_form_with_blank_optional_fields_passes_none() -> None:
    manager = FakeManager()
    app = BuzzFleetApp()

    async with app.run_test() as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()
        app.screen.query_one("#display-name-input", Input).value = "Test Agent"
        app.screen.query_one("#prompt-input", Input).value = "You are a test agent."
        await pilot.click("#submit-button")
        await pilot.pause()

    kwargs = manager.created[0]
    assert kwargs["model"] is None
    assert kwargs["parallelism"] is None
    assert kwargs["idle_timeout_seconds"] is None
    assert kwargs["max_turn_duration_seconds"] is None
    assert kwargs["respond_to_allowlist"] is None


@pytest.mark.asyncio
async def test_submitting_form_with_non_numeric_parallelism_notifies_instead_of_crashing() -> None:
    manager = FakeManager()
    app = BuzzFleetApp()

    async with app.run_test() as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()
        app.screen.query_one("#display-name-input", Input).value = "Test Agent"
        app.screen.query_one("#prompt-input", Input).value = "hi"
        app.screen.query_one("#parallelism-input", Input).value = "not-a-number"
        await pilot.click("#submit-button")
        await pilot.pause()

    assert manager.created == []
    assert isinstance(app.screen, AgentFormScreen)
```

Adjust `test_submitting_form_calls_create_agent` (the pre-existing test) if it now needs `#model-input` etc. to default correctly — it shouldn't need changes since all new fields are optional and blank by default, but re-run it to confirm.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/tui/test_agent_form.py -v`
Expected: FAIL — none of the new widgets/behavior exist yet.

- [ ] **Step 3: Implement**

First, add a module-level `DEFAULT_PERSONAS_DIR` constant to `src/buzz_fleet/personas.py` (Task 4's file) so tests can monkeypatch it:

```python
DEFAULT_PERSONAS_DIR = Path.home() / ".config" / "buzz-fleet" / "personas"
```

Then rewrite `src/buzz_fleet/tui/screens/agent_form.py`:

```python
"""Create/update agent form screen."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Select

from buzz_fleet import personas
from buzz_fleet.manager import AgentManager
from buzz_fleet.models import Agent, SystemPromptSource

_HARNESSES = ["claude", "codex", "pi", "goose"]


class AgentFormScreen(Screen):
    def __init__(self, manager: AgentManager, agent: Agent | None = None) -> None:
        super().__init__()
        self._manager = manager
        self._agent = agent
        self._original_prompt_text: str | None = None
        self._templates: list[personas.PersonaTemplate] = []

    def compose(self) -> ComposeResult:
        yield Header()
        display_name = self._agent.display_name if self._agent else ""
        harness = self._agent.harness if self._agent else "claude"
        prompt_text = ""
        if self._agent and self._agent.system_prompt_source.kind == "inline":
            prompt_text = self._agent.system_prompt_source.text or ""
        # Only meaningful in edit mode: lets on_button_pressed detect whether the
        # user actually edited the prompt, versus merely re-submitting the form
        # with the display name changed. This matters because a persona_file
        # agent's prompt_text is always "" here (never pre-filled from a file
        # path) — without this guard, submitting with an untouched prompt field
        # would silently downgrade the agent to an empty inline prompt and
        # destroy its persona file. See on_button_pressed.
        self._original_prompt_text = prompt_text

        if self._agent is None:
            self._templates, skipped = personas.discover_personas(personas.DEFAULT_PERSONAS_DIR)
            options = [
                (f"{t.display_name} ({t.source_path.name})", i) for i, t in enumerate(self._templates)
            ]
            prompt = "Start from a template…" if not skipped else f"Start from a template… ({skipped} unsupported file(s) found)"
            yield Select(options, prompt=prompt, id="template-select")

        yield Input(value=display_name, placeholder="Display name", id="display-name-input")
        yield Select(
            [(h, h) for h in _HARNESSES], value=harness, allow_blank=False, id="harness-select"
        )
        yield Input(value=prompt_text, placeholder="System prompt", id="prompt-input")
        yield Input(
            value=self._agent.model if self._agent and self._agent.model else "",
            placeholder="Model (optional)",
            id="model-input",
        )
        yield Input(
            value=str(self._agent.parallelism) if self._agent and self._agent.parallelism is not None else "",
            placeholder="Parallelism (optional)",
            id="parallelism-input",
        )
        yield Input(
            value=(
                str(self._agent.idle_timeout_seconds)
                if self._agent and self._agent.idle_timeout_seconds is not None
                else ""
            ),
            placeholder="Idle timeout seconds (optional)",
            id="idle-timeout-input",
        )
        yield Input(
            value=(
                str(self._agent.max_turn_duration_seconds)
                if self._agent and self._agent.max_turn_duration_seconds is not None
                else ""
            ),
            placeholder="Max turn duration seconds (optional)",
            id="max-turn-duration-input",
        )
        yield Input(
            value=(
                ", ".join(self._agent.respond_to_allowlist)
                if self._agent and self._agent.respond_to_allowlist
                else ""
            ),
            placeholder="Respond-to allowlist pubkeys, comma-separated (optional)",
            id="respond-to-allowlist-input",
        )
        yield Button("Update" if self._agent else "Create", id="submit-button")
        yield Footer()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "template-select":
            return
        if event.value == Select.BLANK:
            return
        template = self._templates[event.value]
        self.query_one("#display-name-input", Input).value = template.display_name
        if template.harness in _HARNESSES:
            self.query_one("#harness-select", Select).value = template.harness
        self.query_one("#prompt-input", Input).value = template.prompt_body
        self.query_one("#model-input", Input).value = template.model or ""
        self.query_one("#parallelism-input", Input).value = (
            str(template.parallelism) if template.parallelism is not None else ""
        )
        self.query_one("#idle-timeout-input", Input).value = (
            str(template.idle_timeout_seconds) if template.idle_timeout_seconds is not None else ""
        )
        self.query_one("#max-turn-duration-input", Input).value = (
            str(template.max_turn_duration_seconds)
            if template.max_turn_duration_seconds is not None
            else ""
        )
        # respond_to_allowlist is deliberately never pre-filled from a
        # template — see the design spec.

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "submit-button":
            return
        display_name = self.query_one("#display-name-input", Input).value
        prompt_text = self.query_one("#prompt-input", Input).value
        harness = self.query_one("#harness-select", Select).value
        model = self.query_one("#model-input", Input).value.strip() or None
        respond_to_raw = self.query_one("#respond-to-allowlist-input", Input).value.strip()
        respond_to_allowlist = (
            [key.strip() for key in respond_to_raw.split(",") if key.strip()] if respond_to_raw else None
        )

        try:
            parallelism = self._parse_optional_int("#parallelism-input")
            idle_timeout_seconds = self._parse_optional_int("#idle-timeout-input")
            max_turn_duration_seconds = self._parse_optional_int("#max-turn-duration-input")
        except ValueError:
            self.notify("Parallelism, idle timeout, and max turn duration must be whole numbers.", severity="error")
            return

        try:
            if self._agent is not None:
                changes: dict[str, object] = {
                    "display_name": display_name,
                    "harness": harness,
                    "model": model,
                    "parallelism": parallelism,
                    "idle_timeout_seconds": idle_timeout_seconds,
                    "max_turn_duration_seconds": max_turn_duration_seconds,
                    "respond_to_allowlist": respond_to_allowlist,
                }
                # Only touch system_prompt_source if the user actually edited the
                # prompt field. This is the fix for the v1 bug where editing only
                # the display name of a persona_file agent silently overwrote its
                # persona file with an empty inline prompt (the prompt Input is
                # never pre-filled for persona_file agents, so leaving it alone
                # must mean "leave the prompt source alone", not "set it to '').
                if prompt_text != self._original_prompt_text:
                    changes["system_prompt_source"] = SystemPromptSource(kind="inline", text=prompt_text)
                self._manager.update_agent(self._agent.id, **changes)
            else:
                prompt_source = SystemPromptSource(kind="inline", text=prompt_text)
                self._manager.create_agent(
                    display_name=display_name,
                    harness=harness,
                    system_prompt_source=prompt_source,
                    model=model,
                    parallelism=parallelism,
                    idle_timeout_seconds=idle_timeout_seconds,
                    max_turn_duration_seconds=max_turn_duration_seconds,
                    respond_to_allowlist=respond_to_allowlist,
                )
        except ValueError as e:
            # e.g. a blank/punctuation-only display name (agent_slug raises)
            # must not crash the app — surface it and let the user retry.
            self.notify(str(e), severity="error")
            return
        self.app.pop_screen()

    def _parse_optional_int(self, input_id: str) -> int | None:
        raw = self.query_one(input_id, Input).value.strip()
        return int(raw) if raw else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/tui/test_agent_form.py -v`
Expected: PASS (all tests, including the pre-existing ones — re-check
`test_submitting_form_in_edit_mode_calls_update_agent` and the two other
pre-existing edit-mode tests still pass now that `update_agent`'s `changes`
dict always includes `harness`/`model`/the three numeric fields/allowlist;
if a pre-existing test asserts an exact `changes` dict rather than checking
specific keys, update its assertions to check the keys it cares about
instead of exact dict equality — matching the intent of "editing only
display name must not touch the prompt," not a byte-for-byte `changes`
comparison).

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: PASS (everything)

- [ ] **Step 6: Commit**

```bash
git add src/buzz_fleet/personas.py src/buzz_fleet/tui/screens/agent_form.py tests/tui/test_agent_form.py
git commit -m "feat: add persona template picker, harness select, and new field inputs to the create-agent form"
```

---

### Task 7: README updates

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document the new directory, CLI flags, and TUI fields**

Add a new subsection after "### Manage agents (CLI)" documenting:
- The default template directory (`~/.config/buzz-fleet/personas`, auto-created), both supported formats (`.persona.md`, `.agent.json`), and that `.agent.png` is not parsed (counted as unsupported).
- The five new CLI flags on `agent create`/`agent update`: `--model`, `--parallelism`, `--idle-timeout-seconds`, `--max-turn-duration-seconds`, `--respond-to-allowlist`.
- In the "### Manage agents (TUI)" section, mention the template picker (`c` create → optional template dropdown that prefills the rest of the form, editable before submit) and that these same new fields are available as blank-by-default inputs on the create/edit form.

Use the exact env var names from the Global Constraints section above so the README stays accurate to what the code actually emits.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document the persona template picker and new agent fields"
```
