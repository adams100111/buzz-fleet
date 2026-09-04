import stat
from datetime import datetime, timezone
from pathlib import Path

from buzz_fleet.models import Agent, Community, SystemPromptSource
from buzz_fleet.systemd import agent_env_path, agent_prompt_path, write_agent_files


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
        created_at=datetime.now(timezone.utc),
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
