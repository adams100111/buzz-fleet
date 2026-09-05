import pytest

from buzz_fleet.tui.app import BuzzFleetApp
from buzz_fleet.tui.screens.logs import LogsScreen


@pytest.mark.asyncio
async def test_escape_closes_logs_screen(monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.tui.screens.logs.tail_logs", lambda runner, agent_id: "log output")

    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await app.push_screen(LogsScreen("some-agent"))
        await pilot.pause()
        assert isinstance(app.screen, LogsScreen)

        await pilot.press("escape")
        await pilot.pause()

        assert not isinstance(app.screen, LogsScreen)
