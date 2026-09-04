"""Live agent dashboard: a table of agents polled from systemctl status."""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header

from buzz_fleet import state
from buzz_fleet.proc import RealCommandRunner
from buzz_fleet.systemctl_client import status as systemctl_status

CURRENT_COMMUNITY_ID = "eltahir"


def list_agents() -> list:
    community = state.load_community(CURRENT_COMMUNITY_ID)
    return state.load_agents(community.id) if community else []


def agent_status(agent_id: str) -> str:
    return systemctl_status(RealCommandRunner(), agent_id).name


class DashboardScreen(Screen):
    def compose(self) -> ComposeResult:
        yield Header()
        table = DataTable(id="agent-table")
        table.add_columns("id", "display_name", "harness", "status")
        yield table
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_agents()

    @work(exclusive=True)
    async def refresh_agents(self) -> None:
        table = self.query_one("#agent-table", DataTable)
        table.clear()
        for agent in list_agents():
            table.add_row(agent.id, agent.display_name, agent.harness, agent_status(agent.id))
