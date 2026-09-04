from datetime import datetime, timezone

import pytest

from buzz_fleet.models import Agent, SystemPromptSource
from buzz_fleet.tui.app import BuzzFleetApp


def _agent(agent_id: str) -> Agent:
    return Agent(
        id=agent_id,
        community_id="eltahir",
        display_name=agent_id.title(),
        harness="claude",
        private_key="nsec1x",
        public_key="a" * 64,
        system_prompt_source=SystemPromptSource(kind="inline", text="hi"),
        created_at=datetime.now(timezone.utc),
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
        table = app.screen.query_one("#agent-table")
        assert ("laravel-dev", "Laravel-Dev", "claude", "RUNNING") in [
            tuple(str(v) for v in table.get_row_at(i)) for i in range(table.row_count)
        ]
