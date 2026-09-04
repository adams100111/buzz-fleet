from pathlib import Path

import pytest
from textual.widgets import Input

from buzz_fleet.tui.app import BuzzFleetApp
from buzz_fleet.tui.screens.agent_form import AgentFormScreen


class FakeManager:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.updated: list[tuple[str, dict]] = []

    def create_agent(self, **kwargs):
        self.created.append(kwargs)
        return object()

    def update_agent(self, agent_id, **kwargs):
        self.updated.append((agent_id, kwargs))
        return object()


@pytest.mark.asyncio
async def test_submitting_form_calls_create_agent() -> None:
    manager = FakeManager()
    app = BuzzFleetApp()

    async with app.run_test() as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()
        # Set values directly rather than simulating keystrokes: Textual's
        # pilot.press() takes key *names* ("space", not a literal " "), so
        # press(*"Test Agent") would break on the space in "Test Agent" —
        # this is the standard way to fill an Input in a Textual test.
        app.screen.query_one("#display-name-input", Input).value = "Test Agent"
        app.screen.query_one("#prompt-input", Input).value = "You are a test agent."
        await pilot.click("#submit-button")
        await pilot.pause()

    assert len(manager.created) == 1
    assert manager.created[0]["display_name"] == "Test Agent"


@pytest.mark.asyncio
async def test_submitting_form_in_edit_mode_calls_update_agent() -> None:
    from datetime import UTC, datetime

    from buzz_fleet.models import Agent, SystemPromptSource

    manager = FakeManager()
    existing = Agent(
        id="laravel-dev",
        community_id="eltahir",
        display_name="Laravel Dev",
        harness="claude",
        private_key="nsec1x",
        public_key="a" * 64,
        system_prompt_source=SystemPromptSource(kind="inline", text="old prompt"),
        created_at=datetime.now(UTC),
    )
    app = BuzzFleetApp()

    async with app.run_test() as pilot:
        await app.push_screen(AgentFormScreen(manager, agent=existing))
        await pilot.pause()
        assert app.screen.query_one("#display-name-input", Input).value == "Laravel Dev"
        assert app.screen.query_one("#prompt-input", Input).value == "old prompt"
        app.screen.query_one("#prompt-input", Input).value = "new prompt"
        await pilot.click("#submit-button")
        await pilot.pause()

    assert len(manager.updated) == 1
    assert manager.created == []
    agent_id, changes = manager.updated[0]
    assert agent_id == "laravel-dev"
    assert changes["system_prompt_source"].text == "new prompt"


@pytest.mark.asyncio
async def test_editing_only_display_name_does_not_touch_persona_file_prompt() -> None:
    """Regression test for Fix 1.

    Every CLI-created agent uses kind="persona_file". Opening it in the TUI
    edit form and changing only the display name must NOT destroy its persona
    file by silently sending an empty inline prompt in the update.
    """
    from datetime import UTC, datetime

    from buzz_fleet.models import Agent, SystemPromptSource

    manager = FakeManager()
    existing = Agent(
        id="laravel-dev",
        community_id="eltahir",
        display_name="Laravel Dev",
        harness="claude",
        private_key="nsec1x",
        public_key="a" * 64,
        system_prompt_source=SystemPromptSource(kind="persona_file", path=Path("/some/persona.md")),
        created_at=datetime.now(UTC),
    )
    app = BuzzFleetApp()

    async with app.run_test() as pilot:
        await app.push_screen(AgentFormScreen(manager, agent=existing))
        await pilot.pause()
        # persona_file agents never get their prompt Input pre-filled.
        assert app.screen.query_one("#prompt-input", Input).value == ""
        app.screen.query_one("#display-name-input", Input).value = "Laravel Dev (renamed)"
        await pilot.click("#submit-button")
        await pilot.pause()

    assert len(manager.updated) == 1
    agent_id, changes = manager.updated[0]
    assert agent_id == "laravel-dev"
    assert changes["display_name"] == "Laravel Dev (renamed)"
    assert "system_prompt_source" not in changes
