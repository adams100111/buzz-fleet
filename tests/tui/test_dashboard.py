from datetime import UTC, datetime

import pytest

from buzz_fleet.models import Agent, AgentVisibilityState, SystemPromptSource
from buzz_fleet.systemctl_client import AgentStatus
from buzz_fleet.tui.app import BuzzFleetApp
from buzz_fleet.tui.screens.dashboard import DashboardScreen, _visibility_display

# The "no connected community by default" isolation fixture lives in
# tests/tui/conftest.py — every file in this directory constructs a real
# BuzzFleetApp(), so it's shared rather than duplicated per file.


def _agent(agent_id: str = "test-agent", **overrides) -> Agent:
    # Shared builder for every test in this file: the pre-existing dashboard
    # tests below call `_agent("some-id")` positionally, while the
    # visibility tests need `visibility_managed`/`visibility_state`
    # overrides — merged into one helper (with `**overrides` folded into
    # its defaults) rather than keeping a second, colliding `_agent()`
    # definition side by side.
    defaults = {
        "id": agent_id,
        "community_id": "eltahir",
        "display_name": agent_id.title(),
        "harness": "claude",
        "private_key": "nsec1x",
        "public_key": "a" * 64,
        "system_prompt_source": SystemPromptSource(kind="inline", text="hi"),
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Agent(**defaults)


def test_visibility_display_old_agent_is_inactive_colored_dash() -> None:
    text, _color = _visibility_display(_agent())
    assert text == "—"


def test_visibility_display_synced_is_success_colored() -> None:
    agent = _agent(
        visibility_managed=True,
        visibility_state=AgentVisibilityState(
            profile_published=True, managed_agent_published=True, add_policy_published=True
        ),
    )
    text, _color = _visibility_display(agent)
    assert text == "synced"


def test_visibility_display_pending_is_warning_colored() -> None:
    text, _color = _visibility_display(_agent(visibility_managed=True))
    assert text == "pending"


def test_visibility_display_error_is_error_colored() -> None:
    agent = _agent(visibility_managed=True, visibility_state=AgentVisibilityState(profile_error="invalid: bad thing"))
    text, _color = _visibility_display(agent)
    assert text.startswith("error:")


@pytest.mark.asyncio
async def test_dashboard_lists_agents_with_status(monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.list_agents", lambda: [_agent("laravel-dev")])
    monkeypatch.setattr(
        "buzz_fleet.tui.screens.dashboard.agent_status",
        lambda agent_id: AgentStatus.RUNNING,
    )

    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        # Push DashboardScreen explicitly rather than relying on BuzzFleetApp's
        # automatic on_mount routing, which now depends on whether a community
        # is connected (Fix 6) — irrelevant to what this test is checking.
        await app.push_screen(DashboardScreen())
        await pilot.pause()
        table = app.screen.query_one("#agent-table")
        # Displayed status text borrows systemd's own vocabulary ("active"),
        # not the internal AgentStatus.RUNNING enum name. Trailing "—" is the
        # visibility column: this agent has visibility_managed=False (an old,
        # unmanaged agent), which is what the "—" dash denotes.
        assert ("laravel-dev", "Laravel-Dev", "claude", "active", "—") in [
            tuple(str(v) for v in table.get_row_at(i)) for i in range(table.row_count)
        ]


@pytest.mark.asyncio
async def test_create_agent_with_no_connected_community_does_not_crash(monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.list_agents", list)
    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.state.load_community", lambda community_id: None)

    created_managers = []
    monkeypatch.setattr(
        "buzz_fleet.tui.screens.dashboard.AgentManager",
        lambda *args, **kwargs: created_managers.append((args, kwargs)),
    )

    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        # Push DashboardScreen explicitly (see comment in the test above).
        await app.push_screen(DashboardScreen())
        await pilot.pause()
        screen = app.screen
        screen.action_create_agent()
        await pilot.pause()

    assert created_managers == []


@pytest.mark.asyncio
async def test_dashboard_refreshes_when_a_pushed_screen_is_popped(monkeypatch) -> None:
    """Regression test: creating/editing/cancelling an agent via AgentFormScreen

    (or viewing logs) pops back to an already-mounted DashboardScreen — whose
    on_mount() already ran once and won't run again. Without an explicit
    refresh on screen-resume, a newly created agent silently never appears
    in the table (this was a real bug: the relay membership + local state
    were saved correctly, the dashboard just never re-read them).
    """
    from textual.screen import Screen

    agents: list[Agent] = []
    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.list_agents", lambda: list(agents))
    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.agent_status", lambda agent_id: AgentStatus.RUNNING)

    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.push_screen(DashboardScreen())
        await pilot.pause()
        table = app.screen.query_one("#agent-table")
        assert table.row_count == 0

        # Simulate "a new agent was created while a screen was pushed on top":
        # push some other screen (stands in for AgentFormScreen/LogsScreen),
        # mutate the underlying data as create_agent would, then pop back —
        # exactly the sequence a real create/edit/cancel goes through.
        agents.append(_agent("new-agent"))
        await app.push_screen(Screen())
        await pilot.pause()
        await app.pop_screen()
        await pilot.pause()

        table = app.screen.query_one("#agent-table")
        assert table.row_count == 1
        assert table.get_row_at(0)[0] == "new-agent"


@pytest.mark.asyncio
async def test_refresh_heals_runtime_when_a_community_is_connected(monkeypatch) -> None:
    """Regression test for the buzz-acp-never-installed incident: opening
    the dashboard (or returning to it) must self-heal automatically — no
    command the user has to remember to run. Verifies the wiring only
    (that ensure_runtime_ready() is actually called when connected); the
    healing logic itself is covered directly in test_manager.py.
    """
    from unittest.mock import MagicMock

    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.list_agents", list)
    monkeypatch.setattr(
        "buzz_fleet.tui.screens.dashboard.state.load_community",
        lambda community_id: object(),
    )
    fake_manager = MagicMock()
    monkeypatch.setattr(
        "buzz_fleet.tui.screens.dashboard.AgentManager",
        lambda runner, community: fake_manager,
    )

    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.push_screen(DashboardScreen())
        await pilot.pause()

    # BuzzFleetApp.on_mount() also auto-pushes a DashboardScreen once it
    # sees a "connected" community (shared with the explicit push below via
    # the same monkeypatched state.load_community) — this test only cares
    # that healing happens when connected, not the exact call count.
    assert fake_manager.ensure_runtime_ready.called


@pytest.mark.asyncio
async def test_refresh_skips_healing_when_no_community_is_connected(monkeypatch) -> None:
    from unittest.mock import MagicMock

    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.list_agents", list)
    fake_manager_cls = MagicMock()
    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.AgentManager", fake_manager_cls)

    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.push_screen(DashboardScreen())
        await pilot.pause()

    fake_manager_cls.assert_not_called()


@pytest.mark.asyncio
async def test_view_logs_and_delete_on_empty_dashboard_does_not_crash(monkeypatch) -> None:
    # Regression test for Fix 2: DataTable.cursor_row is 0 (an int, never None)
    # on an empty table, so a guard checking `cursor_row is None` fails to
    # catch the empty-table case and get_row_at(0) raises RowDoesNotExist,
    # crashing the whole app when 'l'/'x'/'u' is pressed with no rows.
    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.list_agents", list)

    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.push_screen(DashboardScreen())
        await pilot.pause()
        screen = app.screen
        table = screen.query_one("#agent-table")
        assert table.row_count == 0

        # Must not raise.
        screen.action_view_logs()
        await pilot.pause()
        screen.action_delete_agent()
        await pilot.pause()
        screen.action_edit_agent()
        await pilot.pause()


@pytest.mark.asyncio
async def test_delete_agent_shows_confirmation_before_deleting(monkeypatch) -> None:
    """Regression test: deleting an agent is destructive and irreversible
    (it tears down the systemd unit and, for a managed agent, leaves every
    channel + retracts its relay records). A single 'x'/Delete keypress must
    never delete outright — it must show a confirmation modal first.
    """
    from unittest.mock import MagicMock

    from buzz_fleet.tui.screens.confirm_delete import ConfirmDeleteScreen

    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.list_agents", lambda: [_agent("laravel-dev")])
    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.agent_status", lambda agent_id: AgentStatus.RUNNING)
    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.state.load_community", lambda community_id: object())
    fake_manager = MagicMock()
    fake_manager.list_agents.return_value = [_agent("laravel-dev")]
    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.AgentManager", lambda runner, community: fake_manager)

    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.push_screen(DashboardScreen())
        await pilot.pause()
        screen = app.screen
        screen.action_delete_agent()
        await pilot.pause()

        assert isinstance(app.screen, ConfirmDeleteScreen)
        fake_manager.delete_agent.assert_not_called()


@pytest.mark.asyncio
async def test_confirming_delete_dialog_deletes_the_agent(monkeypatch) -> None:
    from unittest.mock import MagicMock

    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.list_agents", lambda: [_agent("laravel-dev")])
    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.agent_status", lambda agent_id: AgentStatus.RUNNING)
    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.state.load_community", lambda community_id: object())
    fake_manager = MagicMock()
    fake_manager.list_agents.return_value = [_agent("laravel-dev")]
    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.AgentManager", lambda runner, community: fake_manager)

    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.push_screen(DashboardScreen())
        await pilot.pause()
        screen = app.screen
        screen.action_delete_agent()
        await pilot.pause()

        await pilot.press("y")
        await pilot.pause()

        fake_manager.delete_agent.assert_called_once_with("laravel-dev")


@pytest.mark.asyncio
async def test_cancelling_delete_dialog_does_not_delete(monkeypatch) -> None:
    from unittest.mock import MagicMock

    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.list_agents", lambda: [_agent("laravel-dev")])
    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.agent_status", lambda agent_id: AgentStatus.RUNNING)
    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.state.load_community", lambda community_id: object())
    fake_manager = MagicMock()
    fake_manager.list_agents.return_value = [_agent("laravel-dev")]
    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.AgentManager", lambda runner, community: fake_manager)

    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.push_screen(DashboardScreen())
        await pilot.pause()
        screen = app.screen
        screen.action_delete_agent()
        await pilot.pause()

        await pilot.press("escape")
        await pilot.pause()

        fake_manager.delete_agent.assert_not_called()
        assert isinstance(app.screen, DashboardScreen)


@pytest.mark.asyncio
async def test_delete_key_binding_triggers_delete_confirmation(monkeypatch) -> None:
    """The Delete/Del key must work as an alias for 'x', per the same
    destructive-action confirmation flow."""
    from unittest.mock import MagicMock

    from buzz_fleet.tui.screens.confirm_delete import ConfirmDeleteScreen

    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.list_agents", lambda: [_agent("laravel-dev")])
    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.agent_status", lambda agent_id: AgentStatus.RUNNING)
    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.state.load_community", lambda community_id: object())
    fake_manager = MagicMock()
    fake_manager.list_agents.return_value = [_agent("laravel-dev")]
    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.AgentManager", lambda runner, community: fake_manager)

    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.push_screen(DashboardScreen())
        await pilot.pause()

        await pilot.press("delete")
        await pilot.pause()

        assert isinstance(app.screen, ConfirmDeleteScreen)
