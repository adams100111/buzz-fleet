from datetime import UTC, datetime

from buzz_fleet.models import Agent, AgentVisibilityState, SystemPromptSource


def _base_kwargs() -> dict:
    return {
        "id": "test-agent",
        "community_id": "eltahir",
        "display_name": "Test Agent",
        "harness": "claude",
        "private_key": "nsec1x",
        "public_key": "a" * 64,
        "system_prompt_source": SystemPromptSource(kind="inline", text="hi"),
        "created_at": datetime.now(UTC),
    }


def test_new_fields_default_to_none() -> None:
    agent = Agent(**_base_kwargs())
    assert agent.parallelism is None
    assert agent.idle_timeout_seconds is None
    assert agent.max_turn_duration_seconds is None
    assert agent.respond_to_allowlist is None


def test_new_fields_round_trip_through_json() -> None:
    agent = Agent(
        **_base_kwargs(),
        parallelism=3,
        idle_timeout_seconds=120,
        max_turn_duration_seconds=600,
        respond_to_allowlist=["a" * 64, "b" * 64],
    )
    restored = Agent.model_validate_json(agent.model_dump_json())
    assert restored.parallelism == 3
    assert restored.idle_timeout_seconds == 120
    assert restored.max_turn_duration_seconds == 600
    assert restored.respond_to_allowlist == ["a" * 64, "b" * 64]


def _agent(**overrides) -> Agent:
    defaults = _base_kwargs()
    defaults.update(overrides)
    return Agent(**defaults)


def test_new_agent_defaults_to_not_visibility_managed() -> None:
    agent = _agent()
    assert agent.visibility_managed is False
    assert agent.visibility_state == AgentVisibilityState()


def test_agent_loaded_from_json_without_visibility_fields_defaults_safely() -> None:
    """Regression test for the old-agent exemption: an Agent record saved
    before this feature existed has no `visibility_managed`/`visibility_state`
    keys in its JSON at all — it must load with the safe defaults, not raise.
    """
    agent = _agent()
    old_json = agent.model_dump_json(exclude={"visibility_managed", "visibility_state"})
    reloaded = Agent.model_validate_json(old_json)
    assert reloaded.visibility_managed is False
    assert reloaded.visibility_state.profile_published is False


def test_visibility_state_tracks_channel_outcomes_independently() -> None:
    state = AgentVisibilityState(channels={"c1": "joined", "c2": "error"}, channel_errors={"c2": "invalid: channel not found"})
    assert state.channels["c1"] == "joined"
    assert state.channel_errors["c2"] == "invalid: channel not found"


def test_session_fields_default_none_and_round_trip() -> None:
    agent = Agent(**_base_kwargs())
    assert (agent.session_policy, agent.max_turns_per_session, agent.heartbeat_interval_seconds) == (None, None, None)
    again = Agent.model_validate_json(
        Agent(**_base_kwargs(), session_policy="channel", max_turns_per_session=12, heartbeat_interval_seconds=300).model_dump_json()
    )
    assert (again.session_policy, again.max_turns_per_session, again.heartbeat_interval_seconds) == ("channel", 12, 300)


def test_directory_fields_round_trip() -> None:
    agent = Agent(
        **_base_kwargs(), role="reviewer", capabilities=["laravel", "security-review"], description="Reviews PHP."
    )
    again = Agent.model_validate_json(agent.model_dump_json())
    assert (again.role, again.capabilities, again.description) == (
        "reviewer",
        ["laravel", "security-review"],
        "Reviews PHP.",
    )


def test_directory_fields_default_to_none() -> None:
    agent = _agent()
    assert (agent.role, agent.capabilities, agent.description) == (None, None, None)


def test_community_fleet_fields_default_none() -> None:
    from buzz_fleet.models import Community

    c = Community(id="e", relay_url="wss://r", relay_admin_nsec="nsec1a")
    assert c.fleet_channel_id is None and c.fleet_record is None


def test_env_and_mcp_default_to_none() -> None:
    agent = _agent()
    assert agent.env is None
    assert agent.mcp_server is None


def test_env_and_mcp_round_trip_with_secrets(tmp_path, monkeypatch) -> None:
    from buzz_fleet import state
    from buzz_fleet.models import McpServer

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    agent = Agent(**_base_kwargs(), env={"DATABASE_URL": "postgres://x"},
                  mcp_server=McpServer(name="boost", command="php", args=["artisan", "boost:mcp"], env={"TOKEN": "t"}))
    state.save_agent(agent)
    again = state.load_agents("eltahir")[0]
    assert again.env is not None
    assert again.env["DATABASE_URL"].get_secret_value() == "postgres://x"
    assert again.mcp_server is not None
    assert again.mcp_server.env["TOKEN"].get_secret_value() == "t" and again.mcp_server.args == ["artisan", "boost:mcp"]


