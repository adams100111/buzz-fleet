"""Confirmation modal shown before an agent is actually deleted.

Deleting an agent is destructive and not undoable from the TUI (it tears
down the systemd unit and — for a visibility-managed agent — leaves every
channel, retracts its kind:30177 record, and files a kind:9035 archive
request on the relay). It must never fire from a single accidental
keypress.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label


class ConfirmDeleteScreen(ModalScreen[bool]):
    """Ask "delete this agent?" and dismiss with the answer."""

    DEFAULT_CSS = """
    ConfirmDeleteScreen {
        align: center middle;

        #confirm-dialog {
            width: auto;
            max-width: 60;
            border: round $error;
            background: $surface;
            padding: 1 2;

            #confirm-message {
                margin-bottom: 1;
            }

            #confirm-buttons {
                align-horizontal: right;
                height: auto;

                & > Button {
                    margin-left: 1;
                }
            }
        }
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("n", "cancel", "Cancel"),
        Binding("y", "confirm", "Delete"),
    ]

    def __init__(self, agent_id: str, display_name: str) -> None:
        super().__init__()
        self._agent_id = agent_id
        self._display_name = display_name

    def compose(self) -> ComposeResult:
        dialog = Vertical(id="confirm-dialog")
        dialog.border_title = "Delete agent"
        with dialog:
            yield Label(
                f'Delete "{self._display_name}" ({self._agent_id})? '
                "This stops it and cannot be undone.",
                id="confirm-message",
            )
            with Vertical(id="confirm-buttons"):
                yield Button("Delete", variant="error", id="confirm-delete")
                yield Button("Cancel", variant="default", id="confirm-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-delete")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)
