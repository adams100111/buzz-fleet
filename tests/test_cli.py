import json
import subprocess

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
