"""Live log-tail screen for one agent's systemd unit."""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Header, RichLog

from buzz_fleet.proc import RealCommandRunner
from buzz_fleet.systemctl_client import tail_logs


class LogsScreen(Screen):
    def __init__(self, agent_id: str) -> None:
        super().__init__()
        self._agent_id = agent_id

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
