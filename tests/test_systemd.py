import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from buzz_fleet.models import Agent, Community, SystemPromptSource
from buzz_fleet.systemd import (
    TEMPLATE_UNIT,
    agent_env_path,
    agent_prompt_path,
    ensure_linger_enabled,
    ensure_template_unit_installed,
    write_agent_files,
)


def _agent() -> Agent:
    return Agent(
        id="laravel-backend-dev",
        community_id="eltahir",
        display_name="Laravel Backend Dev",
        harness="claude",
        private_key="nsec1agent",
        public_key="a" * 64,
        system_prompt_source=SystemPromptSource(kind="inline", text="You are the Laravel dev."),
        team_instructions="Team-wide rules here.",
        model=None,
        created_at=datetime.now(UTC),
    )


def _community() -> Community:
    return Community(id="eltahir", relay_url="wss://buzz.eltahir.me", relay_admin_nsec="nsec1admin")


def test_write_agent_files_creates_env_and_prompt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path)
    agent = _agent()

    write_agent_files(agent, _community(), anthropic_api_key="sk-ant-test", openai_api_key=None)

    env_content = agent_env_path(agent.id).read_text()
    assert "BUZZ_PRIVATE_KEY=nsec1agent" in env_content
    assert "BUZZ_RELAY_URL=wss://buzz.eltahir.me" in env_content
    assert "BUZZ_ACP_AGENT_COMMAND=claude-agent-acp" in env_content
    assert "ANTHROPIC_API_KEY=sk-ant-test" in env_content
    assert f"BUZZ_ACP_SYSTEM_PROMPT_FILE={agent_prompt_path(agent.id)}" in env_content
    assert "BUZZ_ACP_TEAM_INSTRUCTIONS=Team-wide rules here." in env_content
    assert agent_prompt_path(agent.id).read_text() == "You are the Laravel dev."


def test_env_file_is_mode_0600(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path)
    write_agent_files(_agent(), _community(), anthropic_api_key="sk-ant-test", openai_api_key=None)

    mode = stat.S_IMODE(agent_env_path("laravel-backend-dev").stat().st_mode)
    assert mode == 0o600


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def test_ensure_template_unit_installed_writes_file_and_reloads(tmp_path: Path, monkeypatch) -> None:
    unit_path = tmp_path / "systemd" / "buzz-agent@.service"
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", unit_path)
    runner = FakeRunner()

    ensure_template_unit_installed(runner)

    assert unit_path.read_text() == TEMPLATE_UNIT
    assert ["systemctl", "--user", "daemon-reload"] in runner.calls


def test_ensure_template_unit_installed_is_a_noop_when_already_current(tmp_path: Path, monkeypatch) -> None:
    unit_path = tmp_path / "systemd" / "buzz-agent@.service"
    unit_path.parent.mkdir(parents=True)
    unit_path.write_text(TEMPLATE_UNIT)
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", unit_path)
    runner = FakeRunner()

    ensure_template_unit_installed(runner)

    assert runner.calls == []


class LingerRunner:
    """FakeRunner variant that answers loginctl calls; other args get a
    generic OK, matching what ensure_linger_enabled's two calls need.
    """

    def __init__(self, already_lingering: bool, enable_succeeds: bool = True) -> None:
        self.already_lingering = already_lingering
        self.enable_succeeds = enable_succeeds
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:2] == ["loginctl", "show-user"]:
            return subprocess.CompletedProcess(
                args, 0, stdout="yes" if self.already_lingering else "no", stderr=""
            )
        if args[:2] == ["loginctl", "enable-linger"]:
            if self.enable_succeeds:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="Interactive authentication required."
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def test_ensure_linger_enabled_is_a_noop_when_already_enabled() -> None:
    runner = LingerRunner(already_lingering=True)

    ensure_linger_enabled(runner)

    assert not any(c[:2] == ["loginctl", "enable-linger"] for c in runner.calls)


def test_ensure_linger_enabled_enables_it_when_not_set() -> None:
    runner = LingerRunner(already_lingering=False)

    ensure_linger_enabled(runner)

    assert any(c[:2] == ["loginctl", "enable-linger"] for c in runner.calls)


def test_ensure_linger_enabled_raises_clear_error_when_enable_fails() -> None:
    runner = LingerRunner(already_lingering=False, enable_succeeds=False)

    try:
        ensure_linger_enabled(runner)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "sudo loginctl enable-linger" in str(e)


def test_resolve_prompt_text_strips_frontmatter_from_persona_file(tmp_path: Path) -> None:
    from buzz_fleet.systemd import resolve_prompt_text

    persona_path = tmp_path / "laravel.persona.md"
    persona_path.write_text(
        "---\n"
        "display_name: Laravel Backend Dev\n"
        "runtime: claude\n"
        "---\n"
        "You are the Laravel dev.\n"
    )
    agent = _agent().model_copy(
        update={"system_prompt_source": SystemPromptSource(kind="persona_file", path=persona_path)}
    )

    assert resolve_prompt_text(agent) == "You are the Laravel dev.\n"


def test_resolve_prompt_text_returns_whole_file_when_no_frontmatter(tmp_path: Path) -> None:
    from buzz_fleet.systemd import resolve_prompt_text

    plain_path = tmp_path / "plain.md"
    plain_path.write_text("Just a plain prompt, no frontmatter.\n")
    agent = _agent().model_copy(
        update={"system_prompt_source": SystemPromptSource(kind="persona_file", path=plain_path)}
    )

    assert resolve_prompt_text(agent) == "Just a plain prompt, no frontmatter.\n"
