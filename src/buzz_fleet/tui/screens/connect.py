"""Connect screen — collects relay URL + admin nsec, reuses buzz_fleet.connect logic."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input

from buzz_fleet.connect import connect_and_save
from buzz_fleet.proc import RealCommandRunner
from buzz_fleet.tui.screens.dashboard import CURRENT_COMMUNITY_ID


class ConnectScreen(Screen):
    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="Relay URL, e.g. wss://buzz.eltahir.me", id="relay-input")
        yield Input(placeholder="Owner/admin nsec", password=True, id="nsec-input")
        yield Button("Connect", id="connect-button")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "connect-button":
            return
        relay_url = self.query_one("#relay-input", Input).value
        admin_nsec = self.query_one("#nsec-input", Input).value
        runner = RealCommandRunner()
        if connect_and_save(runner, CURRENT_COMMUNITY_ID, relay_url, admin_nsec):
            from buzz_fleet.tui.screens.dashboard import DashboardScreen

            self.app.switch_screen(DashboardScreen())
        else:
            self.notify("Could not authenticate against that relay with that key.", severity="error")
