from datetime import UTC, datetime

from buzz_fleet.models import Agent, SystemPromptSource


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
