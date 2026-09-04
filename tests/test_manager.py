import json
import subprocess
from pathlib import Path

from buzz_fleet.manager import AgentManager
from buzz_fleet.models import Community, SystemPromptSource


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
