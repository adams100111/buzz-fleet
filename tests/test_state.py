import json
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from buzz_fleet import atomic, paths, state
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
    # Fix round 1: also pin secrets_dir() itself (not just its "communities"
    # child) and the secrets *file's* own mode -- neither was asserted before,
    # though both already held in practice via atomic.write_secure's defaults.
    assert oct(stat.S_IMODE(paths.secrets_dir().stat().st_mode)) == "0o700"
    secret_file = paths.secrets_dir() / "communities" / "eltahir.json"
    assert oct(stat.S_IMODE(secret_file.stat().st_mode)) == "0o600"


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


# --- Fix round 1: write ordering and refusal to load/persist a masked secret ---


def _agent(**overrides: object) -> Agent:
    defaults: dict[str, object] = {
        "id": "reviewer",
        "community_id": "eltahir",
        "display_name": "Reviewer",
        "harness": "claude",
        "private_key": SecretStr("nsec1agent"),
        "public_key": "a" * 64,
        "system_prompt_source": SystemPromptSource(kind="inline", text="hi"),
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Agent(**defaults)


def test_load_community_raises_when_secrets_file_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_community(_community())
    secret_path = paths.secrets_dir() / "communities" / "eltahir.json"
    secret_path.unlink()

    with pytest.raises(ValueError, match=r"secrets file .*eltahir\.json.* is missing") as exc:
        state.load_community("eltahir")
    assert str(secret_path) in str(exc.value)


def test_load_agent_raises_when_secrets_file_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_agent(_agent())
    secret_path = paths.secrets_dir() / "communities" / "eltahir" / "agents" / "reviewer.json"
    secret_path.unlink()

    with pytest.raises(ValueError, match=r"secrets file .*reviewer\.json.* is missing") as exc:
        state.load_agents("eltahir")
    assert str(secret_path) in str(exc.value)


def test_empty_secrets_file_raises_not_silently(monkeypatch, tmp_path) -> None:
    """An empty `{}` secrets file exists (unlike the missing-file case above)
    but doesn't account for the community's required `relay_admin_nsec` — the
    merge leaves the mask in place, and that must raise too, not load a
    Community whose secret is the literal string "**********"."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_community(_community())
    secret_path = paths.secrets_dir() / "communities" / "eltahir.json"
    secret_path.write_text("{}")

    with pytest.raises(ValueError, match="does not account for every secret"):
        state.load_community("eltahir")


def test_saving_a_masked_secret_raises_and_leaves_secrets_file_unchanged(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_community(_community())
    secret_path = paths.secrets_dir() / "communities" / "eltahir.json"
    before = secret_path.read_text()

    masked = Community(
        id="eltahir",
        relay_url="wss://relay.example",
        relay_admin_nsec=SecretStr("**********"),
    )
    with pytest.raises(ValueError, match="refusing to persist a masked secret"):
        state.save_community(masked)

    assert secret_path.read_text() == before


def test_saving_a_masked_agent_secret_raises(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="refusing to persist a masked secret"):
        state.save_agent(_agent(private_key=SecretStr("**********")))


def test_second_write_failure_leaves_no_orphaned_state_file(monkeypatch, tmp_path) -> None:
    """Fix round 1: secrets are written before state. If the *second* write
    (the state file) fails, the surviving secrets file is an orphan with no
    matching state file -- invisible to load_community/list_community_ids,
    and therefore harmless -- rather than the reverse (a state file promising
    a secret that was never written)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    real_write_secure = atomic.write_secure
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_write_secure(*args, **kwargs)

    monkeypatch.setattr(atomic, "write_secure", flaky)

    with pytest.raises(OSError):
        state.save_community(_community())

    state_path = paths.state_dir() / "communities" / "eltahir.json"
    secret_path = paths.secrets_dir() / "communities" / "eltahir.json"
    assert not state_path.exists()
    assert secret_path.exists()
    assert state.list_community_ids() == []


def test_active_community_round_trips(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert state.load_active_community() is None

    state.save_active_community("acme")

    assert state.load_active_community() == "acme"


def test_active_community_file_is_mode_0600(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_active_community("acme")

    path = paths.state_dir() / "active-community"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_save_active_community_overwrites_previous_pointer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_active_community("acme")
    state.save_active_community("eltahir")

    assert state.load_active_community() == "eltahir"
