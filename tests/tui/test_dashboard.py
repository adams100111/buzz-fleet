from datetime import UTC, datetime

import pytest

from buzz_fleet.models import Agent, SystemPromptSource
from buzz_fleet.tui.app import BuzzFleetApp
from buzz_fleet.tui.screens.dashboard import DashboardScreen


def _agent(agent_id: str) -> Agent:
    return Agent(
        id=agent_id,
        community_id="eltahir",
        display_name=agent_id.title(),
        harness="claude",
        private_key="nsec1x",
        public_key="a" * 64,
        system_prompt_source=SystemPromptSource(kind="inline", text="hi"),
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_dashboard_lists_agents_with_status(monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.tui.screens.dashboard.list_agents", lambda: [_agent("laravel-dev")])
    monkeypatch.setattr(
        "buzz_fleet.tui.screens.dashboard.agent_status",
        lambda agent_id: "RUNNING",
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
        assert ("laravel-dev", "Laravel-Dev", "claude", "RUNNING") in [
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
