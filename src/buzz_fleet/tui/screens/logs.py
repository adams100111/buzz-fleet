"""Live log-tail screen for one agent's systemd unit."""

from __future__ import annotations

from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.widgets import Footer, Header, RichLog

from buzz_fleet.proc import RealCommandRunner
from buzz_fleet.systemctl_client import tail_logs
from buzz_fleet.tui.theme import PANEL_BORDER


class LogsScreen(Screen):
    DEFAULT_CSS = f"""
    LogsScreen {{
        #log-view {{
            border: round {PANEL_BORDER};
            margin: 1 2;
        }}
    }}
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close"),
    ]

    def __init__(self, key: str) -> None:
        """`key` is a community-qualified instance key (see units.instance_key)."""
        super().__init__()
        self._key = key

    def action_close(self) -> None:
        self.app.pop_screen()

    def compose(self) -> ComposeResult:
        yield Header()
        log_view = RichLog(id="log-view")
        log_view.border_title = self._key
        yield log_view
        yield Footer()

    def on_mount(self) -> None:
        self.stream_logs()

    @work(exclusive=True)
    async def stream_logs(self) -> None:
        log_widget = self.query_one("#log-view", RichLog)
        log_widget.write(tail_logs(RealCommandRunner(), self._key))
