from pathlib import Path

import pytest
from textual.widgets import Button, Input, Select, Static

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

    async with app.run_test(size=(80, 50)) as pilot:
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

    async with app.run_test(size=(80, 50)) as pilot:
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

    async with app.run_test(size=(80, 50)) as pilot:
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


class RaisingManager:
    def create_agent(self, **kwargs):
        raise ValueError("display_name must contain at least one alphanumeric character")


@pytest.mark.asyncio
async def test_submitting_blank_display_name_notifies_instead_of_crashing() -> None:
    """Regression test: agent_slug raising ValueError on a blank/punctuation-only
    display name must not crash the app — it should show a notification and
    leave the form open for the user to correct.
    """
    manager = RaisingManager()
    app = BuzzFleetApp()

    async with app.run_test(size=(80, 50)) as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()
        app.screen.query_one("#display-name-input", Input).value = "!!!"
        await pilot.click("#submit-button")
        await pilot.pause()

        # The form screen is still on top — pop_screen was never reached.
        assert isinstance(app.screen, AgentFormScreen)


@pytest.mark.asyncio
async def test_escape_cancels_form_without_calling_manager() -> None:
    manager = FakeManager()
    app = BuzzFleetApp()

    async with app.run_test(size=(80, 50)) as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()
        assert isinstance(app.screen, AgentFormScreen)

        await pilot.press("escape")
        await pilot.pause()

        assert not isinstance(app.screen, AgentFormScreen)
        assert manager.created == []


@pytest.mark.asyncio
async def test_template_select_present_only_in_create_mode(tmp_path, monkeypatch) -> None:
    from buzz_fleet import personas

    personas_dir = tmp_path / "personas"
    personas_dir.mkdir()
    (personas_dir / "laravel.persona.md").write_text(
        "---\ndisplay_name: Laravel Backend Dev\nruntime: claude\n---\nPrompt body.\n"
    )
    monkeypatch.setattr(personas, "DEFAULT_PERSONAS_DIR", personas_dir)

    manager = FakeManager()
    app = BuzzFleetApp()

    async with app.run_test(size=(80, 50)) as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()
        assert app.screen.query("#template-select")

        await app.pop_screen()
        from datetime import UTC, datetime

        from buzz_fleet.models import Agent
        from buzz_fleet.models import SystemPromptSource as SPS

        existing = Agent(
            id="x",
            community_id="eltahir",
            display_name="X",
            harness="claude",
            private_key="nsec1x",
            public_key="a" * 64,
            system_prompt_source=SPS(kind="inline", text="hi"),
            created_at=datetime.now(UTC),
        )
        await app.push_screen(AgentFormScreen(manager, agent=existing))
        await pilot.pause()
        assert not app.screen.query("#template-select")


@pytest.mark.asyncio
async def test_no_templates_found_shows_explanatory_message_instead_of_blank_select(
    tmp_path, monkeypatch
) -> None:
    from buzz_fleet import personas

    monkeypatch.setattr(personas, "DEFAULT_PERSONAS_DIR", tmp_path / "personas")

    manager = FakeManager()
    app = BuzzFleetApp()

    async with app.run_test(size=(80, 50)) as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()

        assert not app.screen.query("#template-select")
        message = app.screen.query_one("#no-templates-message", Static)
        assert "No templates found" in message.render().plain


@pytest.mark.asyncio
async def test_selecting_template_prefills_and_overwrites_form_fields(tmp_path, monkeypatch) -> None:
    from buzz_fleet import personas

    personas_dir = tmp_path / "personas"
    personas_dir.mkdir()
    (personas_dir / "laravel.persona.md").write_text(
        "---\ndisplay_name: Laravel Backend Dev\nruntime: claude\nmodel: claude-sonnet-5\n---\n"
        "You are the Laravel dev.\n"
    )
    monkeypatch.setattr(personas, "DEFAULT_PERSONAS_DIR", personas_dir)

    manager = FakeManager()
    app = BuzzFleetApp()

    async with app.run_test(size=(80, 50)) as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()
        app.screen.query_one("#display-name-input", Input).value = "Something Typed First"

        # Only one template on disk, so it's index 0 — discover_personas globs
        # .persona.md files (sorted) before .agent.json files, and the picker
        # builds options as enumerate(self._templates). Avoid relying on any
        # private Select attribute to read options back.
        select = app.screen.query_one("#template-select", Select)
        select.value = 0
        await pilot.pause()

        assert app.screen.query_one("#display-name-input", Input).value == "Laravel Backend Dev"
        assert app.screen.query_one("#prompt-input", Input).value == "You are the Laravel dev.\n"
        assert app.screen.query_one("#model-input", Input).value == "claude-sonnet-5"


