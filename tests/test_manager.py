import json
import subprocess
from pathlib import Path

import pytest

from buzz_fleet.manager import AgentManager
from buzz_fleet.models import Community, SystemPromptSource
from buzz_fleet.systemd import agent_env_path, agent_prompt_path


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:2] == ["buzz-fleet-signer", "generate-key"]:
            stdout = json.dumps({"public_key": "ab" * 32, "secret_key": "nsec1agent"})
        elif args[:2] == ["buzz-fleet-signer", "pubkey-from-nsec"]:
            stdout = json.dumps({"ok": True, "public_key": "c" * 64})
        elif "add-member" in args or "remove-member" in args:
            stdout = json.dumps({"ok": True})
        elif args[:2] == ["loginctl", "show-user"]:
            stdout = "yes"  # already lingering — the common case in these tests
        elif args[:2] == ["loginctl", "enable-linger"]:
            stdout = ""
        else:
            stdout = "active\n"
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")


def _community() -> Community:
    return Community(id="eltahir", relay_url="wss://buzz.eltahir.me", relay_admin_nsec="nsec1admin")


@pytest.fixture(autouse=True)
def _buzz_acp_already_installed(tmp_path: Path, monkeypatch) -> None:
    """Every test in this file exercises `create_agent`, which now calls
    `ensure_runtime_ready()` -> `buzz_acp.ensure_buzz_acp_installed()`.
    Without this fixture, every test run would perform a REAL network
    download into the real user's home directory — pre-seed an
    already-installed, executable stub so it's a safe no-op by default.
    Tests that specifically want the "just installed" self-heal path
    override `buzz_acp.ensure_buzz_acp_installed` directly instead.
    """
    from buzz_fleet import buzz_acp

    acp_dir = tmp_path / "buzz-acp-bin"
    acp_dir.mkdir()
    stub = acp_dir / "buzz-acp"
    stub.write_bytes(b"stub")
    stub.chmod(0o755)
    monkeypatch.setattr(buzz_acp, "BUZZ_ACP_DIR", acp_dir)
    monkeypatch.setattr(buzz_acp, "BUZZ_ACP_PATH", stub)


def test_create_agent_mints_key_registers_and_starts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())

    agent = manager.create_agent(
        display_name="Laravel Backend Dev",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="You are the dev."),
    )

    assert agent.id == "laravel-backend-dev"
    assert agent.public_key == "ab" * 32
    add_member_call = next(c for c in runner.calls if "add-member" in c)
    assert "--pubkey" in add_member_call and "ab" * 32 in add_member_call
    assert ["systemctl", "--user", "enable", "--now", "buzz-agent@laravel-backend-dev"] in runner.calls
    assert manager.list_agents() == [agent]


def test_delete_agent_removes_member_and_stops_unit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Throwaway",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )

    manager.delete_agent(agent.id)

    assert ["systemctl", "--user", "disable", "--now", "buzz-agent@throwaway"] in runner.calls
    assert any("remove-member" in c for c in runner.calls)
    assert manager.list_agents() == []


def test_update_agent_restarts_without_re_registering(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Throwaway",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    add_member_calls_before = len([c for c in runner.calls if "add-member" in c])

    updated = manager.update_agent(agent.id, system_prompt_source=SystemPromptSource(kind="inline", text="y"))

    add_member_calls_after = len([c for c in runner.calls if "add-member" in c])
    assert add_member_calls_after == add_member_calls_before
    assert ["systemctl", "--user", "restart", "buzz-agent@throwaway"] in runner.calls
    assert updated.system_prompt_source.text == "y"


def test_create_agent_with_missing_persona_file_fails_before_add_member(tmp_path: Path, monkeypatch) -> None:
    """Regression test for Fix 3.

    A missing/invalid persona_file path must fail loudly before any relay-side
    effect (add-member) happens — otherwise the relay membership is published
    but never recorded locally, orphaning it with no way to revoke it.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())

    with pytest.raises(FileNotFoundError):
        manager.create_agent(
            display_name="Broken Persona",
            harness="claude",
            system_prompt_source=SystemPromptSource(kind="persona_file", path=Path("/nonexistent/persona.md")),
        )

    assert not any("add-member" in c for c in runner.calls)


def test_delete_agent_removes_env_and_prompt_files(tmp_path: Path, monkeypatch) -> None:
    """Regression test for Fix 4: deleting an agent must remove its
    private-key-bearing .env file and its .prompt.md file, not just the state
    JSON — otherwise the secret survives "deletion" on disk.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Throwaway",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    assert agent_env_path(agent.id).exists()
    assert agent_prompt_path(agent.id).exists()

    manager.delete_agent(agent.id)

    assert not agent_env_path(agent.id).exists()
    assert not agent_prompt_path(agent.id).exists()


def test_update_agent_preserves_previously_set_api_keys(tmp_path: Path, monkeypatch) -> None:
    """Regression test for Fix 9: update_agent must not wipe a previously-set
    ANTHROPIC_API_KEY/OPENAI_API_KEY when the update doesn't touch keys at all.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Keyed Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
        anthropic_api_key="sk-ant-test",
    )
    assert "ANTHROPIC_API_KEY=sk-ant-test" in agent_env_path(agent.id).read_text()

    manager.update_agent(agent.id, display_name="Keyed Agent Renamed")

    env_content = agent_env_path(agent.id).read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-test" in env_content


class FailingEnableRunner(FakeRunner):
    """Fails only `systemctl ... enable --now ...` — e.g. no `loginctl
    enable-linger` yet on a fresh host, the literal first-run condition.
    """

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if "enable" in args and "--now" in args:
            self.calls.append(args)
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="Failed to connect to bus: No medium found"
            )
        return super().run(args)


def test_create_agent_is_recorded_locally_even_if_enable_now_fails(tmp_path: Path, monkeypatch) -> None:
    """Regression test: a failed `enable_now` must not orphan the relay
    membership + private-key env file that were already published/written —
    the agent must still be discoverable (and therefore deletable/retryable)
    via `list_agents()` even though its unit never actually started.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FailingEnableRunner()
    manager = AgentManager(runner, _community())

    with pytest.raises(RuntimeError):
        manager.create_agent(
            display_name="Orphan Test",
            harness="claude",
            system_prompt_source=SystemPromptSource(kind="inline", text="x"),
        )

    recorded = manager.list_agents()
    assert len(recorded) == 1
    assert recorded[0].id == "orphan-test"
    add_member_call = next(c for c in runner.calls if "add-member" in c)
    assert add_member_call  # membership was published — the local record above is what makes it revocable


class LingerCantEnableRunner(FakeRunner):
    """Fails only `loginctl enable-linger` — e.g. a non-active SSH session
    without polkit permission to self-enable it.
    """

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["loginctl", "show-user"]:
            self.calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="no", stderr="")
        if args[:2] == ["loginctl", "enable-linger"]:
            self.calls.append(args)
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="Interactive authentication required."
            )
        return super().run(args)


