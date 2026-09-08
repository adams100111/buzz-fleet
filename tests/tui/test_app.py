from unittest.mock import MagicMock

import pytest

from buzz_fleet import paths, state
from buzz_fleet.models import Community
from buzz_fleet.tui.app import BuzzFleetApp
from buzz_fleet.tui.screens.connect import ConnectScreen
from buzz_fleet.tui.screens.dashboard import DashboardScreen


@pytest.mark.asyncio
async def test_buzz_fleet_theme_is_registered_and_active() -> None:
    app = BuzzFleetApp()

    async with app.run_test():
        assert app.theme == "buzz-fleet"
        theme = app.get_theme("buzz-fleet")
        assert theme.primary == "#D9A73B"
        assert theme.background == "#16130D"


@pytest.mark.asyncio
async def test_malformed_config_with_one_community_still_reaches_dashboard(monkeypatch) -> None:
    """Regression test: config.load() raises ValueError for a malformed
    config.toml, and on_mount used to catch only RuntimeError — an unhandled
    ValueError crashed the TUI at startup, even for a single-community user
    who has nothing ambiguous to resolve at all."""
    # DashboardScreen's own on_mount self-heals via a real AgentManager,
    # which would otherwise shell out to the real buzz-fleet-signer binary
    # with this fake nsec — irrelevant to what this test checks.
    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.list_agents", lambda community_id: [])
    monkeypatch.setattr(
        "buzz_fleet.tui.screens.dashboard.AgentManager", lambda runner, community: MagicMock()
    )
    cfg = paths.config_dir() / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("this is not valid toml [[[")
    state.save_community(Community(id="eltahir", relay_url="wss://r", relay_admin_nsec="nsec1x"))

    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await pilot.pause()

        assert isinstance(app.screen, DashboardScreen)
        assert app.screen._community_id == "eltahir"


@pytest.mark.asyncio
async def test_malformed_config_with_several_communities_falls_back_to_connect_and_notifies() -> None:
    """With more than one community and a malformed config.toml, resolution
    genuinely can't succeed — on_mount must not crash, and must say why the
    connect screen appeared rather than leaving the user baffled."""
    cfg = paths.config_dir() / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("this is not valid toml [[[")
    state.save_community(Community(id="eltahir", relay_url="wss://r", relay_admin_nsec="nsec1x"))
    state.save_community(Community(id="acme", relay_url="wss://r", relay_admin_nsec="nsec1x"))

    app = BuzzFleetApp()
    app.notify = MagicMock()
    async with app.run_test() as pilot:
        await pilot.pause()

        assert isinstance(app.screen, ConnectScreen)

    app.notify.assert_called_once()
    (message,), kwargs = app.notify.call_args
    assert "not valid TOML" in message
    assert kwargs.get("severity") == "warning"


@pytest.mark.asyncio
async def test_no_local_community_falls_back_to_connect_and_notifies_why() -> None:
    app = BuzzFleetApp()
    app.notify = MagicMock()
    async with app.run_test() as pilot:
        await pilot.pause()

        assert isinstance(app.screen, ConnectScreen)

    app.notify.assert_called_once()
    (message,), kwargs = app.notify.call_args
    assert "buzz-fleet connect" in message
    assert kwargs.get("severity") == "warning"


@pytest.mark.asyncio
async def test_stale_pointer_falls_back_to_connect_and_notifies(monkeypatch) -> None:
    """Regression test: the old on_mount checked
    state.load_community(...) is None before pushing a dashboard. Without
    that guard, a stale BUZZ_FLEET_COMMUNITY (an explicit intent that
    resolve_community_id correctly does not existence-check itself) would
    push a DashboardScreen for a community that no longer exists locally —
    an empty table with nothing wrong-looking until a key is pressed."""
    monkeypatch.setenv("BUZZ_FLEET_COMMUNITY", "gone")
    state.save_community(Community(id="eltahir", relay_url="wss://r", relay_admin_nsec="nsec1x"))

    app = BuzzFleetApp()
    app.notify = MagicMock()
    async with app.run_test() as pilot:
        await pilot.pause()

        assert isinstance(app.screen, ConnectScreen)

    app.notify.assert_called_once()
    (message,), kwargs = app.notify.call_args
    assert "gone" in message
    assert kwargs.get("severity") == "warning"