def test_mcp_server_name_rejects_path_traversal() -> None:
    """Real defect this guards: McpServer.name becomes a filesystem path
    segment (systemd's `mcp-<name>.sh` wrapper) built directly from this
    field. Before this validator, name="../../../pwned" wrote a real,
    executable, secret-bearing file outside the agent's own directory
    entirely.
    """
    import pytest

    from buzz_fleet.models import McpServer

    with pytest.raises(ValueError, match="not a safe filesystem path segment"):
        McpServer(name="../../../pwned", command="php")


def test_mcp_server_name_rejects_slash() -> None:
    import pytest

    from buzz_fleet.models import McpServer

    with pytest.raises(ValueError, match="not a safe filesystem path segment"):
        McpServer(name="a/b", command="php")


def test_mcp_server_name_rejects_leading_dot() -> None:
    import pytest

    from buzz_fleet.models import McpServer

    with pytest.raises(ValueError, match="not a safe filesystem path segment"):
        McpServer(name=".hidden", command="php")


def test_mcp_server_name_rejects_empty_string() -> None:
    import pytest

    from buzz_fleet.models import McpServer

    with pytest.raises(ValueError, match="not a safe filesystem path segment"):
        McpServer(name="", command="php")


def test_mcp_server_name_accepts_safe_characters() -> None:
    from buzz_fleet.models import McpServer

    server = McpServer(name="boost-server_2", command="php")
    assert server.name == "boost-server_2"


def test_agent_env_rejects_key_with_embedded_newline() -> None:
    # Finding 4 (final review): systemd.env_line carefully escapes/quotes
    # the *value* of a KEY=value line -- the key itself was interpolated
    # raw. `env_line("GOOD\nBUZZ_ACP_AGENT_OWNER", "deadbeef")` produced two
    # lines: one bad, one a real assignment systemd reads as genuine. Must
    # be refused at the model boundary, the earliest point every entry path
    # (CLI, TUI, persona import) shares.
    import pytest
    from pydantic import SecretStr

    with pytest.raises(ValueError, match="not a valid identifier"):
        Agent(**_base_kwargs(), env={"GOOD\nBUZZ_ACP_AGENT_OWNER": SecretStr("deadbeef")})


def test_agent_env_rejects_non_identifier_key() -> None:
    import pytest
    from pydantic import SecretStr

    with pytest.raises(ValueError, match="not a valid identifier"):
        Agent(**_base_kwargs(), env={"has space": SecretStr("x")})
    with pytest.raises(ValueError, match="not a valid identifier"):
        Agent(**_base_kwargs(), env={"1STARTS_WITH_DIGIT": SecretStr("x")})
    with pytest.raises(ValueError, match="not a valid identifier"):
        Agent(**_base_kwargs(), env={"": SecretStr("x")})


def test_agent_env_rejects_buzz_prefix() -> None:
    # Finding 4 (final review): write_agent_files writes the `env` block
    # *after* BUZZ_PRIVATE_KEY/BUZZ_ACP_AGENT_OWNER/BUZZ_ACP_RESPOND_TO and
    # the fleet variables, and systemd lets a later assignment win -- an env
    # entry could silently re-point the agent's identity, owner, or
    # respond-to policy. Refusing the whole BUZZ_ prefix closes this
    # regardless of write order.
    import pytest
    from pydantic import SecretStr

    with pytest.raises(ValueError, match="reserved"):
        Agent(**_base_kwargs(), env={"BUZZ_ACP_AGENT_OWNER": SecretStr("attacker-pubkey")})


def test_agent_env_rejects_pi_coding_agent_dir() -> None:
    import pytest
    from pydantic import SecretStr

    with pytest.raises(ValueError, match="reserved"):
        Agent(**_base_kwargs(), env={"PI_CODING_AGENT_DIR": SecretStr("/tmp/evil")})


def test_agent_env_accepts_safe_keys() -> None:
    from pydantic import SecretStr

    agent = Agent(**_base_kwargs(), env={"DATABASE_URL": SecretStr("postgres://x"), "GOOSE_PROVIDER": SecretStr("anthropic")})
    assert agent.env is not None and set(agent.env) == {"DATABASE_URL", "GOOSE_PROVIDER"}
