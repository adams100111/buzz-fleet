import json
import subprocess
from types import SimpleNamespace

from typer.testing import CliRunner

from buzz_fleet.cli.app import app

runner_cli = CliRunner()


class FakeRunner:
    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"ok": True}), stderr="")


def test_connect_saves_community_on_success(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.cli.app.RealCommandRunner", lambda: FakeRunner())

    result = runner_cli.invoke(
        app,
        ["connect", "--id", "eltahir", "--relay", "wss://buzz.eltahir.me", "--admin-nsec", "nsec1abc"],
    )

    assert result.exit_code == 0
    from buzz_fleet.state import load_community

    saved = load_community("eltahir")
    assert saved is not None
    assert saved.relay_url == "wss://buzz.eltahir.me"


def test_agent_update_calls_manager_with_changes(monkeypatch) -> None:
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
        ["agent", "update", "--community", "eltahir", "agent-1", "--display-name", "New Name"],
    )

    assert result.exit_code == 0, result.output
    assert calls["agent_id"] == "agent-1"
    assert calls["changes"] == {"display_name": "New Name"}


def test_agent_update_with_no_changes_exits_with_error(monkeypatch) -> None:
    class FakeAgentManager:
        def __init__(self, runner: object, community: object) -> None:
            pass

        def update_agent(self, agent_id: str, **changes: object) -> object:
            raise AssertionError("update_agent should not be called when there are no changes")

    monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)

    result = runner_cli.invoke(app, ["agent", "update", "--community", "eltahir", "agent-1"])

    assert result.exit_code == 1
    assert "Nothing to update" in result.output
