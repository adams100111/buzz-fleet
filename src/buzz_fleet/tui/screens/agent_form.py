"""Create/update agent form screen."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Select

from buzz_fleet import personas
from buzz_fleet.manager import AgentManager
from buzz_fleet.models import Agent, SystemPromptSource

_HARNESSES = ["claude", "codex", "pi", "goose"]


class AgentFormScreen(Screen):
    # Compact the form's fields (borderless, single-row) so that the full set
    # of inputs plus the submit button fits within the default terminal
    # height used by tests (and small real terminals) without scrolling.
    DEFAULT_CSS = """
    AgentFormScreen {
        Input {
            height: 1;
            border: none;
            padding: 0 1;
        }
        Select > SelectCurrent {
            height: 1;
            border: none;
        }
        Button {
            height: 1;
            border: none;
        }
    }
    """

    def __init__(self, manager: AgentManager, agent: Agent | None = None) -> None:
        super().__init__()
        self._manager = manager
        self._agent = agent
        self._original_prompt_text: str | None = None
        self._templates: list[personas.PersonaTemplate] = []

    def compose(self) -> ComposeResult:
        yield Header()
        display_name = self._agent.display_name if self._agent else ""
        harness = self._agent.harness if self._agent else "claude"
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

        if self._agent is None:
            self._templates, skipped = personas.discover_personas(personas.DEFAULT_PERSONAS_DIR)
            options = [
                (f"{t.display_name} ({t.source_path.name})", i) for i, t in enumerate(self._templates)
            ]
            prompt = (
                "Start from a template…"
                if not skipped
                else f"Start from a template… ({skipped} unsupported file(s) found)"
            )
            yield Select(options, prompt=prompt, id="template-select")

        yield Input(value=display_name, placeholder="Display name", id="display-name-input")
        yield Select(
            [(h, h) for h in _HARNESSES], value=harness, allow_blank=False, id="harness-select"
        )
        yield Input(value=prompt_text, placeholder="System prompt", id="prompt-input")
        yield Input(
            value=self._agent.model if self._agent and self._agent.model else "",
            placeholder="Model (optional)",
            id="model-input",
        )
        yield Input(
            value=str(self._agent.parallelism)
            if self._agent and self._agent.parallelism is not None
            else "",
            placeholder="Parallelism (optional)",
            id="parallelism-input",
        )
        yield Input(
            value=(
                str(self._agent.idle_timeout_seconds)
                if self._agent and self._agent.idle_timeout_seconds is not None
                else ""
            ),
            placeholder="Idle timeout seconds (optional)",
            id="idle-timeout-input",
        )
        yield Input(
            value=(
                str(self._agent.max_turn_duration_seconds)
                if self._agent and self._agent.max_turn_duration_seconds is not None
                else ""
            ),
            placeholder="Max turn duration seconds (optional)",
            id="max-turn-duration-input",
        )
        yield Input(
            value=(
                ", ".join(self._agent.respond_to_allowlist)
                if self._agent and self._agent.respond_to_allowlist
                else ""
            ),
            placeholder="Respond-to allowlist pubkeys, comma-separated (optional)",
            id="respond-to-allowlist-input",
        )
        yield Button("Update" if self._agent else "Create", id="submit-button")
        yield Footer()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "template-select":
            return
        if event.value is Select.BLANK:
            return
        template = self._templates[event.value]
        self.query_one("#display-name-input", Input).value = template.display_name
        if template.harness in _HARNESSES:
            self.query_one("#harness-select", Select).value = template.harness
        self.query_one("#prompt-input", Input).value = template.prompt_body
        self.query_one("#model-input", Input).value = template.model or ""
        self.query_one("#parallelism-input", Input).value = (
            str(template.parallelism) if template.parallelism is not None else ""
        )
        self.query_one("#idle-timeout-input", Input).value = (
            str(template.idle_timeout_seconds) if template.idle_timeout_seconds is not None else ""
        )
        self.query_one("#max-turn-duration-input", Input).value = (
            str(template.max_turn_duration_seconds)
            if template.max_turn_duration_seconds is not None
            else ""
        )
        # respond_to_allowlist is deliberately never pre-filled from a
        # template — see the design spec.

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "submit-button":
            return
        display_name = self.query_one("#display-name-input", Input).value
        prompt_text = self.query_one("#prompt-input", Input).value
        harness = self.query_one("#harness-select", Select).value
        model = self.query_one("#model-input", Input).value.strip() or None
        respond_to_raw = self.query_one("#respond-to-allowlist-input", Input).value.strip()
        respond_to_allowlist = (
            [key.strip() for key in respond_to_raw.split(",") if key.strip()]
            if respond_to_raw
            else None
        )

        try:
            parallelism = self._parse_optional_int("#parallelism-input")
            idle_timeout_seconds = self._parse_optional_int("#idle-timeout-input")
            max_turn_duration_seconds = self._parse_optional_int("#max-turn-duration-input")
        except ValueError:
            self.notify(
                "Parallelism, idle timeout, and max turn duration must be whole numbers.",
                severity="error",
            )
            return

        try:
            if self._agent is not None:
                changes: dict[str, object] = {
                    "display_name": display_name,
                    "harness": harness,
                    "model": model,
                    "parallelism": parallelism,
                    "idle_timeout_seconds": idle_timeout_seconds,
                    "max_turn_duration_seconds": max_turn_duration_seconds,
                    "respond_to_allowlist": respond_to_allowlist,
                }
                # Only touch system_prompt_source if the user actually edited the
                # prompt field. This is the fix for the v1 bug where editing only
                # the display name of a persona_file agent silently overwrote its
                # persona file with an empty inline prompt (the prompt Input is
                # never pre-filled for persona_file agents, so leaving it alone
                # must mean "leave the prompt source alone", not "set it to '').
                if prompt_text != self._original_prompt_text:
                    changes["system_prompt_source"] = SystemPromptSource(
                        kind="inline", text=prompt_text
                    )
                self._manager.update_agent(self._agent.id, **changes)
            else:
                prompt_source = SystemPromptSource(kind="inline", text=prompt_text)
                self._manager.create_agent(
                    display_name=display_name,
                    harness=harness,
                    system_prompt_source=prompt_source,
                    model=model,
                    parallelism=parallelism,
                    idle_timeout_seconds=idle_timeout_seconds,
                    max_turn_duration_seconds=max_turn_duration_seconds,
                    respond_to_allowlist=respond_to_allowlist,
                )
        except ValueError as e:
            # e.g. a blank/punctuation-only display name (agent_slug raises)
            # must not crash the app — surface it and let the user retry.
            self.notify(str(e), severity="error")
            return
        self.app.pop_screen()

    def _parse_optional_int(self, input_id: str) -> int | None:
        raw = self.query_one(input_id, Input).value.strip()
        return int(raw) if raw else None
