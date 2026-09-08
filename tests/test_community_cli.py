"""`community use` is the toggle for the command line. The picker screen in
Task 10 calls the same state function."""

import json

from pydantic import SecretStr
from typer.testing import CliRunner

from buzz_fleet import paths, state
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
    # Several communities, none active: a state to display, not an error —
    # the hint line must still tell the user how to pick one.
    assert "No active community. Run `buzz-fleet community use <id>`." in result.stdout


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
    # This is printed with err=True, matching the try/except -> echo(err=True)
    # -> Exit(1) shape used everywhere else in this module — .output (not
    # .stdout) is what captures stderr under this repo's pinned Typer.
    assert "eltahir" in result.output


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
    # err=True, same reasoning as test_use_rejects_an_unknown_community above.
    assert "community use" in result.output


def _malformed_config(tmp_path) -> None:
    """Genuine garbage, not valid TOML with a bad value — the same shape
    tests/test_identity.py and tests/tui/test_app.py already use for this
    regression."""
    cfg = paths.config_dir() / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("this is not valid toml [[[")


def test_list_survives_a_malformed_config_toml(monkeypatch, tmp_path) -> None:
    """resolve_community_id calls config.load() internally, which raises
    ValueError for a malformed config.toml — with several communities and no
    active pointer, resolution reaches that call. `_active_or_none()` must
    catch ValueError as well as RuntimeError, or this tracebacks instead of
    just showing nothing marked active (the exact bug fixed in the TUI in
    Task 7)."""
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    _malformed_config(tmp_path)
    result = runner_cli.invoke(app, ["community", "list"])
    assert result.exit_code == 0
    assert "eltahir" in result.stdout
    assert "acme" in result.stdout
    assert "No active community. Run `buzz-fleet community use <id>`." in result.stdout


def test_show_survives_a_malformed_config_toml(monkeypatch, tmp_path) -> None:
    """Same regression as test_list_survives_a_malformed_config_toml, but for
    `community show`: it must fail with the ordinary "no active community"
    message and exit 1, not a raw ValueError traceback."""
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    _malformed_config(tmp_path)
    result = runner_cli.invoke(app, ["community", "show"])
    assert result.exit_code == 1
    assert "community use" in result.output
