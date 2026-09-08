"""Connect screen — collects relay URL + admin nsec, reuses buzz_fleet.connect logic."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Static

from buzz_fleet import state
from buzz_fleet.connect import connect_and_save
from buzz_fleet.proc import RealCommandRunner
from buzz_fleet.tui.theme import SECTION_CSS
from buzz_fleet.tui.theme import section as _section


class ConnectScreen(Screen):
    DEFAULT_CSS = f"""
    ConnectScreen {{
        {SECTION_CSS}

        #connect-center {{
            align: center middle;
            height: 1fr;
        }}

        .form-section {{
            width: 60;
            height: auto;
            margin: 0;
        }}
    }}
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="connect-center"), _section("Connect a community"):
            yield Static("The relay and owner/admin key you'd use to log into Buzz Desktop.")
            yield Input(placeholder="Local id for this community, e.g. eltahir", id="id-input")
            yield Input(placeholder="Relay URL, e.g. wss://buzz.eltahir.me", id="relay-input")
            yield Input(placeholder="Owner/admin nsec", password=True, id="nsec-input")
            yield Button("Connect", id="connect-button", variant="primary")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "connect-button":
            return
        community_id = self.query_one("#id-input", Input).value.strip()
        if not community_id:
            self.notify("A community id is required.", severity="error")
            return
        relay_url = self.query_one("#relay-input", Input).value
        admin_nsec = self.query_one("#nsec-input", Input).value
        runner = RealCommandRunner()
        try:
            connected = connect_and_save(runner, community_id, relay_url, admin_nsec)
        except ValueError as e:
            self.notify(str(e), severity="error")
            return
        if connected:
            from buzz_fleet.tui.screens.dashboard import DashboardScreen

            state.save_active_community(community_id)
            self.app.switch_screen(DashboardScreen(community_id))
        else:
            self.notify("Could not authenticate against that relay with that key.", severity="error")
