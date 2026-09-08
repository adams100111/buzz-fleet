"""The migration is per-agent and idempotent, so resuming after an interruption
is just running it again. It refuses to start while any unit is active, because
moving an agent's env file out from under a running unit is how you get an agent
holding a key it can no longer re-read."""

import shutil
import subprocess
from pathlib import Path

import pytest

from buzz_fleet import migrate, paths, systemd, units


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


def _two_community_legacy_tree(tmp_path: Path, monkeypatch, *, second_agent_id: str) -> Path:
    """Two legacy communities, "eltahir" (from `_legacy_tree`, agent
    "reviewer") and "acme" (agent `second_agent_id`).

    No test in this file exercised more than one legacy community before
    this fixture -- every other test uses `_legacy_tree`, which builds
    exactly one community with one agent, so the normal multi-community
    migrate path (and the collision the legacy layout's bare, global
    agent-id keying makes possible) had never actually been run.

    `second_agent_id="reviewer"` reproduces the collision this fixture
    exists for: the legacy layout keys env/prompt files on the bare agent
    id GLOBALLY, so a second community whose agent is ALSO called
    "reviewer" doesn't get its own `agents/reviewer.env` -- there is
    exactly one on a real machine hitting this, already written by
    `_legacy_tree`, and this deliberately does not write a second one.
    Any other value (e.g. "auditor") gives "acme" its own, non-colliding
    agent id with its own env/prompt file, to prove the ordinary
    multi-community path actually works.
    """
    legacy = _legacy_tree(tmp_path, monkeypatch)

    (legacy / "communities" / "acme" / "agents").mkdir(parents=True)
    (legacy / "communities" / "acme.json").write_text(
        '{"id":"acme","relay_url":"wss://r2","relay_admin_nsec":"nsec1owner2"}'
    )
    (legacy / "communities" / "acme" / "agents" / f"{second_agent_id}.json").write_text(
        '{"id":"' + second_agent_id + '","community_id":"acme","display_name":"A",'
        '"harness":"claude","private_key":"nsec1other","public_key":"' + "b" * 64 + '",'
        '"system_prompt_source":{"kind":"inline","text":"yo"}}'
    )
    if second_agent_id != "reviewer":
        (legacy / "agents" / f"{second_agent_id}.env").write_text("BUZZ_PRIVATE_KEY=nsec1other\n")
        (legacy / "agents" / f"{second_agent_id}.prompt.md").write_text("yo")
    return legacy


def test_dry_run_changes_nothing(tmp_path, monkeypatch) -> None:
    legacy = _legacy_tree(tmp_path, monkeypatch)
    runner = FakeRunner()
    steps = migrate.run(runner, dry_run=True)
    assert steps
    assert (legacy / "agents" / "reviewer.env").exists()
    assert not (paths.state_dir() / "communities" / "eltahir.json").exists()
    # Minor fix-round addition: a dry run must be a genuine no-op, not just
    # "doesn't write state" -- it must not shell out at all (plan() never
    # touches `runner`) and must not take a backup either.
    assert runner.calls == []
    assert list(Path.home().glob(".config/buzz-fleet.bak-*")) == []


def test_refuses_while_a_unit_is_active(tmp_path, monkeypatch) -> None:
    _legacy_tree(tmp_path, monkeypatch)
    runner = FakeRunner(active={"buzz-agent@reviewer.service"})
    with pytest.raises(RuntimeError, match="still active"):
        migrate.run(runner)


def test_refuses_while_a_unit_is_activating(tmp_path, monkeypatch) -> None:
    """Fix-round C3: the crash-loop state CLAUDE.md documents from a real
    775-restart incident, and exactly the machine most likely to be
    migrated -- a naive `== "active"` comparison would treat this as safe."""
    _legacy_tree(tmp_path, monkeypatch)

    class ActivatingRunner(FakeRunner):
        def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if "is-active" in args:
                return subprocess.CompletedProcess(args, 0, stdout="activating", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with pytest.raises(RuntimeError, match="still active"):
        migrate.run(ActivatingRunner())


def test_refuses_when_systemctl_gives_no_usable_answer(tmp_path, monkeypatch) -> None:
    """Fix-round C3: a broken or unreachable systemctl (nonzero exit, empty
    stdout) must refuse to migrate, not be treated as "safely inactive"."""
    _legacy_tree(tmp_path, monkeypatch)

    class BrokenRunner(FakeRunner):
        def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if "is-active" in args:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="Failed to connect to bus")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with pytest.raises(RuntimeError, match="still active"):
        migrate.run(BrokenRunner())


