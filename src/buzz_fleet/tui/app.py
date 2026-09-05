"""BuzzFleetApp — the Textual application shell."""

from __future__ import annotations

from textual.app import App

from buzz_fleet import state
from buzz_fleet.tui.screens.connect import ConnectScreen
from buzz_fleet.tui.screens.dashboard import CURRENT_COMMUNITY_ID, DashboardScreen
from buzz_fleet.tui.theme import BUZZ_FLEET_THEME


class BuzzFleetApp(App):
    def on_mount(self) -> None:
        self.register_theme(BUZZ_FLEET_THEME)
        self.theme = "buzz-fleet"
        if state.load_community(CURRENT_COMMUNITY_ID) is None:
            self.push_screen(ConnectScreen())
        else:
            self.push_screen(DashboardScreen())
