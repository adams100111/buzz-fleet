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
            marker = Text("*", style="bold") if community_id == self._active else Text("")
            # A community whose state/secrets file exists but can't be
            # turned into a `Community`/`list[Agent]` (truncated, hand-
            # edited, half-restored from a backup, a wrong-mode or
            # root-owned secrets file, or even a directory sitting where a
            # file should be) makes `load_community`/`load_agents` raise
            # rather than return `None` — `state.py` deliberately never
            # swallows that, since silently treating corruption as "absent"
            # would be worse for every other caller (e.g. `migrate`, which
            # depends on these failures being loud). Here, at the display
            # layer, the right response is to degrade that one row rather
            # than take the whole picker down: `buzz-fleet community list`
            # already lists a corrupt community fine (it never calls
            # `load_community` at all), and a user whose file got mangled
            # still needs to be able to open this screen and switch away
            # from it. Two exception families, both caught: `ValueError`
            # (bad JSON, or pydantic's `ValidationError`, a `ValueError`
            # subclass) from a file that parses as bytes but not as data,
            # and `OSError` (e.g. `PermissionError` on an unreadable
            # secrets file, `IsADirectoryError`) from `_read_merged`'s own
            # `Path.read_text()` calls, which run before any parsing and
            # have no error handling of their own.
            try:
                community = state.load_community(community_id)
            except (ValueError, OSError):
                relay = "<unreadable>"
            else:
                # community_id came from list_community_ids(), so its file
                # exists and load_community can only return None when the
                # file is absent -- never here. Asserted, not branched on,
                # so the exception handler above stays the only real
                # failure path.
                assert community is not None
                relay = community.relay_url
            try:
                agents = str(len(state.load_agents(community_id)))
            except (ValueError, OSError):
                agents = "?"
            table.add_row(marker, community_id, relay, agents)
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
