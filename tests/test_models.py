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
