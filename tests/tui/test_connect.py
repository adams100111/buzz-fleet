"""Tests for the ConnectScreen (Fix 6) and the shared connect_and_save logic."""

from __future__ import annotations

import json
import subprocess

import pytest
from textual.widgets import Input

from buzz_fleet import state
from buzz_fleet.connect import connect_and_save
from buzz_fleet.tui.app import BuzzFleetApp
from buzz_fleet.tui.screens.connect import ConnectScreen
from buzz_fleet.tui.screens.dashboard import DashboardScreen

# The screen no longer hardcodes a community id (that's the point of this
# task) — tests that drive it through the UI type this into #id-input.
# Deliberately NOT "eltahir" (the value the deleted CURRENT_COMMUNITY_ID
# constant held): a test using that same literal would still pass against a
# regression that re-hardcoded it and ignored #id-input entirely.
COMMUNITY_ID = "acme"


class FakeRunner:
    def __init__(self, ok: bool) -> None:
        self._ok = ok
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[1:2] == ["pubkey-from-nsec"]:
            payload = {"ok": True, "public_key": "a" * 64} if self._ok else {"ok": False, "error": "bad nsec"}
        else:
            payload = {"ok": self._ok}
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")


def test_connect_and_save_saves_community_on_success(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    runner = FakeRunner(ok=True)

    result = connect_and_save(runner, "eltahir", "wss://buzz.eltahir.me", "nsec1abc")

    assert result is True
    saved = state.load_community("eltahir")
    assert saved is not None
    assert saved.relay_url == "wss://buzz.eltahir.me"


def test_connect_and_save_does_not_save_on_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    runner = FakeRunner(ok=False)

    result = connect_and_save(runner, "eltahir", "wss://buzz.eltahir.me", "nsec1bad")

    assert result is False
    assert state.load_community("eltahir") is None


def test_connect_and_save_preserves_display_name_and_fleet_record_on_reconnect(
    tmp_path, monkeypatch
) -> None:
    """Reconnecting an existing id must not wipe fields the user is not
    re-supplying — only relay_url, the admin nsec and owner_pubkey are
    actually being re-entered on the connect screen."""
    from buzz_fleet.models import Community
    from buzz_fleet.orchestration.record import FleetRecord

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_community(
        Community(
            id="eltahir",
            relay_url="wss://old.example",
            relay_admin_nsec="nsec1old",
            owner_pubkey="0" * 64,
            display_name="The Eltahir Household",
            fleet_channel_id="6f1c0000-0000-4000-8000-000000000000",
            fleet_record=FleetRecord(retrieval_key="r" * 64, created_at=1),
        )
    )
    runner = FakeRunner(ok=True)

    result = connect_and_save(runner, "eltahir", "wss://new.example", "nsec1new")

    assert result is True
    reconnected = state.load_community("eltahir")
    assert reconnected is not None
    assert reconnected.relay_url == "wss://new.example"
    assert reconnected.relay_admin_nsec.get_secret_value() == "nsec1new"
    assert reconnected.display_name == "The Eltahir Household"
    assert reconnected.fleet_channel_id == "6f1c0000-0000-4000-8000-000000000000"
    assert reconnected.fleet_record is not None
    assert reconnected.fleet_record.retrieval_key == "r" * 64


@pytest.mark.asyncio
async def test_connect_screen_success_switches_to_dashboard(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        "buzz_fleet.tui.screens.connect.RealCommandRunner", lambda: FakeRunner(ok=True)
    )

    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await app.push_screen(ConnectScreen())
        await pilot.pause()
        app.screen.query_one("#id-input", Input).value = COMMUNITY_ID
        app.screen.query_one("#relay-input", Input).value = "wss://buzz.eltahir.me"
        app.screen.query_one("#nsec-input", Input).value = "nsec1abc"
        await pilot.click("#connect-button")
        await pilot.pause()

        assert isinstance(app.screen, DashboardScreen)
        assert app.screen._community_id == COMMUNITY_ID

    assert state.load_community(COMMUNITY_ID) is not None
    assert state.load_active_community() == COMMUNITY_ID


@pytest.mark.asyncio
async def test_connect_screen_rejects_an_unsafe_id_and_does_not_save(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        "buzz_fleet.tui.screens.connect.RealCommandRunner", lambda: FakeRunner(ok=True)
    )

    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await app.push_screen(ConnectScreen())
        await pilot.pause()
        app.screen.query_one("#id-input", Input).value = "../escape"
        app.screen.query_one("#relay-input", Input).value = "wss://buzz.eltahir.me"
        app.screen.query_one("#nsec-input", Input).value = "nsec1abc"
        await pilot.click("#connect-button")
        await pilot.pause()

        assert isinstance(app.screen, ConnectScreen)

    assert state.list_community_ids() == []


@pytest.mark.asyncio
async def test_connect_screen_requires_an_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        "buzz_fleet.tui.screens.connect.RealCommandRunner", lambda: FakeRunner(ok=True)
    )

    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await app.push_screen(ConnectScreen())
        await pilot.pause()
        # Deliberately leave #id-input blank.
        app.screen.query_one("#relay-input", Input).value = "wss://buzz.eltahir.me"
        app.screen.query_one("#nsec-input", Input).value = "nsec1abc"
        await pilot.click("#connect-button")
        await pilot.pause()

        assert isinstance(app.screen, ConnectScreen)

    assert state.list_community_ids() == []


@pytest.mark.asyncio
async def test_connect_screen_failure_stays_on_screen_and_does_not_save(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        "buzz_fleet.tui.screens.connect.RealCommandRunner", lambda: FakeRunner(ok=False)
    )

    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await app.push_screen(ConnectScreen())
        await pilot.pause()
        app.screen.query_one("#id-input", Input).value = COMMUNITY_ID
        app.screen.query_one("#relay-input", Input).value = "wss://buzz.eltahir.me"
        app.screen.query_one("#nsec-input", Input).value = "nsec1bad"
        await pilot.click("#connect-button")
        await pilot.pause()

        assert isinstance(app.screen, ConnectScreen)

    assert state.load_community(COMMUNITY_ID) is None


@pytest.mark.asyncio
async def test_connecting_a_second_community_adds_rather_than_overwrites(tmp_path, monkeypatch) -> None:
    """The headline behavior this task adds: connecting used to always
    overwrite the single hardcoded "eltahir" community. Now it must add a
    new one alongside whatever is already saved."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        "buzz_fleet.tui.screens.connect.RealCommandRunner", lambda: FakeRunner(ok=True)
    )

    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await app.push_screen(ConnectScreen())
        await pilot.pause()
        app.screen.query_one("#id-input", Input).value = "eltahir"
        app.screen.query_one("#relay-input", Input).value = "wss://buzz.eltahir.me"
        app.screen.query_one("#nsec-input", Input).value = "nsec1abc"
        await pilot.click("#connect-button")
        await pilot.pause()

        assert isinstance(app.screen, DashboardScreen)

        await app.push_screen(ConnectScreen())
        await pilot.pause()
        app.screen.query_one("#id-input", Input).value = "acme"
        app.screen.query_one("#relay-input", Input).value = "wss://buzz.acme.example"
        app.screen.query_one("#nsec-input", Input).value = "nsec1def"
        await pilot.click("#connect-button")
        await pilot.pause()

        assert isinstance(app.screen, DashboardScreen)
        assert app.screen._community_id == "acme"

    assert sorted(state.list_community_ids()) == ["acme", "eltahir"]
    assert state.load_community("eltahir") is not None
    assert state.load_community("acme") is not None
    assert state.load_active_community() == "acme"
