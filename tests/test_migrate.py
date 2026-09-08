"""The migration is per-agent and idempotent, so resuming after an interruption
is just running it again. It refuses to start while any unit is active, because
moving an agent's env file out from under a running unit is how you get an agent
holding a key it can no longer re-read."""

import subprocess
from pathlib import Path

import pytest

from buzz_fleet import migrate, paths


class FakeRunner:
    """`is-active` answers from `self.active`; everything else succeeds."""

    def __init__(self, active: set[str] | None = None) -> None:
        self.active = active or set()
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if "is-active" in args:
            unit = args[-1]
            return subprocess.CompletedProcess(
                args, 0, stdout="active" if unit in self.active else "inactive", stderr=""
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _legacy_tree(tmp_path: Path, monkeypatch) -> Path:
    """A pre-migration machine: one community, one agent, env and prompt files."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    legacy = home / ".config" / "buzz-fleet"
    (legacy / "communities" / "eltahir" / "agents").mkdir(parents=True)
    (legacy / "communities" / "eltahir.json").write_text(
        '{"id":"eltahir","relay_url":"wss://r","relay_admin_nsec":"nsec1owner"}'
    )
    (legacy / "communities" / "eltahir" / "agents" / "reviewer.json").write_text(
        '{"id":"reviewer","community_id":"eltahir","display_name":"R","harness":"claude",'
        '"private_key":"nsec1agent","public_key":"' + "a" * 64 + '",'
        '"system_prompt_source":{"kind":"inline","text":"hi"}}'
    )
    (legacy / "agents").mkdir(parents=True)
    (legacy / "agents" / "reviewer.env").write_text("BUZZ_PRIVATE_KEY=nsec1agent\n")
    (legacy / "agents" / "reviewer.prompt.md").write_text("hi")
    return legacy


def test_dry_run_changes_nothing(tmp_path, monkeypatch) -> None:
    legacy = _legacy_tree(tmp_path, monkeypatch)
    steps = migrate.run(FakeRunner(), dry_run=True)
    assert steps
    assert (legacy / "agents" / "reviewer.env").exists()
    assert not (paths.state_dir() / "communities" / "eltahir.json").exists()


def test_refuses_while_a_unit_is_active(tmp_path, monkeypatch) -> None:
    _legacy_tree(tmp_path, monkeypatch)
    runner = FakeRunner(active={"buzz-agent@reviewer.service"})
    with pytest.raises(RuntimeError, match="still active"):
        migrate.run(runner)


def test_moves_state_and_secrets_apart(tmp_path, monkeypatch) -> None:
    _legacy_tree(tmp_path, monkeypatch)
    migrate.run(FakeRunner())
    community = (paths.state_dir() / "communities" / "eltahir.json").read_text()
    assert "nsec1owner" not in community
    secret = (paths.secrets_dir() / "communities" / "eltahir.json").read_text()
    assert "nsec1owner" in secret


def test_env_file_moves_to_the_qualified_secret_path(tmp_path, monkeypatch) -> None:
    _legacy_tree(tmp_path, monkeypatch)
    migrate.run(FakeRunner())
    moved = paths.secrets_dir() / "units" / "eltahir:reviewer.env"
    assert moved.read_text() == "BUZZ_PRIVATE_KEY=nsec1agent\n"
    assert oct(moved.stat().st_mode & 0o777) == "0o600"


def test_prompt_file_moves_to_the_qualified_state_path(tmp_path, monkeypatch) -> None:
    _legacy_tree(tmp_path, monkeypatch)
    migrate.run(FakeRunner())
    assert (paths.state_dir() / "units" / "eltahir:reviewer.prompt.md").read_text() == "hi"


def test_old_unit_is_disabled_and_new_one_enabled(tmp_path, monkeypatch) -> None:
    _legacy_tree(tmp_path, monkeypatch)
    runner = FakeRunner()
    migrate.run(runner)
    flat = [" ".join(c) for c in runner.calls]
    assert any("disable --now buzz-agent@reviewer.service" in c for c in flat)
    assert any("enable --now buzz-agent@eltahir:reviewer.service" in c for c in flat)


def test_backup_is_taken_before_anything_moves(tmp_path, monkeypatch) -> None:
    _legacy_tree(tmp_path, monkeypatch)
    migrate.run(FakeRunner())
    backups = list((Path.home()).glob(".config/buzz-fleet.bak-*"))
    assert len(backups) == 1
    assert (backups[0] / "agents" / "reviewer.env").exists()


def test_running_twice_is_a_no_op(tmp_path, monkeypatch) -> None:
    _legacy_tree(tmp_path, monkeypatch)
    migrate.run(FakeRunner())
    second = migrate.run(FakeRunner())
    assert second == []


def test_resumes_a_half_migrated_tree(tmp_path, monkeypatch) -> None:
    """The interrupted case: the env file already moved, the prompt did not.
    Only the unfinished half is redone."""
    legacy = _legacy_tree(tmp_path, monkeypatch)
    already = paths.secrets_dir() / "units" / "eltahir:reviewer.env"
    already.parent.mkdir(parents=True, exist_ok=True)
    already.write_text("BUZZ_PRIVATE_KEY=nsec1agent\n")
    (legacy / "agents" / "reviewer.env").unlink()

    migrate.run(FakeRunner())
    assert (paths.state_dir() / "units" / "eltahir:reviewer.prompt.md").read_text() == "hi"
    assert already.read_text() == "BUZZ_PRIVATE_KEY=nsec1agent\n"


def test_writes_a_layout_version_marker(tmp_path, monkeypatch) -> None:
    _legacy_tree(tmp_path, monkeypatch)
    migrate.run(FakeRunner())
    assert (paths.state_dir() / "layout-version").read_text().strip() == str(migrate.LAYOUT_VERSION)


def test_no_legacy_tree_is_a_no_op(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert migrate.run(FakeRunner()) == []


def test_env_file_paths_into_the_legacy_tree_are_rewritten(tmp_path, monkeypatch) -> None:
    """The hazard a byte-for-byte copy would reintroduce: a copied-verbatim env
    file's BUZZ_ACP_SYSTEM_PROMPT_FILE still points at the legacy prompt path,
    which the migration deletes by moving it elsewhere. A test that only checks
    the env file was copied would pass against that broken version — this one
    resolves the path the migrated file actually points at and proves it exists.
    """
    legacy = _legacy_tree(tmp_path, monkeypatch)
    old_prompt = legacy / "agents" / "reviewer.prompt.md"
    old_work = Path.home() / ".local" / "share" / "buzz-fleet" / "work" / "reviewer"
    old_work.mkdir(parents=True)
    (old_work / "mcp-search.sh").write_text("#!/bin/sh\nexec true\n")
    (legacy / "agents" / "reviewer.env").write_text(
        "BUZZ_PRIVATE_KEY=nsec1agent\n"
        f"BUZZ_ACP_SYSTEM_PROMPT_FILE={old_prompt}\n"
        f"BUZZ_ACP_MCP_COMMAND={old_work}/mcp-search.sh\n"
    )

    migrate.run(FakeRunner())

    moved = paths.secrets_dir() / "units" / "eltahir:reviewer.env"
    env_values = dict(
        line.split("=", 1) for line in moved.read_text().splitlines() if line.strip()
    )

    prompt_path = Path(env_values["BUZZ_ACP_SYSTEM_PROMPT_FILE"])
    assert prompt_path.exists()
    assert prompt_path == paths.state_dir() / "units" / "eltahir:reviewer.prompt.md"
    assert str(old_prompt) not in moved.read_text()

    mcp_command = Path(env_values["BUZZ_ACP_MCP_COMMAND"])
    assert mcp_command == paths.data_dir() / "work" / "eltahir:reviewer" / "mcp-search.sh"
    assert str(old_work) not in moved.read_text()


def test_refuses_the_whole_migration_for_an_invalid_legacy_community_id(
    tmp_path, monkeypatch
) -> None:
    """Task 7 validates community ids at creation but deliberately did not
    retrofit that check onto already-saved communities, so a legacy machine can
    hold an id that's invalid under today's rules. Hitting that mid-migration
    (in `units.unit_name`) would abort after some agents had already moved —
    `plan()` must catch it up front and refuse everything, including agents
    belonging to an otherwise-fine community.
    """
    legacy = _legacy_tree(tmp_path, monkeypatch)
    (legacy / "communities" / "bad:id.json").write_text(
        '{"id":"bad:id","relay_url":"wss://r","relay_admin_nsec":"nsec1owner2"}'
    )

    with pytest.raises(RuntimeError, match="bad:id"):
        migrate.run(FakeRunner())

    # Nothing moved at all -- not even the perfectly valid "eltahir" community.
    assert not (paths.state_dir() / "communities" / "eltahir.json").exists()
    assert not (paths.state_dir() / "communities" / "bad:id.json").exists()
