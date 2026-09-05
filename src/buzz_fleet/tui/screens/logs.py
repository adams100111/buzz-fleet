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


class LogsScreen(Screen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close"),
    ]

    def __init__(self, agent_id: str) -> None:
        super().__init__()
        self._agent_id = agent_id

    def action_close(self) -> None:
        self.app.pop_screen()

    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(id="log-view")
        yield Footer()

    def on_mount(self) -> None:
        self.stream_logs()

    @work(exclusive=True)
    async def stream_logs(self) -> None:
        log_widget = self.query_one("#log-view", RichLog)
        log_widget.write(tail_logs(RealCommandRunner(), self._agent_id))
