"""`community use` is the toggle for the command line. The picker screen in
Task 10 calls the same state function."""

import json

from pydantic import SecretStr
from typer.testing import CliRunner

from buzz_fleet import state
from buzz_fleet.cli.app import app
from buzz_fleet.models import Community

runner_cli = CliRunner()


def _connect(monkeypatch, tmp_path, cid: str) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("BUZZ_FLEET_COMMUNITY", raising=False)
    state.save_community(
        Community(id=cid, relay_url=f"wss://{cid}.example", relay_admin_nsec=SecretStr("nsec1x"))
    )


def test_list_shows_every_community(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    result = runner_cli.invoke(app, ["community", "list"])
    assert result.exit_code == 0
    assert "eltahir" in result.stdout
    assert "acme" in result.stdout


def test_list_marks_the_active_community(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    state.save_active_community("acme")
    lines = runner_cli.invoke(app, ["community", "list"]).stdout.splitlines()
    marked = [line for line in lines if line.startswith("*")]
    assert marked == ["* acme"]


def test_list_json_reports_active(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    state.save_active_community("acme")
    payload = json.loads(runner_cli.invoke(app, ["community", "list", "--json"]).stdout)
    assert {"id": "acme", "active": True} in payload
    assert {"id": "eltahir", "active": False} in payload


def test_list_with_no_communities_points_at_connect(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    result = runner_cli.invoke(app, ["community", "list"])
    assert result.exit_code == 0
    assert "buzz-fleet connect" in result.stdout


def test_use_sets_the_active_community(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    result = runner_cli.invoke(app, ["community", "use", "acme"])
    assert result.exit_code == 0
    assert state.load_active_community() == "acme"


def test_use_rejects_an_unknown_community(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    result = runner_cli.invoke(app, ["community", "use", "nope"])
    assert result.exit_code == 1
    assert "eltahir" in result.stdout


def test_use_does_not_change_the_pointer_on_rejection(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    state.save_active_community("eltahir")
    runner_cli.invoke(app, ["community", "use", "nope"])
    assert state.load_active_community() == "eltahir"


def test_show_reports_the_active_community(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    state.save_active_community("eltahir")
    out = runner_cli.invoke(app, ["community", "show"]).stdout
    assert "eltahir" in out
    assert "wss://eltahir.example" in out


def test_show_without_an_active_community_fails(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    result = runner_cli.invoke(app, ["community", "show"])
    assert result.exit_code == 1
    assert "community use" in result.stdout
