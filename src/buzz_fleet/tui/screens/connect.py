"""Connect screen — collects relay URL + admin nsec, reuses buzz_fleet.cli connect logic."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Header, Static


class ConnectScreen(Screen):
    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Press 'd' to view the dashboard (connect form lands in Task 11).")
        yield Footer()
