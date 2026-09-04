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
        self._original_prompt_text: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        display_name = self._agent.display_name if self._agent else ""
        prompt_text = ""
        if self._agent and self._agent.system_prompt_source.kind == "inline":
            prompt_text = self._agent.system_prompt_source.text or ""
        # Only meaningful in edit mode: lets on_button_pressed detect whether the
        # user actually edited the prompt, versus merely re-submitting the form
        # with the display name changed. This matters because a persona_file
        # agent's prompt_text is always "" here (never pre-filled from a file
        # path) — without this guard, submitting with an untouched prompt field
        # would silently downgrade the agent to an empty inline prompt and
        # destroy its persona file. See on_button_pressed.
        self._original_prompt_text = prompt_text
        yield Input(value=display_name, placeholder="Display name", id="display-name-input")
        yield Input(value=prompt_text, placeholder="System prompt", id="prompt-input")
        yield Button("Update" if self._agent else "Create", id="submit-button")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "submit-button":
            return
        display_name = self.query_one("#display-name-input", Input).value
        prompt_text = self.query_one("#prompt-input", Input).value
        try:
            if self._agent is not None:
                changes: dict[str, object] = {"display_name": display_name}
                # Only touch system_prompt_source if the user actually edited the
                # prompt field. This is the fix for the v1 bug where editing only
                # the display name of a persona_file agent silently overwrote its
                # persona file with an empty inline prompt (the prompt Input is
                # never pre-filled for persona_file agents, so leaving it alone
                # must mean "leave the prompt source alone", not "set it to '').
                if prompt_text != self._original_prompt_text:
                    changes["system_prompt_source"] = SystemPromptSource(kind="inline", text=prompt_text)
                self._manager.update_agent(self._agent.id, **changes)
            else:
                prompt_source = SystemPromptSource(kind="inline", text=prompt_text)
                self._manager.create_agent(
                    display_name=display_name,
                    harness="claude",
                    system_prompt_source=prompt_source,
                )
        except ValueError as e:
            # e.g. a blank/punctuation-only display name (agent_slug raises)
            # must not crash the app — surface it and let the user retry.
            self.notify(str(e), severity="error")
            return
        self.app.pop_screen()
