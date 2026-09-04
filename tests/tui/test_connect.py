"""Tests for the ConnectScreen (Fix 6) and the shared connect_and_save logic."""

from __future__ import annotations

import json
import subprocess

import pytest
from textual.widgets import Input

from buzz_fleet import state
from buzz_fleet.connect import connect_and_save
from buzz_fleet.tui.app import BuzzFleetApp
from buzz_fleet.tui.screens.connect import CURRENT_COMMUNITY_ID, ConnectScreen
from buzz_fleet.tui.screens.dashboard import DashboardScreen


class FakeRunner:
    def __init__(self, ok: bool) -> None:
        self._ok = ok
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"ok": self._ok}), stderr="")


def test_connect_and_save_saves_community_on_success(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    runner = FakeRunner(ok=True)

    result = connect_and_save(runner, "eltahir", "wss://buzz.eltahir.me", "nsec1abc")

    assert result is True
    saved = state.load_community("eltahir")
    assert saved is not None
    assert saved.relay_url == "wss://buzz.eltahir.me"


def test_connect_and_save_does_not_save_on_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    runner = FakeRunner(ok=False)

    result = connect_and_save(runner, "eltahir", "wss://buzz.eltahir.me", "nsec1bad")

    assert result is False
    assert state.load_community("eltahir") is None


@pytest.mark.asyncio
async def test_connect_screen_success_switches_to_dashboard(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr(
        "buzz_fleet.tui.screens.connect.RealCommandRunner", lambda: FakeRunner(ok=True)
    )

    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await app.push_screen(ConnectScreen())
        await pilot.pause()
        app.screen.query_one("#relay-input", Input).value = "wss://buzz.eltahir.me"
        app.screen.query_one("#nsec-input", Input).value = "nsec1abc"
        await pilot.click("#connect-button")
        await pilot.pause()

        assert isinstance(app.screen, DashboardScreen)

    assert state.load_community(CURRENT_COMMUNITY_ID) is not None


@pytest.mark.asyncio
async def test_connect_screen_failure_stays_on_screen_and_does_not_save(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr(
        "buzz_fleet.tui.screens.connect.RealCommandRunner", lambda: FakeRunner(ok=False)
    )

    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await app.push_screen(ConnectScreen())
        await pilot.pause()
        app.screen.query_one("#relay-input", Input).value = "wss://buzz.eltahir.me"
        app.screen.query_one("#nsec-input", Input).value = "nsec1bad"
        await pilot.click("#connect-button")
        await pilot.pause()

        assert isinstance(app.screen, ConnectScreen)

    assert state.load_community(CURRENT_COMMUNITY_ID) is None