def test_create_agent_fails_before_any_side_effect_when_linger_cannot_be_enabled(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression test: if lingering can't be auto-enabled (needs a manual
    one-time `sudo`), create_agent must fail immediately with a clear
    message — before minting a key, publishing relay membership, or writing
    any files — not fail confusingly later at enable_now.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = LingerCantEnableRunner()
    manager = AgentManager(runner, _community())

    with pytest.raises(RuntimeError, match="sudo loginctl enable-linger"):
        manager.create_agent(
            display_name="Never Created",
            harness="claude",
            system_prompt_source=SystemPromptSource(kind="inline", text="x"),
        )

    assert manager.list_agents() == []
    assert not any("generate-key" in c for c in runner.calls)
    assert not any("add-member" in c for c in runner.calls)


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


def test_ensure_runtime_ready_restarts_existing_agents_when_buzz_acp_just_installed(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression test for the real incident: buzz-fleet never installed
    buzz-acp itself, so every agent's unit crash-looped forever with no way
    to notice short of a human running `systemctl --user status` by hand.
    ensure_runtime_ready() must restart already-existing agents the moment
    it (re)installs buzz-acp, so a previously-broken agent heals itself the
    next time the dashboard loads or any agent command runs — never a
    restart when buzz-acp was already fine (that would restart healthy
    agents on every single call, not just the one that matters).
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    first = manager.create_agent(
        display_name="First Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    second = manager.create_agent(
        display_name="Second Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    runner.calls.clear()

    from buzz_fleet import buzz_acp

    monkeypatch.setattr(buzz_acp, "ensure_buzz_acp_installed", lambda: True)

    manager.ensure_runtime_ready()

    assert ["systemctl", "--user", "restart", f"buzz-agent@{first.id}"] in runner.calls
    assert ["systemctl", "--user", "restart", f"buzz-agent@{second.id}"] in runner.calls


def test_ensure_runtime_ready_does_not_restart_agents_when_buzz_acp_already_installed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    manager.create_agent(
        display_name="First Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    runner.calls.clear()

    manager.ensure_runtime_ready()

    assert not any(c[:3] == ["systemctl", "--user", "restart"] for c in runner.calls)


def test_ensure_runtime_ready_refreshes_agent_whose_adapter_command_is_now_resolvable(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression test for the real, second half of the incident: installing

    a harness adapter *after* an agent already exists doesn't help that
    agent on its own — its .env file still has the stale command written
    before the adapter existed, and systemd's own PATH won't pick up a
    version-manager-installed binary regardless. ensure_runtime_ready()
    must notice the now-resolvable command differs from what's on disk and
    heal it, without needing buzz-acp itself to have just been installed.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")

    from buzz_fleet import harnesses

    monkeypatch.setattr(harnesses.shutil, "which", lambda cmd: None)  # not resolvable at create time
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Codex Bot",
        harness="codex",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    env_path = agent_env_path(agent.id)
    assert "BUZZ_ACP_AGENT_COMMAND=codex-acp" in env_path.read_text()
    runner.calls.clear()

    # The adapter is "installed" now — resolvable to an absolute path.
    monkeypatch.setattr(
        harnesses.shutil,
        "which",
        lambda cmd: "/home/dev/.local/share/mise/installs/node/22/bin/codex-acp"
        if cmd == "codex-acp"
        else None,
    )

    manager.ensure_runtime_ready()

    assert (
        "BUZZ_ACP_AGENT_COMMAND=/home/dev/.local/share/mise/installs/node/22/bin/codex-acp"
        in env_path.read_text()
    )
    assert ["systemctl", "--user", "restart", f"buzz-agent@{agent.id}"] in runner.calls


def test_ensure_runtime_ready_continues_healing_other_agents_if_one_restart_fails(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")

    class FlakyRestartRunner(FakeRunner):
        def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:3] == ["systemctl", "--user", "restart"] and "first-agent" in args[3]:
                self.calls.append(args)
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")
            return super().run(args)

    runner = FlakyRestartRunner()
    manager = AgentManager(runner, _community())
    manager.create_agent(
        display_name="First Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    second = manager.create_agent(
        display_name="Second Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    runner.calls.clear()

    from buzz_fleet import buzz_acp

    monkeypatch.setattr(buzz_acp, "ensure_buzz_acp_installed", lambda: True)

    manager.ensure_runtime_ready()  # must not raise despite the first restart failing

    assert ["systemctl", "--user", "restart", f"buzz-agent@{second.id}"] in runner.calls