def test_refuses_while_a_unit_is_deactivating(tmp_path, monkeypatch) -> None:
    """Fix-round 2 MUST-FIX 1: an auto-restarting (Restart=on-failure) unit
    transits `deactivating -> activating -> active` on EVERY restart, so a
    migration that accepts "deactivating" as safe guards only half of the
    exact crash-loop cycle `test_refuses_while_a_unit_is_activating` guards
    the other half of. `systemctl_client`'s own `AgentStatus.STOPPED` folds
    "deactivating" in with "inactive" for display purposes -- migrate must
    NOT inherit that coarsening for a safety decision, hence
    `raw_state_of_unit` plus migrate's own explicit allowlist rather than
    reusing `status()`/`AgentStatus`."""
    _legacy_tree(tmp_path, monkeypatch)

    class DeactivatingRunner(FakeRunner):
        def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if "is-active" in args:
                return subprocess.CompletedProcess(args, 0, stdout="deactivating", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with pytest.raises(RuntimeError, match="still active"):
        migrate.run(DeactivatingRunner())


def test_failed_and_inactive_are_safe(tmp_path, monkeypatch) -> None:
    """The flip side of the refusing tests above: `inactive` and `failed`
    (the literal raw systemctl strings migrate's allowlist accepts) must
    NOT block a migration."""
    for unit_state in ("inactive", "failed"):
        _legacy_tree(tmp_path / unit_state, monkeypatch)

        class Runner(FakeRunner):
            def run(self, args: list[str], _state: str = unit_state) -> subprocess.CompletedProcess[str]:
                self.calls.append(args)
                if "is-active" in args:
                    return subprocess.CompletedProcess(args, 0, stdout=_state, stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        migrate.run(Runner())  # must not raise


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


def test_legacy_prompt_file_is_removed_once_the_copy_is_verified(tmp_path, monkeypatch) -> None:
    """Fix-round I3: without this, a byte-for-byte-copy (un-rewritten) env
    file's BUZZ_ACP_SYSTEM_PROMPT_FILE would still resolve to an existing
    file (the never-deleted legacy copy), making an `exists()`-only check
    vacuous. Deleting the legacy prompt after a verified write closes that;
    prompt text isn't a secret, unlike the env/community/agent files
    deliberately left in place (see README's "Upgrading from 0.8.x")."""
    legacy = _legacy_tree(tmp_path, monkeypatch)
    migrate.run(FakeRunner())
    assert not (legacy / "agents" / "reviewer.prompt.md").exists()


def test_old_unit_is_disabled_and_new_one_enabled(tmp_path, monkeypatch) -> None:
    _legacy_tree(tmp_path, monkeypatch)
    runner = FakeRunner()
    migrate.run(runner)
    flat = [" ".join(c) for c in runner.calls]
    assert any("disable --now buzz-agent@reviewer.service" in c for c in flat)
    assert any("enable --now buzz-agent@eltahir:reviewer.service" in c for c in flat)


def test_migrate_installs_the_current_template_unit(tmp_path, monkeypatch) -> None:
    """Fix-round C1: without this, every unit `migrate` enables resolves
    against the STALE legacy template (`EnvironmentFile=~/.config/buzz-fleet/
    agents/%i.env`), pointing at an env file that will never exist there
    again -- the unit fails to start after migration reports success."""
    _legacy_tree(tmp_path, monkeypatch)
    runner = FakeRunner()
    migrate.run(runner)
    content = systemd.template_unit_path().read_text()
    assert str(paths.secrets_dir() / "units") in content
    assert "/agents/%i.env" not in content

    # Fix-round 2: the content check above would also pass against a buggy
    # version that installs the template AFTER the loop (too late for the
    # very `enable --now` this is meant to protect) -- assert the ORDER of
    # calls, not just the end state.
    flat = [" ".join(c) for c in runner.calls]
    daemon_reload_index = flat.index("systemctl --user daemon-reload")
    first_enable_index = next(i for i, c in enumerate(flat) if "enable --now" in c)
    assert daemon_reload_index < first_enable_index


def test_a_failed_enable_aborts_the_migration_rather_than_reporting_success(tmp_path, monkeypatch) -> None:
    """Fix-round C2: a nonzero `enable --now` must abort loudly, not be
    silently discarded -- the bug this closes is exactly "migration reports
    success while every agent fails to start"."""
    legacy = _legacy_tree(tmp_path, monkeypatch)

    class EnableFailsRunner(FakeRunner):
        def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:4] == ["systemctl", "--user", "enable", "--now"]:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="Failed to enable unit: bad")
            if "is-active" in args:
                return subprocess.CompletedProcess(args, 0, stdout="inactive", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with pytest.raises(RuntimeError, match="enable"):
        migrate.run(EnableFailsRunner())

    assert not (paths.state_dir() / "layout-version").exists()
    # The backup still happened (it's unconditional, before any step runs) --
    # this isn't a "nothing happened" abort, it's "abort loudly instead of
    # reporting success", which is different from never having tried.
    assert list((legacy.parent).glob(f"{legacy.name}.bak-*"))


def test_disabling_an_already_disabled_legacy_unit_is_tolerated(tmp_path, monkeypatch) -> None:
    """Fix-round C2: `disable --now` returning nonzero (e.g. a resumed
    migration re-disabling a unit with nothing left to disable) must NOT
    abort the migration -- only a failed `enable --now` is fatal."""
    _legacy_tree(tmp_path, monkeypatch)

    class DisableFailsRunner(FakeRunner):
        def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:4] == ["systemctl", "--user", "disable", "--now"]:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="Unit not loaded")
            if "is-active" in args:
                return subprocess.CompletedProcess(args, 0, stdout="inactive", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    migrate.run(DisableFailsRunner())  # must not raise
    assert (paths.state_dir() / "layout-version").exists()


def test_backup_is_taken_before_anything_moves(tmp_path, monkeypatch) -> None:
    _legacy_tree(tmp_path, monkeypatch)
    migrate.run(FakeRunner())
    backups = list((Path.home()).glob(".config/buzz-fleet.bak-*"))
    assert len(backups) == 1
    assert (backups[0] / "agents" / "reviewer.env").exists()


def test_backup_root_is_tightened_to_0700(tmp_path, monkeypatch) -> None:
    """Fix-round I2: a real machine's `~/.config/buzz-fleet` (and its
    `communities/`/`agents/` subdirectories) was found at 0755 --
    `copytree` preserves that, so the backup's plaintext secrets would
    otherwise sit under a world-readable root."""
    _legacy_tree(tmp_path, monkeypatch)
    legacy = paths.legacy_dir()
    legacy.chmod(0o755)
    migrate.run(FakeRunner())
    backups = list((Path.home()).glob(".config/buzz-fleet.bak-*"))
    assert oct(backups[0].stat().st_mode & 0o777) == "0o700"


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


def test_interrupted_work_copy_is_redone_not_left_half_done(tmp_path, monkeypatch) -> None:
    """Fix-round C4: `plan()`'s postcondition is "does the work dir exist",
    so a naive direct `copytree` into the final path would let an
    interrupted copy be silently skipped forever on resume, reporting
    success with a truncated work directory. The atomic copy-then-rename
    fix means an interruption can only ever leave the SIBLING temp
    directory populated, never the final path itself -- this simulates
    exactly that (a killed process's leftover temp dir) and proves resume
    cleans it up and redoes the whole copy correctly."""
    _legacy_tree(tmp_path, monkeypatch)
    work_source = Path.home() / ".local" / "share" / "buzz-fleet" / "work" / "reviewer"
    work_source.mkdir(parents=True)
    (work_source / "real.txt").write_text("the real content")

    key = units.instance_key("eltahir", "reviewer")
    work_target = systemd.work_dir(key)
    stale_tmp = work_target.parent / f".{work_target.name}.migrating"
    stale_tmp.mkdir(parents=True)
    (stale_tmp / "partial.txt").write_text("leftover from a killed run")

    migrate.run(FakeRunner())

    assert not stale_tmp.exists()
    assert (work_target / "real.txt").read_text() == "the real content"
    assert not (work_target / "partial.txt").exists()


def test_work_dir_symlinks_are_preserved_not_dereferenced(tmp_path, monkeypatch) -> None:
    """Minor fix-round item: real work dirs contain symlinks (node_modules,
    .pi-agent/npm) -- copytree must pass symlinks=True, or a dangling link
    aborts the whole step instead of being copied as a link."""
    _legacy_tree(tmp_path, monkeypatch)
    work_source = Path.home() / ".local" / "share" / "buzz-fleet" / "work" / "reviewer"
    work_source.mkdir(parents=True)
    (work_source / "dangling").symlink_to(work_source / "does-not-exist")

    migrate.run(FakeRunner())  # must not raise on the dangling symlink

    key = units.instance_key("eltahir", "reviewer")
    work_target = systemd.work_dir(key)
    assert (work_target / "dangling").is_symlink()


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


def test_stranded_env_files_with_no_communities_dir_refuse_rather_than_hide(tmp_path, monkeypatch) -> None:
    """Fix-round I1: without `communities/`, there's no way to know which
    community a bare `agents/*.env` file belonged to -- reporting "nothing
    to do" while it sits stranded is worse than refusing loudly."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    legacy = home / ".config" / "buzz-fleet"
    (legacy / "agents").mkdir(parents=True)
    (legacy / "agents" / "orphan.env").write_text("BUZZ_PRIVATE_KEY=nsec1orphan\n")

    with pytest.raises(RuntimeError, match="agents"):
        migrate.run(FakeRunner())


def test_a_straggler_added_after_a_full_migration_is_still_found(tmp_path, monkeypatch) -> None:
    """Fix-round I1: `plan()` must not short-circuit off a whole-machine
    "already done" flag -- a community or agent an older binary wrote to the
    legacy tree AFTER a previous migration completed must still be found and
    migrated, not silently ignored forever."""
    legacy = _legacy_tree(tmp_path, monkeypatch)
    migrate.run(FakeRunner())
    assert (paths.state_dir() / "layout-version").exists()

    (legacy / "communities" / "eltahir" / "agents" / "straggler.json").write_text(
        '{"id":"straggler","community_id":"eltahir","display_name":"S","harness":"claude",'
        '"private_key":"nsec1straggler","public_key":"' + "b" * 64 + '",'
        '"system_prompt_source":{"kind":"inline","text":"yo"}}'
    )
    (legacy / "agents" / "straggler.env").write_text("BUZZ_PRIVATE_KEY=nsec1straggler\n")

    second = migrate.run(FakeRunner())

    assert second  # NOT a no-op
    assert (paths.state_dir() / "communities" / "eltahir" / "agents" / "straggler.json").exists()
    assert (paths.secrets_dir() / "units" / "eltahir:straggler.env").exists()


def test_deleting_a_migrated_agent_then_remigrating_does_not_resurrect_it(
    tmp_path, monkeypatch
) -> None:
    """Fix-round 2 MUST-FIX 2: `manager.delete_agent` only ever touches the
    NEW layout -- the agent's state JSON (state.delete_agent), its env file
    (systemd.agent_env_path), and its prompt (systemd.agent_prompt_path).
    It knows nothing about the still-present legacy files, and the legacy
    tree is deliberately kept around until the operator cleans it up by
    hand (README's "Upgrading from 0.8.x"). Before this fix, `plan()`
    re-derived every step straight from the legacy tree on every call, so
    deleting a migrated agent and re-running migrate silently recreated it
    -- BUZZ_PRIVATE_KEY and all -- directly undoing the deletion (and Fix
    4's whole point, that a deleted agent's key must not survive on disk).
    """
    legacy = _legacy_tree(tmp_path, monkeypatch)
    migrate.run(FakeRunner())

    # Simulate `buzz-fleet agent delete reviewer` -- exactly the three
    # NEW-layout files `manager.delete_agent` removes, nothing under legacy.
    (paths.state_dir() / "communities" / "eltahir" / "agents" / "reviewer.json").unlink()
    (paths.secrets_dir() / "communities" / "eltahir" / "agents" / "reviewer.json").unlink()
    (paths.secrets_dir() / "units" / "eltahir:reviewer.env").unlink()
    (paths.state_dir() / "units" / "eltahir:reviewer.prompt.md").unlink()

    second = migrate.run(FakeRunner())

    assert second == []  # the marker says this agent is done; nothing to redo
    assert not (paths.state_dir() / "communities" / "eltahir" / "agents" / "reviewer.json").exists()
    assert not (paths.secrets_dir() / "communities" / "eltahir" / "agents" / "reviewer.json").exists()
    assert not (paths.secrets_dir() / "units" / "eltahir:reviewer.env").exists()
    assert not (paths.state_dir() / "units" / "eltahir:reviewer.prompt.md").exists()
    # The legacy files are still there, untouched (as documented) -- the
    # fix is that migrate refuses to re-derive from them, not that it
    # deletes them once an agent's marker exists.
    assert (legacy / "agents" / "reviewer.env").exists()


def test_stray_env_files_already_migrated_do_not_block_a_clean_run(tmp_path, monkeypatch) -> None:
    """Fix-round 2 small item: the stray-`agents/`-with-no-`communities/`
    refusal must not fire for an env file that's simply left over after a
    successful migration plus a partial cleanup -- the README's own
    documented order deletes `communities/` before `agents/`, so a machine
    mid-cleanup legitimately has this shape and must still report "safe to
    re-run", not exit 1."""
    legacy = _legacy_tree(tmp_path, monkeypatch)
    migrate.run(FakeRunner())

    # Partial cleanup, per the README: communities/ deleted, agents/ not yet.
    shutil.rmtree(legacy / "communities")

    assert migrate.run(FakeRunner()) == []


def test_env_file_paths_into_the_legacy_tree_are_rewritten(tmp_path, monkeypatch) -> None:
    """The hazard a byte-for-byte copy would reintroduce: a copied-verbatim env
    file's BUZZ_ACP_SYSTEM_PROMPT_FILE still points at the legacy prompt path,
    which the migration deletes by moving it elsewhere. A test that only checks
    the env file was copied would pass against that broken version -- this one
    resolves the paths the migrated file actually points at and proves they
    exist (BUZZ_ACP_SYSTEM_PROMPT_FILE) or are the correct, executable file
    (BUZZ_ACP_MCP_COMMAND). Also covers PI_CODING_AGENT_DIR (fix-round I3):
    `_rewrite_env` is a plain substring replacement of the whole legacy work
    dir, so it rewrites every variable derived from it, not only
    BUZZ_ACP_MCP_COMMAND.
    """
    legacy = _legacy_tree(tmp_path, monkeypatch)
    old_prompt = legacy / "agents" / "reviewer.prompt.md"
    old_work = Path.home() / ".local" / "share" / "buzz-fleet" / "work" / "reviewer"
    old_work.mkdir(parents=True)
    (old_work / "mcp-search.sh").write_text("#!/bin/sh\nexec true\n")
    (old_work / "mcp-search.sh").chmod(0o700)
    (legacy / "agents" / "reviewer.env").write_text(
        "BUZZ_PRIVATE_KEY=nsec1agent\n"
        f"BUZZ_ACP_SYSTEM_PROMPT_FILE={old_prompt}\n"
        f"BUZZ_ACP_MCP_COMMAND={old_work}/mcp-search.sh\n"
        f"PI_CODING_AGENT_DIR={old_work}/.pi-agent\n"
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
    # Fix-round 2: path equality alone doesn't prove the work-dir copy
    # actually happened -- assert the file the rewritten path points at is
    # real and still executable (the work dir is genuinely copied by this
    # migration, so this is meaningful, not automatically true).
    assert mcp_command.is_file()
    assert mcp_command.stat().st_mode & 0o100
    assert str(old_work) not in moved.read_text()

    pi_dir = Path(env_values["PI_CODING_AGENT_DIR"])
    assert pi_dir == paths.data_dir() / "work" / "eltahir:reviewer" / ".pi-agent"


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
    _legacy_tree(tmp_path, monkeypatch)
    (paths.legacy_dir() / "communities" / "bad:id.json").write_text(
        '{"id":"bad:id","relay_url":"wss://r","relay_admin_nsec":"nsec1owner2"}'
    )

    with pytest.raises(RuntimeError, match="bad:id"):
        migrate.run(FakeRunner())

    # Nothing moved at all -- not even the perfectly valid "eltahir" community.
    assert not (paths.state_dir() / "communities" / "eltahir.json").exists()
    assert not (paths.state_dir() / "communities" / "bad:id.json").exists()


def test_refuses_the_whole_migration_for_an_invalid_legacy_agent_id(tmp_path, monkeypatch) -> None:
    """Fix-round minor item: an agent id that makes its community-qualified
    instance key invalid (a stray colon, here) must be caught the same way a
    bad community id is -- up front, as a clean RuntimeError -- rather than a
    bare ValueError surfacing out of `units.unit_name` mid-migration."""
    legacy = _legacy_tree(tmp_path, monkeypatch)
    (legacy / "communities" / "eltahir" / "agents" / "bad:agent.json").write_text(
        '{"id":"bad:agent","community_id":"eltahir","display_name":"B","harness":"claude",'
        '"private_key":"nsec1bad","public_key":"' + "c" * 64 + '",'
        '"system_prompt_source":{"kind":"inline","text":"hi"}}'
    )

    with pytest.raises(RuntimeError, match="bad:agent"):
        migrate.run(FakeRunner())

    assert not (paths.state_dir() / "communities" / "eltahir.json").exists()


def test_refuses_the_whole_migration_when_an_agent_id_collides_across_communities(
    tmp_path, monkeypatch
) -> None:
    """FIX 1 (Critical, final whole-branch review): the legacy layout keys
    env files, prompt files, and work dirs on the bare agent id GLOBALLY, so
    two communities that each contain an agent called "reviewer" share
    exactly ONE legacy env file and ONE legacy prompt file between them.
    Before this fix, `plan()` fanned that single shared file out to both
    destinations -- giving at least one agent the wrong `BUZZ_PRIVATE_KEY`,
    permanently, under a unit name that now asserts it's correct -- and a
    resumed run reported success with the second agent's
    `BUZZ_ACP_SYSTEM_PROMPT_FILE` pointing at a file the first run's prompt
    step had already unlinked. `plan()` must now catch this up front, the
    same as the id-validity checks above, and refuse before writing
    anything: merging two agents that share one legacy env file can't be
    done safely, since which key belongs to which agent isn't recorded
    anywhere on disk.
    """
    legacy = _two_community_legacy_tree(tmp_path, monkeypatch, second_agent_id="reviewer")

    with pytest.raises(RuntimeError, match="reviewer") as exc_info:
        migrate.run(FakeRunner())
    message = str(exc_info.value)
    assert "eltahir" in message
    assert "acme" in message

    # Nothing written at all -- not even the perfectly fine parts of either
    # community -- and no backup taken, same "refuse before touching
    # anything" contract the id-validity checks above already have.
    assert not (paths.state_dir() / "communities" / "eltahir.json").exists()
    assert not (paths.state_dir() / "communities" / "acme.json").exists()
    assert list(legacy.parent.glob(f"{legacy.name}.bak-*")) == []


def test_two_communities_with_distinct_agent_ids_migrate_correctly(tmp_path, monkeypatch) -> None:
    """The flip side of the collision test above, and the first test in this
    suite to actually exercise a normal (non-colliding) multi-community
    migration: two communities with distinct agent ids must both migrate,
    each agent's env file ending up with its OWN `BUZZ_PRIVATE_KEY`, never
    the other's. This is the test that proves the multi-community path
    works at all -- nothing in the suite exercised it before this fixture.
    """
    _two_community_legacy_tree(tmp_path, monkeypatch, second_agent_id="auditor")

    migrate.run(FakeRunner())

    reviewer_env = (paths.secrets_dir() / "units" / "eltahir:reviewer.env").read_text()
    auditor_env = (paths.secrets_dir() / "units" / "acme:auditor.env").read_text()
    assert "BUZZ_PRIVATE_KEY=nsec1agent\n" == reviewer_env
    assert "BUZZ_PRIVATE_KEY=nsec1other\n" == auditor_env
    assert "nsec1other" not in reviewer_env
    assert "nsec1agent" not in auditor_env


def test_created_at_backfill_is_printed_not_silent(tmp_path, monkeypatch, capsys) -> None:
    """Fix-round minor item: the mtime backfill for a legacy agent file
    missing `created_at` must be loud (naming the agent and the mtime used),
    not a silent `setdefault` -- nothing reads the field today, but the
    unbuilt recycling work would, and a fabricated timestamp would then be
    wrong."""
    _legacy_tree(tmp_path, monkeypatch)
    migrate.run(FakeRunner())
    captured = capsys.readouterr()
    # Fix-round 2: stderr, not stdout -- stdout is reserved for the CLI's
    # own step-by-step listing (each Step's description); interleaving this
    # note into that would make both harder to read or script against.
    assert "eltahir:reviewer" in captured.err
    assert "created_at" in captured.err
    assert "eltahir:reviewer" not in captured.out


def test_a_legacy_agent_file_missing_other_required_data_is_a_clean_runtime_error(
    tmp_path, monkeypatch
) -> None:
    """Fix-round minor item: any OTHER missing/invalid required field (not
    just the tolerated `created_at`) must raise a clear RuntimeError naming
    the file, not a bare pydantic ValidationError mid-apply."""
    legacy = _legacy_tree(tmp_path, monkeypatch)
    (legacy / "communities" / "eltahir" / "agents" / "reviewer.json").write_text(
        '{"id":"reviewer","community_id":"eltahir","display_name":"R"}'
    )

    with pytest.raises(RuntimeError, match="reviewer.json"):
        migrate.run(FakeRunner())
