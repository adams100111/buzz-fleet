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
