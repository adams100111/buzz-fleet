"""BuzzFleetApp — the Textual application shell."""

from __future__ import annotations

import os

from textual.app import App

from buzz_fleet.orchestration.identity import resolve_community_id
from buzz_fleet.tui.screens.connect import ConnectScreen
from buzz_fleet.tui.screens.dashboard import DashboardScreen
from buzz_fleet.tui.theme import BUZZ_FLEET_THEME


class BuzzFleetApp(App):
    def on_mount(self) -> None:
        self.register_theme(BUZZ_FLEET_THEME)
        self.theme = "buzz-fleet"
        try:
            community_id = resolve_community_id(os.environ, None)
        except RuntimeError:
            # No community yet, or several with nothing selected. Connecting
            # is the answer to the first; the picker is the answer to the
            # second, and it is the same screen.
            self.push_screen(ConnectScreen())
            return
        self.push_screen(DashboardScreen(community_id))
