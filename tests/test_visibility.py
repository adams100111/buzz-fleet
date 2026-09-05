from datetime import UTC, datetime

from buzz_fleet.models import Agent, AgentVisibilityState, SystemPromptSource
from buzz_fleet.visibility import (
    classify_signer_error,
    managed_agent_content,
    resolved_channel_add_policy,
    visibility_status_text,
)


def _agent(**overrides) -> Agent:
    defaults = {
        "id": "test-agent",
        "community_id": "eltahir",
        "display_name": "Test Agent",
        "harness": "claude",
        "private_key": "nsec1x",
        "public_key": "a" * 64,
        "system_prompt_source": SystemPromptSource(kind="inline", text="You are a test agent."),
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Agent(**defaults)


def test_classify_signer_error_permanent_on_invalid_prefix() -> None:
    assert classify_signer_error(RuntimeError("join-channel failed: invalid: channel not found")) == "permanent"


def test_classify_signer_error_transient_otherwise() -> None:
    assert classify_signer_error(RuntimeError("join-channel failed: connection refused")) == "transient"
    assert classify_signer_error(ValueError("Expecting value: line 1 column 1")) == "transient"


def test_classify_signer_error_permanent_on_signer_local_validation_failures() -> None:
    """The signer's own local validation failures (never reach the relay)
    must classify as permanent too, matching the "invalid: " convention
    documented on classify_signer_error — otherwise a malformed channel
    UUID, auth tag, or channel_add_policy value retries forever instead of
    surfacing as a visible error.
    """
    assert (
        classify_signer_error(RuntimeError("join-channel failed: invalid: channel_id invalid length: expected..."))
        == "permanent"
    )
    assert (
        classify_signer_error(
            RuntimeError('publish-agent-add-policy failed: invalid: channel_add_policy "sometimes" (must be ...)')
        )
        == "permanent"
    )
    assert (
        classify_signer_error(
            RuntimeError(
                'publish-agent-profile failed: invalid: auth tag must be a 4-element JSON array starting with "auth"'
            )
        )
        == "permanent"
    )


def test_resolved_channel_add_policy_defaults_to_owner_only() -> None:
    assert resolved_channel_add_policy(_agent()) == "owner_only"
    assert resolved_channel_add_policy(_agent(channel_add_policy="anyone")) == "anyone"


def test_managed_agent_content_maps_fields_and_derives_respond_to() -> None:
    agent = _agent(model="claude-sonnet-5", parallelism=3, respond_to_allowlist=["b" * 64])
    content = managed_agent_content(agent)
    assert content["name"] == "Test Agent"
    assert content["model"] == "claude-sonnet-5"
    assert content["parallelism"] == 3
    assert content["respond_to"] == "allowlist"
    assert content["respond_to_allowlist"] == ["b" * 64]
    assert content["persona_id"] is None
    assert content["provider"] is None


def test_managed_agent_content_defaults_parallelism_and_respond_to() -> None:
    content = managed_agent_content(_agent())
    assert content["parallelism"] == 1
    assert content["respond_to"] == "owner-only"


def test_managed_agent_content_prepends_team_instructions() -> None:
    agent = _agent(team_instructions="Test-first. Strict typing.")
    content = managed_agent_content(agent)
    assert content["system_prompt"] == "Test-first. Strict typing.\n\nYou are a test agent."


def test_visibility_status_text_old_agent_shows_dash() -> None:
    assert visibility_status_text(_agent()) == "—"


def test_visibility_status_text_synced_when_everything_done() -> None:
    agent = _agent(
        visibility_managed=True,
        visibility_state=AgentVisibilityState(
            profile_published=True, managed_agent_published=True, add_policy_published=True
        ),
    )
    assert visibility_status_text(agent) == "synced"


def test_visibility_status_text_pending_when_something_incomplete() -> None:
    agent = _agent(visibility_managed=True)
    assert visibility_status_text(agent) == "pending"


def test_visibility_status_text_surfaces_permanent_error() -> None:
    agent = _agent(
        visibility_managed=True,
        visibility_state=AgentVisibilityState(channel_errors={"c1": "invalid: channel not found"}),
    )
    assert visibility_status_text(agent) == "error: invalid: channel not found"


def test_visibility_status_text_truncates_long_error() -> None:
    long_reason = "invalid: " + "x" * 200
    agent = _agent(
        visibility_managed=True,
        visibility_state=AgentVisibilityState(profile_error=long_reason),
    )
    result = visibility_status_text(agent)
    assert result.startswith("error: ")
    assert result.endswith("…")
    assert len(result) < len(f"error: {long_reason}")
    assert len(result) == len("error: ") + 60 + 1


def test_visibility_status_text_does_not_alter_short_error() -> None:
    agent = _agent(
        visibility_managed=True,
        visibility_state=AgentVisibilityState(profile_error="invalid: short reason"),
    )
    assert visibility_status_text(agent) == "error: invalid: short reason"
