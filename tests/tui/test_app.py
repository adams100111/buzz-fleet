import pytest

from buzz_fleet.tui.app import BuzzFleetApp


@pytest.mark.asyncio
async def test_buzz_fleet_theme_is_registered_and_active() -> None:
    app = BuzzFleetApp()

    async with app.run_test():
        assert app.theme == "buzz-fleet"
        theme = app.get_theme("buzz-fleet")
        assert theme.primary == "#D9A73B"
        assert theme.background == "#16130D"
