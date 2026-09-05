"""Live agent dashboard: a table of agents polled from systemctl status."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header

from buzz_fleet import state
from buzz_fleet.manager import AgentManager
from buzz_fleet.proc import RealCommandRunner
from buzz_fleet.systemctl_client import AgentStatus
from buzz_fleet.systemctl_client import status as systemctl_status
from buzz_fleet.tui.screens.agent_form import AgentFormScreen
from buzz_fleet.tui.screens.logs import LogsScreen
from buzz_fleet.tui.theme import PANEL_BORDER, STATUS_INACTIVE

CURRENT_COMMUNITY_ID = "eltahir"

# Displayed status text borrows systemd's own vocabulary (active/inactive/
# failed) rather than inventing new words for states the underlying system
# already names — paired with a theme color so state is legible at a glance.
# Status color is reserved for this alone; it never doubles as decoration
# elsewhere in the app. Literal hex here, not a Textual theme variable:
# these colors render inside Rich `Text` objects (DataTable cells), which
# Rich styles directly — Textual's `$variable` CSS syntax doesn't apply.
_STATUS_DISPLAY: dict[AgentStatus, tuple[str, str]] = {
    AgentStatus.RUNNING: ("active", "#7FB069"),
    AgentStatus.STOPPED: ("inactive", STATUS_INACTIVE),
    AgentStatus.FAILED: ("failed", "#C1553A"),
    AgentStatus.UNKNOWN: ("unknown", STATUS_INACTIVE),
}


def list_agents() -> list:
    community = state.load_community(CURRENT_COMMUNITY_ID)
    return state.load_agents(community.id) if community else []


def agent_status(agent_id: str) -> AgentStatus:
    return systemctl_status(RealCommandRunner(), agent_id)


class DashboardScreen(Screen):
    DEFAULT_CSS = f"""
    DashboardScreen {{
        #agent-table {{
            border: round {PANEL_BORDER};
            margin: 1 2;
        }}
    }}
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("c", "create_agent", "Create agent"),
        Binding("u", "edit_agent", "Edit agent"),
        Binding("x", "delete_agent", "Delete agent"),
        Binding("l", "view_logs", "View logs"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        table = DataTable(id="agent-table")
        table.border_title = "Agents"
        table.add_columns("id", "display name", "harness", "status")
        yield table
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_agents()

    def on_screen_resume(self) -> None:
        # Fires when this screen becomes visible again after AgentFormScreen/
        # LogsScreen is popped (e.g. after creating, editing, or cancelling
        # out of the form) — without this, a newly created/edited agent never
        # shows up until the dashboard is torn down and rebuilt from scratch,
        # even though it was really created (relay membership published,
        # local state saved) — the table just never re-reads it.
        self.refresh_agents()

    @work(exclusive=True)
    async def refresh_agents(self) -> None:
        table = self.query_one("#agent-table", DataTable)
        table.clear()
        for agent in list_agents():
            text, color = _STATUS_DISPLAY[agent_status(agent.id)]
            table.add_row(agent.id, agent.display_name, agent.harness, Text(text, style=f"bold {color}"))

    def _selected_agent_id(self) -> str | None:
        table = self.query_one("#agent-table", DataTable)
        # DataTable.cursor_row is an int that is 0 on an empty table (never
        # None), so guarding on `cursor_row is None` doesn't catch the empty
        # case — get_row_at(0) then raises RowDoesNotExist and crashes the
        # app. Guard on row_count instead.
        if table.row_count == 0:
            return None
        return str(table.get_row_at(table.cursor_row)[0])

    def _manager_or_notify(self) -> AgentManager | None:
        community = state.load_community(CURRENT_COMMUNITY_ID)
        if community is None:
            self.notify("No connected community — run `buzz-fleet connect` first.", severity="error")
            return None
        return AgentManager(RealCommandRunner(), community)

    def action_create_agent(self) -> None:
        manager = self._manager_or_notify()
        if manager is None:
            return
        self.app.push_screen(AgentFormScreen(manager))

    def action_edit_agent(self) -> None:
        agent_id = self._selected_agent_id()
        if agent_id is None:
            return
        manager = self._manager_or_notify()
        if manager is None:
            return
        agent = next((a for a in manager.list_agents() if a.id == agent_id), None)
        if agent is None:
            return
        self.app.push_screen(AgentFormScreen(manager, agent=agent))

    def action_delete_agent(self) -> None:
        agent_id = self._selected_agent_id()
        if agent_id is None:
            return
        manager = self._manager_or_notify()
        if manager is None:
            return
        manager.delete_agent(agent_id)
        self.refresh_agents()

    def action_view_logs(self) -> None:
        agent_id = self._selected_agent_id()
        if agent_id is None:
            return
        self.app.push_screen(LogsScreen(agent_id))
