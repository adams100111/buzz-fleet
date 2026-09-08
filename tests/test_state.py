import json
import stat
from datetime import UTC, datetime
from pathlib import Path

from pydantic import SecretStr

from buzz_fleet import paths, state
from buzz_fleet.models import Agent, Community, SystemPromptSource
from buzz_fleet.state import (
    load_agents,
    load_community,
    save_agent,
    save_community,
)


def test_save_and_load_community_round_trips(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    community = Community(id="eltahir", relay_url="wss://buzz.eltahir.me", relay_admin_nsec="nsec1abc")

    save_community(community)
    loaded = load_community("eltahir")

    assert loaded is not None
    assert loaded.relay_url == "wss://buzz.eltahir.me"
    assert loaded.relay_admin_nsec.get_secret_value() == "nsec1abc"


def test_saved_community_file_is_mode_0600(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    save_community(Community(id="eltahir", relay_url="wss://buzz.eltahir.me", relay_admin_nsec="nsec1abc"))

    path = paths.state_dir() / "communities" / "eltahir.json"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_save_and_load_agent_round_trips(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    now = datetime.now(UTC)
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
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    agent = Agent(
        id="agent-1",
        community_id="test-community",
        display_name="Test Agent",
        harness="claude",
        private_key="nsec_secret",
        public_key="npub_public",
        system_prompt_source=SystemPromptSource(kind="inline", text="Test"),
        created_at=datetime.now(UTC),
    )

    save_agent(agent)

    path = paths.state_dir() / "communities" / "test-community" / "agents" / "agent-1.json"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_list_community_ids(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert state.list_community_ids() == []
    for cid in ("b", "a"):
        state.save_community(Community(id=cid, relay_url="wss://r", relay_admin_nsec="nsec1a"))
    assert state.list_community_ids() == ["a", "b"]


def _community() -> Community:
    return Community(
        id="eltahir",
        relay_url="wss://relay.example",
        relay_admin_nsec=SecretStr("nsec1secret"),
    )


def test_state_file_contains_no_secret(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_community(_community())
    raw = (paths.state_dir() / "communities" / "eltahir.json").read_text()
    assert "nsec1secret" not in raw
    assert json.loads(raw)["relay_url"] == "wss://relay.example"


def test_secret_file_contains_the_secret(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_community(_community())
    secret = paths.secrets_dir() / "communities" / "eltahir.json"
    assert json.loads(secret.read_text())["relay_admin_nsec"] == "nsec1secret"


def test_round_trip_restores_the_secret(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_community(_community())
    loaded = state.load_community("eltahir")
    assert loaded is not None
    assert loaded.relay_admin_nsec.get_secret_value() == "nsec1secret"


def test_secrets_directory_is_0700(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_community(_community())
    mode = stat.S_IMODE((paths.secrets_dir() / "communities").stat().st_mode)
    assert oct(mode) == "0o700"


def test_nested_and_dict_secrets_round_trip(monkeypatch, tmp_path) -> None:
    """Agent.env is dict[str, SecretStr] and mcp_server.env is nested one level
    deeper — both must survive the split, and neither may leak into state."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from buzz_fleet.models import Agent, McpServer, SystemPromptSource

    agent = Agent(
        id="reviewer",
        community_id="eltahir",
        display_name="Reviewer",
        harness="claude",
        private_key=SecretStr("nsec1agent"),
        public_key="a" * 64,
        system_prompt_source=SystemPromptSource(kind="inline", text="hi"),
        env={"ANTHROPIC_API_KEY": SecretStr("sk-top")},
        mcp_server=McpServer(name="m", command="c", args=[], env={"TOKEN": SecretStr("tok")}),
        created_at=datetime.now(UTC),
    )
    state.save_agent(agent)
    raw = (paths.state_dir() / "communities" / "eltahir" / "agents" / "reviewer.json").read_text()
    for leaked in ("nsec1agent", "sk-top", "tok"):
        assert leaked not in raw

    loaded = state.load_agents("eltahir")[0]
    assert loaded.private_key.get_secret_value() == "nsec1agent"
    assert loaded.env["ANTHROPIC_API_KEY"].get_secret_value() == "sk-top"
    assert loaded.mcp_server.env["TOKEN"].get_secret_value() == "tok"


def test_delete_agent_removes_both_files(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from buzz_fleet.models import Agent, SystemPromptSource

    state.save_agent(
        Agent(
            id="reviewer",
            community_id="eltahir",
            display_name="R",
            harness="claude",
            private_key=SecretStr("nsec1agent"),
            public_key="a" * 64,
            system_prompt_source=SystemPromptSource(kind="inline", text="hi"),
            created_at=datetime.now(UTC),
        )
    )
    state.delete_agent("eltahir", "reviewer")
    assert state.load_agents("eltahir") == []
    assert not (paths.secrets_dir() / "communities" / "eltahir" / "agents" / "reviewer.json").exists()
