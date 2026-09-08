"""Choose the active community.

This writes the same pointer as `buzz-fleet community use`, through the same
`state.save_active_community` function, so the CLI and the TUI can never
disagree about which community is active.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Label

from buzz_fleet import state
from buzz_fleet.tui.theme import PANEL_BORDER


class CommunityPickerScreen(ModalScreen[str | None]):
    """List the connected communities and dismiss with the chosen id."""

    DEFAULT_CSS = f"""
    CommunityPickerScreen {{
        align: center middle;

        #picker-dialog {{
            width: auto;
            max-width: 80;
            height: auto;
            border: round {PANEL_BORDER};
            background: $surface;
            padding: 1 2;
        }}

        #picker-hint {{
            margin-top: 1;
        }}
    }}
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, active: str) -> None:
        super().__init__()
        self._active = active

    def compose(self) -> ComposeResult:
        dialog = Vertical(id="picker-dialog")
        dialog.border_title = "Switch community"
        with dialog:
            yield DataTable(id="community-table", cursor_type="row")
            yield Label("Enter to switch, Escape to cancel.", id="picker-hint")

    def on_mount(self) -> None:
        table = self.query_one("#community-table", DataTable)
        table.add_columns("", "community", "relay", "agents")
        for community_id in state.list_community_ids():
            community = state.load_community(community_id)
            if community is None:
                continue
            marker = Text("*", style="bold") if community_id == self._active else Text("")
            table.add_row(
                marker,
                community_id,
                community.relay_url,
                str(len(state.load_agents(community_id))),
            )
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        table = self.query_one("#community-table", DataTable)
        if table.row_count == 0:
            return
        chosen = str(table.get_row_at(event.cursor_row)[1])
        state.save_active_community(chosen)
        self.dismiss(chosen)

    def action_cancel(self) -> None:
        self.dismiss(None)
