"""Content-mapping, failure classification, and status text for the agent
Desktop-visibility feature — the one place `manager.py`, `cli/app.py`, and
`tui/screens/dashboard.py` all import from, instead of three duplicated
copies of the same logic. See the design spec for the field-mapping table
and status-column rules this implements.
"""

from __future__ import annotations

from typing import Literal

from buzz_fleet import systemd
from buzz_fleet.models import Agent


def classify_signer_error(exc: Exception) -> Literal["transient", "permanent"]:
    """A relay rejection with the "invalid: ..." NIP-01 prefix is permanent
    (the input itself is wrong and retrying changes nothing); anything else
    — a connection failure, a timeout, unparseable signer output — is
    transient and safe to retry on the next `ensure_runtime_ready` pass.

    `buzz-fleet-signer`'s own local validation failures (a malformed auth
    tag, an invalid channel_add_policy value, a malformed channel UUID —
    none of which ever reach the relay) are equally permanent, and the
    signer deliberately formats all of them with this same "invalid: "
    prefix so this one check classifies both sources correctly. Any new
    signer-side validation error must follow this convention (`anyhow::
    bail!("invalid: ...")` / `anyhow::anyhow!("invalid: ...")` in Rust) or
    it will be silently misclassified as transient and retried forever.
    """
    return "permanent" if "invalid:" in str(exc) else "transient"


def resolved_channel_add_policy(agent: Agent) -> str:
    return agent.channel_add_policy or "owner_only"


def managed_agent_content(agent: Agent) -> dict:
    """Build the exact JSON content for kind:30177, matching
    `ManagedAgentEventContent`'s snake_case field set. `team_instructions`
    has no dedicated wire slot — it's concatenated before the resolved
    system prompt instead of silently dropped.
    """
    prompt = systemd.resolve_prompt_text(agent)
    if agent.team_instructions:
        prompt = f"{agent.team_instructions}\n\n{prompt}"
    return {
        "name": agent.display_name,
        "persona_id": None,
        "system_prompt": prompt,
        "model": agent.model,
        "provider": None,
        "persona_source_version": None,
        "parallelism": agent.parallelism if agent.parallelism is not None else 1,
        "respond_to": "allowlist" if agent.respond_to_allowlist else "owner-only",
        "respond_to_allowlist": agent.respond_to_allowlist or [],
    }


def visibility_status_text(agent: Agent) -> str:
    """"—" for an old, unmanaged agent; "synced" once every mandatory step
    and channel join succeeded; "error: <reason>" for the first recorded
    permanent failure; "pending" otherwise.
    """
    if not agent.visibility_managed:
        return "—"
    state = agent.visibility_state
    errors = [e for e in (state.profile_error, state.managed_agent_error, state.add_policy_error) if e]
    errors.extend(state.channel_errors.values())
    if errors:
        reason = errors[0]
        if len(reason) > 60:
            reason = reason[:60] + "…"
        return f"error: {reason}"
    channels_joined = all(status == "joined" for status in state.channels.values())
    if state.profile_published and state.managed_agent_published and state.add_policy_published and channels_joined:
        return "synced"
    return "pending"
