"""BuzzFleetApp — the Textual application shell."""

from __future__ import annotations

from textual.app import App

from buzz_fleet.tui.screens.dashboard import DashboardScreen


class BuzzFleetApp(App):
    def on_mount(self) -> None:
        self.push_screen(DashboardScreen())