@pytest.mark.asyncio
async def test_harness_select_defaults_to_first_available_harness(monkeypatch) -> None:
    from buzz_fleet import harnesses

    def fake_which(cmd: str) -> str | None:
        return "/usr/bin/codex-acp" if cmd == "codex-acp" else None

    monkeypatch.setattr(harnesses.shutil, "which", fake_which)

    manager = FakeManager()
    app = BuzzFleetApp()

    async with app.run_test(size=(80, 50)) as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()

        assert app.screen.query_one("#harness-select", Select).value == "codex"


@pytest.mark.asyncio
async def test_harness_select_keeps_existing_agent_harness_regardless_of_availability(
    monkeypatch,
) -> None:
    from datetime import UTC, datetime

    from buzz_fleet import harnesses
    from buzz_fleet.models import Agent, SystemPromptSource

    monkeypatch.setattr(harnesses.shutil, "which", lambda cmd: None)

    manager = FakeManager()
    existing = Agent(
        id="x",
        community_id="eltahir",
        display_name="X",
        harness="goose",
        private_key="nsec1x",
        public_key="a" * 64,
        system_prompt_source=SystemPromptSource(kind="inline", text="hi"),
        created_at=datetime.now(UTC),
    )
    app = BuzzFleetApp()

    async with app.run_test(size=(80, 50)) as pilot:
        await app.push_screen(AgentFormScreen(manager, agent=existing))
        await pilot.pause()

        assert app.screen.query_one("#harness-select", Select).value == "goose"


@pytest.mark.asyncio
async def test_install_adapter_button_hidden_when_default_harness_is_available(
    monkeypatch,
) -> None:
    from buzz_fleet import harnesses

    monkeypatch.setattr(harnesses.shutil, "which", lambda cmd: "/usr/bin/claude-agent-acp")

    manager = FakeManager()
    app = BuzzFleetApp()

    async with app.run_test(size=(80, 50)) as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()

        assert app.screen.query_one("#install-adapter-button", Button).display is False


@pytest.mark.asyncio
async def test_install_adapter_button_shown_when_default_harness_unavailable(monkeypatch) -> None:
    from buzz_fleet import harnesses

    monkeypatch.setattr(harnesses.shutil, "which", lambda cmd: None)

    manager = FakeManager()
    app = BuzzFleetApp()

    async with app.run_test(size=(80, 50)) as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()

        button = app.screen.query_one("#install-adapter-button", Button)
        assert button.display is True
        assert "claude" in str(button.label)


@pytest.mark.asyncio
async def test_selecting_a_missing_harness_shows_install_button(monkeypatch) -> None:
    from buzz_fleet import harnesses

    def fake_which(cmd: str) -> str | None:
        return "/usr/bin/claude-agent-acp" if "claude" in cmd else None

    monkeypatch.setattr(harnesses.shutil, "which", fake_which)

    manager = FakeManager()
    app = BuzzFleetApp()

    async with app.run_test(size=(80, 50)) as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()

        button = app.screen.query_one("#install-adapter-button", Button)
        assert button.display is False

        app.screen.query_one("#harness-select", Select).value = "codex"
        await pilot.pause()

        assert button.display is True
        assert "codex" in str(button.label)


