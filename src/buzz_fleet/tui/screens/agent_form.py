"""Create/update agent form screen."""

from __future__ import annotations

import uuid
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Select, Static, TextArea

from buzz_fleet import harnesses, personas
from buzz_fleet.manager import AgentManager
from buzz_fleet.models import Agent, SystemPromptSource
from buzz_fleet.proc import RealCommandRunner
from buzz_fleet.tui.theme import SECTION_CSS
from buzz_fleet.tui.theme import section as _section


class AgentFormScreen(Screen):
    DEFAULT_CSS = f"""
    AgentFormScreen {{
        {SECTION_CSS}

        .form-section {{
            height: auto;
        }}

        #no-templates-message {{
            margin-bottom: 1;
        }}
    }}
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, manager: AgentManager, agent: Agent | None = None) -> None:
        super().__init__()
        self._manager = manager
        self._agent = agent
        self._original_prompt_text: str | None = None
        self._templates: list[personas.PersonaTemplate] = []
        self._harness_availability: dict[str, harnesses.HarnessAvailability] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        display_name = self._agent.display_name if self._agent else ""
        harness = self._agent.harness if self._agent else harnesses.default_harness()
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

        self._harness_availability = harnesses.detect_harness_availability()
        install_button = Button(
            f"Install {harness} adapter", id="install-adapter-button", variant="warning"
        )
        install_button.display = self._harness_availability[harness] != "available"

        # Template selection comes first (create mode only) because picking a
        # template overwrites display name, harness, prompt, team
        # instructions, model, and limits below it — choosing it after typing
        # those in would silently clobber what the user just entered.
        if self._agent is None:
            with _section("Template"):
                self._templates, skipped = personas.discover_personas(personas.DEFAULT_PERSONAS_DIR)
                if self._templates or skipped:
                    options = [
                        (f"{t.display_name} ({t.source_path.name})", i)
                        for i, t in enumerate(self._templates)
                    ]
                    prompt = (
                        "Start from a template…"
                        if not skipped
                        else f"Start from a template… ({skipped} unsupported file(s) found)"
                    )
                    yield Static("Template:")
                    yield Select(options, prompt=prompt, id="template-select")
                else:
                    yield Static(
                        f"No templates found in {personas.DEFAULT_PERSONAS_DIR}",
                        id="no-templates-message",
                    )

        with _section("Identity"):
            yield Input(value=display_name, placeholder="Display name", id="display-name-input")
            yield Static("Harness:")
            yield Select(
                harnesses.harness_select_options(),
                value=harness,
                allow_blank=False,
                id="harness-select",
            )
            yield install_button

        with _section("Behavior"):
            yield TextArea(text=prompt_text, placeholder="System prompt", id="prompt-input")
            yield TextArea(
                text=self._agent.team_instructions if self._agent and self._agent.team_instructions else "",
                placeholder="Team instructions (optional)",
                id="team-instructions-input",
            )
            yield Input(
                value=self._agent.model if self._agent and self._agent.model else "",
                placeholder="Model (optional)",
                id="model-input",
            )

        with _section("Limits"):
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

        with _section("Access"):
            yield Input(
                value=(
                    ", ".join(self._agent.respond_to_allowlist)
                    if self._agent and self._agent.respond_to_allowlist
                    else ""
                ),
                placeholder="Respond-to allowlist pubkeys, comma-separated (optional)",
                id="respond-to-allowlist-input",
            )
            yield Input(
                value=(", ".join(self._agent.channel_ids) if self._agent and self._agent.channel_ids else ""),
                placeholder="Channel IDs, comma-separated (optional)",
                id="channel-ids-input",
            )
            yield Static("Channel add policy:")
            yield Select(
                [("anyone", "anyone"), ("owner_only", "owner_only"), ("nobody", "nobody")],
                value=(
                    self._agent.channel_add_policy
                    if self._agent and self._agent.channel_add_policy
                    else "owner_only"
                ),
                allow_blank=False,
                id="channel-add-policy-select",
            )

        yield Button("Update" if self._agent else "Create", id="submit-button", variant="primary")
        yield Footer()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "harness-select":
            available = self._harness_availability.get(event.value) == "available"
            button = self.query_one("#install-adapter-button", Button)
            button.label = f"Install {event.value} adapter"
            button.display = not available
            return
        if event.select.id != "template-select":
            return
        if event.value is Select.BLANK:
            return
        template = self._templates[event.value]
        self.query_one("#display-name-input", Input).value = template.display_name
        if template.harness in harnesses.HARNESSES:
            self.query_one("#harness-select", Select).value = template.harness
        self.query_one("#prompt-input", TextArea).text = template.prompt_body
        self.query_one("#team-instructions-input", TextArea).text = template.team_instructions or ""
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
        if event.button.id == "install-adapter-button":
            self._install_selected_harness_adapter()
            return
        if event.button.id != "submit-button":
            return
        display_name = self.query_one("#display-name-input", Input).value
        prompt_text = self.query_one("#prompt-input", TextArea).text
        harness = self.query_one("#harness-select", Select).value
        team_instructions = self.query_one("#team-instructions-input", TextArea).text.strip() or None
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
            channel_ids = self._parse_optional_uuid_list("#channel-ids-input")
        except ValueError:
            self.notify(
                "Parallelism, idle timeout, max turn duration must be whole numbers, "
                "and channel IDs must be valid UUIDs.",
                severity="error",
            )
            return

        channel_add_policy = self.query_one("#channel-add-policy-select", Select).value

        try:
            if self._agent is not None:
                changes: dict[str, object] = {
                    "display_name": display_name,
                    "harness": harness,
                    "team_instructions": team_instructions,
                    "model": model,
                    "parallelism": parallelism,
                    "idle_timeout_seconds": idle_timeout_seconds,
                    "max_turn_duration_seconds": max_turn_duration_seconds,
                    "respond_to_allowlist": respond_to_allowlist,
                    "channel_ids": channel_ids,
                    "channel_add_policy": channel_add_policy,
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
                    team_instructions=team_instructions,
                    model=model,
                    parallelism=parallelism,
                    idle_timeout_seconds=idle_timeout_seconds,
                    max_turn_duration_seconds=max_turn_duration_seconds,
                    respond_to_allowlist=respond_to_allowlist,
                    channel_ids=channel_ids,
                    channel_add_policy=channel_add_policy,
                )
        except ValueError as e:
            # e.g. a blank/punctuation-only display name (agent_slug raises)
            # must not crash the app — surface it and let the user retry.
            self.notify(str(e), severity="error")
            return
        self.app.pop_screen()

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def _parse_optional_int(self, input_id: str) -> int | None:
        raw = self.query_one(input_id, Input).value.strip()
        return int(raw) if raw else None

    def _parse_optional_uuid_list(self, input_id: str) -> list[str] | None:
        raw = self.query_one(input_id, Input).value.strip()
        if not raw:
            return None
        ids = [entry.strip() for entry in raw.split(",") if entry.strip()]
        for entry in ids:
            uuid.UUID(entry)  # raises ValueError on malformed input
        return ids or None

    def _install_selected_harness_adapter(self) -> None:
        harness = self.query_one("#harness-select", Select).value
        self.notify(f"Installing {harness}'s adapter — this may take a moment…")
        try:
            harnesses.install_adapter(RealCommandRunner(), harness)
        except RuntimeError as e:
            self.notify(str(e), severity="error")
            return
        self._harness_availability[harness] = "available"
        self.query_one("#install-adapter-button", Button).display = False
        self.notify(f"Installed {harness}'s adapter.")
