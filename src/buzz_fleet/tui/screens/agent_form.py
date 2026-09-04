"""Create/update agent form screen."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input

from buzz_fleet.manager import AgentManager
from buzz_fleet.models import Agent, SystemPromptSource


class AgentFormScreen(Screen):
    def __init__(self, manager: AgentManager, agent: Agent | None = None) -> None:
        super().__init__()
        self._manager = manager
        self._agent = agent

    def compose(self) -> ComposeResult:
        yield Header()
        display_name = self._agent.display_name if self._agent else ""
        prompt_text = ""
        if self._agent and self._agent.system_prompt_source.kind == "inline":
            prompt_text = self._agent.system_prompt_source.text or ""
        yield Input(value=display_name, placeholder="Display name", id="display-name-input")
        yield Input(value=prompt_text, placeholder="System prompt", id="prompt-input")
        yield Button("Update" if self._agent else "Create", id="submit-button")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "submit-button":
            return
        display_name = self.query_one("#display-name-input", Input).value
        prompt_text = self.query_one("#prompt-input", Input).value
        prompt_source = SystemPromptSource(kind="inline", text=prompt_text)
        if self._agent is not None:
            self._manager.update_agent(
                self._agent.id,
                display_name=display_name,
                system_prompt_source=prompt_source,
            )
        else:
            self._manager.create_agent(
                display_name=display_name,
                harness="claude",
                system_prompt_source=prompt_source,
            )
        self.app.pop_screen()