@pytest.mark.asyncio
async def test_clicking_install_adapter_button_runs_install_and_hides_itself(monkeypatch) -> None:
    from buzz_fleet import harnesses
    from buzz_fleet.proc import RealCommandRunner

    monkeypatch.setattr(harnesses.shutil, "which", lambda cmd: None)

    calls: list[tuple[object, str]] = []

    def fake_install_adapter(runner: object, harness: str) -> None:
        calls.append((runner, harness))

    monkeypatch.setattr(harnesses, "install_adapter", fake_install_adapter)

    manager = FakeManager()
    app = BuzzFleetApp()

    async with app.run_test(size=(80, 50)) as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()

        button = app.screen.query_one("#install-adapter-button", Button)
        assert button.display is True

        await pilot.click("#install-adapter-button")
        await pilot.pause()

        assert len(calls) == 1
        assert isinstance(calls[0][0], RealCommandRunner)
        assert calls[0][1] == "claude"
        assert button.display is False


@pytest.mark.asyncio
async def test_clicking_install_adapter_button_notifies_error_on_failure(monkeypatch) -> None:
    from buzz_fleet import harnesses

    monkeypatch.setattr(harnesses.shutil, "which", lambda cmd: None)

    def fake_install_adapter(runner: object, harness: str) -> None:
        raise RuntimeError("network error")

    monkeypatch.setattr(harnesses, "install_adapter", fake_install_adapter)

    manager = FakeManager()
    app = BuzzFleetApp()

    async with app.run_test(size=(80, 50)) as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()

        button = app.screen.query_one("#install-adapter-button", Button)
        await pilot.click("#install-adapter-button")
        await pilot.pause()

        # Install failed — button stays visible, form is still open.
        assert button.display is True
        assert isinstance(app.screen, AgentFormScreen)


@pytest.mark.asyncio
async def test_submitting_form_passes_new_fields_to_create_agent() -> None:
    manager = FakeManager()
    app = BuzzFleetApp()

    async with app.run_test(size=(80, 50)) as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()
        app.screen.query_one("#display-name-input", Input).value = "Test Agent"
        app.screen.query_one("#prompt-input", Input).value = "You are a test agent."
        app.screen.query_one("#model-input", Input).value = "claude-sonnet-5"
        app.screen.query_one("#parallelism-input", Input).value = "3"
        app.screen.query_one("#idle-timeout-input", Input).value = "120"
        app.screen.query_one("#max-turn-duration-input", Input).value = "600"
        app.screen.query_one("#respond-to-allowlist-input", Input).value = f"{'a' * 64}, {'b' * 64}"
        await pilot.click("#submit-button")
        await pilot.pause()

    assert len(manager.created) == 1
    kwargs = manager.created[0]
    assert kwargs["model"] == "claude-sonnet-5"
    assert kwargs["parallelism"] == 3
    assert kwargs["idle_timeout_seconds"] == 120
    assert kwargs["max_turn_duration_seconds"] == 600
    assert kwargs["respond_to_allowlist"] == ["a" * 64, "b" * 64]


@pytest.mark.asyncio
async def test_submitting_form_with_blank_optional_fields_passes_none() -> None:
    manager = FakeManager()
    app = BuzzFleetApp()

    async with app.run_test(size=(80, 50)) as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()
        app.screen.query_one("#display-name-input", Input).value = "Test Agent"
        app.screen.query_one("#prompt-input", Input).value = "You are a test agent."
        await pilot.click("#submit-button")
        await pilot.pause()

    kwargs = manager.created[0]
    assert kwargs["model"] is None
    assert kwargs["parallelism"] is None
    assert kwargs["idle_timeout_seconds"] is None
    assert kwargs["max_turn_duration_seconds"] is None
    assert kwargs["respond_to_allowlist"] is None


@pytest.mark.asyncio
async def test_submitting_form_with_non_numeric_parallelism_notifies_instead_of_crashing() -> None:
    manager = FakeManager()
    app = BuzzFleetApp()

    async with app.run_test(size=(80, 50)) as pilot:
        await app.push_screen(AgentFormScreen(manager))
        await pilot.pause()
        app.screen.query_one("#display-name-input", Input).value = "Test Agent"
        app.screen.query_one("#prompt-input", Input).value = "hi"
        app.screen.query_one("#parallelism-input", Input).value = "not-a-number"
        await pilot.click("#submit-button")
        await pilot.pause()

        # Accessed inside the pilot context: app.screen raises ScreenStackError
        # once run_test() has torn down the app, so these must run before exit.
        assert manager.created == []
        assert isinstance(app.screen, AgentFormScreen)
