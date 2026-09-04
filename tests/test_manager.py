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
        elif "add-member" in args or "remove-member" in args:
            stdout = json.dumps({"ok": True})
        else:
            stdout = "active\n"
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")


def _community() -> Community:
    return Community(id="eltahir", relay_url="wss://buzz.eltahir.me", relay_admin_nsec="nsec1admin")


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
