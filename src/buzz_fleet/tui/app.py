"""BuzzFleetApp — the Textual application shell."""

from __future__ import annotations

from textual.app import App

from buzz_fleet import state
from buzz_fleet.tui.screens.connect import ConnectScreen
from buzz_fleet.tui.screens.dashboard import CURRENT_COMMUNITY_ID, DashboardScreen


class BuzzFleetApp(App):
    def on_mount(self) -> None:
        if state.load_community(CURRENT_COMMUNITY_ID) is None:
            self.push_screen(ConnectScreen())
        else:
            self.push_screen(DashboardScreen())
