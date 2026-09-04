"""Live agent dashboard: a table of agents polled from systemctl status."""

from __future__ import annotations

from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header

from buzz_fleet import state
from buzz_fleet.manager import AgentManager
from buzz_fleet.proc import RealCommandRunner
from buzz_fleet.systemctl_client import status as systemctl_status
from buzz_fleet.tui.screens.agent_form import AgentFormScreen
from buzz_fleet.tui.screens.logs import LogsScreen

CURRENT_COMMUNITY_ID = "eltahir"


def list_agents() -> list:
    community = state.load_community(CURRENT_COMMUNITY_ID)
    return state.load_agents(community.id) if community else []


def agent_status(agent_id: str) -> str:
    return systemctl_status(RealCommandRunner(), agent_id).name


class DashboardScreen(Screen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("c", "create_agent", "Create agent"),
        Binding("u", "edit_agent", "Edit agent"),
        Binding("x", "delete_agent", "Delete agent"),
        Binding("l", "view_logs", "View logs"),
    ]

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

    def _selected_agent_id(self) -> str | None:
        table = self.query_one("#agent-table", DataTable)
        if table.cursor_row is None:
            return None
        return str(table.get_row_at(table.cursor_row)[0])

    def action_create_agent(self) -> None:
        community = state.load_community(CURRENT_COMMUNITY_ID)
        manager = AgentManager(RealCommandRunner(), community)
        self.app.push_screen(AgentFormScreen(manager))

    def action_edit_agent(self) -> None:
        agent_id = self._selected_agent_id()
        if agent_id is None:
            return
        community = state.load_community(CURRENT_COMMUNITY_ID)
        manager = AgentManager(RealCommandRunner(), community)
        agent = next((a for a in manager.list_agents() if a.id == agent_id), None)
        if agent is None:
            return
        self.app.push_screen(AgentFormScreen(manager, agent=agent))

    def action_delete_agent(self) -> None:
        agent_id = self._selected_agent_id()
        if agent_id is None:
            return
        community = state.load_community(CURRENT_COMMUNITY_ID)
        manager = AgentManager(RealCommandRunner(), community)
        manager.delete_agent(agent_id)
        self.refresh_agents()

    def action_view_logs(self) -> None:
        agent_id = self._selected_agent_id()
        if agent_id is None:
            return
        self.app.push_screen(LogsScreen(agent_id))
