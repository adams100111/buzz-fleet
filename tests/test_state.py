import stat
from datetime import datetime
from pathlib import Path

from buzz_fleet.models import Agent, Community, SystemPromptSource
from buzz_fleet.state import (
    delete_agent,
    load_agents,
    load_community,
    save_agent,
    save_community,
)


def test_save_and_load_community_round_trips(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    community = Community(id="eltahir", relay_url="wss://buzz.eltahir.me", relay_admin_nsec="nsec1abc")

    save_community(community)
    loaded = load_community("eltahir")

    assert loaded is not None
    assert loaded.relay_url == "wss://buzz.eltahir.me"
    assert loaded.relay_admin_nsec.get_secret_value() == "nsec1abc"


def test_saved_community_file_is_mode_0600(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    save_community(Community(id="eltahir", relay_url="wss://buzz.eltahir.me", relay_admin_nsec="nsec1abc"))

    path = tmp_path / "communities" / "eltahir.json"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_save_and_load_agent_round_trips(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    now = datetime.now()
    agent = Agent(
        id="agent-1",
        community_id="eltahir",
        display_name="Test Agent",
        harness="claude",
        private_key="nsec_private_key_secret",
        public_key="npub_public_key",
        system_prompt_source=SystemPromptSource(kind="inline", text="You are helpful"),
        created_at=now,
    )

    save_agent(agent)
    loaded_agents = load_agents("eltahir")

    assert len(loaded_agents) == 1
    loaded = loaded_agents[0]
    assert loaded.id == "agent-1"
    assert loaded.display_name == "Test Agent"
    assert loaded.private_key.get_secret_value() == "nsec_private_key_secret"
    assert loaded.public_key == "npub_public_key"
    assert loaded.created_at == now


def test_saved_agent_file_is_mode_0600(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    agent = Agent(
        id="agent-1",
        community_id="test-community",
        display_name="Test Agent",
        harness="claude",
        private_key="nsec_secret",
        public_key="npub_public",
        system_prompt_source=SystemPromptSource(kind="inline", text="Test"),
        created_at=datetime.now(),
    )

    save_agent(agent)

    path = tmp_path / "communities" / "test-community" / "agents" / "agent-1.json"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
