"""Pydantic models for buzz-fleet's local state."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

from buzz_fleet.orchestration.record import FleetRecord

# `McpServer.name` becomes a filesystem path segment (systemd.py's
# `mcp-<name>.sh` wrapper, and the key under `.pi-agent/mcp.json`'s
# `mcpServers`) — restricted to a conservative safe set (no `/`, `\`, `.`,
# or anything else that could traverse out of the agent's own directory)
# rather than merely blocking `..`, since the name is attacker-influenceable
# through an imported `.persona.md` pack the operator did not necessarily
# author, the CLI's `--mcp-name`, and the TUI's MCP-name input alike.
MCP_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# `Agent.env`'s keys (and `PersonaTemplate.env`'s, in personas.py, for the
# same reason) are interpolated raw into a systemd EnvironmentFile line by
# systemd.env_line -- that function carefully escapes/quotes the *value*
# (a real incident: an unescaped newline in a value truncated a live
# agent's instructions to 47 bytes), but a key was never validated at all.
# A key containing a newline (e.g. "GOOD\nBUZZ_ACP_AGENT_OWNER") is read by
# systemd as one bad line plus a real, attacker-chosen assignment; a key
# that merely collides with one buzz-fleet already writes (BUZZ_PRIVATE_KEY,
# BUZZ_ACP_AGENT_OWNER, BUZZ_ACP_RESPOND_TO, PI_CODING_AGENT_DIR, ...) wins
# outright, since systemd lets a later assignment in the same file win and
# `write_agent_files` always writes the `env` block last -- silently
# re-pointing the agent's identity, owner, or respond-to policy. The input
# reaches here from persona frontmatter (an untrusted pack the operator did
# not necessarily author), the CLI's `--env`/`--env-file`, and the TUI's env
# textarea alike -- the same three entry paths `MCP_SERVER_NAME_RE` above
# exists to cover for `McpServer.name`.
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_ENV_KEY_EXACT = {"PI_CODING_AGENT_DIR"}
_RESERVED_ENV_KEY_PREFIX = "BUZZ_"


def validate_env_key(key: str) -> None:
    """Raise ValueError with a clear message if `key` is not safe to write as
    a systemd EnvironmentFile key. Shared by `Agent.env` and
    `personas.PersonaTemplate.env` so every entry path is covered by one
    rule instead of validating (or forgetting to validate) it per call site.
    """
    if not ENV_KEY_RE.match(key):
        raise ValueError(
            f"env var name {key!r} is not a valid identifier — only letters, digits, and '_' are "
            "allowed, and it must not start with a digit"
        )
    if key in _RESERVED_ENV_KEY_EXACT or key.startswith(_RESERVED_ENV_KEY_PREFIX):
        raise ValueError(f"env var name {key!r} is reserved for buzz-fleet's own use and cannot be overridden")


class Community(BaseModel):
    id: str
    relay_url: str
    relay_admin_nsec: SecretStr
    display_name: str | None = None
    # Optional (not required) so a community saved before this field existed
    # still loads — AgentManager.ensure_runtime_ready() backfills it the
    # first time it's needed, then persists it, rather than requiring a
    # migration step or breaking on load.
    owner_pubkey: str | None = None
    # Spec 5.9: the community's orchestration channel and the owner-signed
    # fleet record cached from its metadata. Created once by `fleet init`;
    # discovered everywhere else by `AgentManager.ensure_fleet_record`.
    fleet_channel_id: str | None = None
    fleet_record: FleetRecord | None = None


class SystemPromptSource(BaseModel):
    kind: Literal["inline", "persona_file"]
    text: str | None = None
    path: Path | None = None


class McpServer(BaseModel):
    """A single stdio MCP server to attach to this agent. buzz-acp only
    accepts one MCP server (a bare command, no args/env) — when args or env
    are needed, `systemd.write_mcp_wrapper` generates a small shell wrapper
    script that exports `env` and execs `command`/`args`, and that wrapper's
    path is what's actually handed to buzz-acp via `BUZZ_ACP_MCP_COMMAND`.
    """

    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, SecretStr] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name_is_a_safe_path_segment(cls, value: str) -> str:
        if not MCP_SERVER_NAME_RE.match(value):
            raise ValueError(
                f"MCP server name {value!r} is not a safe filesystem path segment — "
                "only letters, digits, '-', and '_' are allowed"
            )
        return value


class AgentVisibilityState(BaseModel):
    """Per-sub-publish status for the Desktop-visibility feature, tracked so
    `AgentManager._sync_visibility` retries only what's actually missing/
    failed, and so a permanently-broken input (e.g. a nonexistent channel
    UUID) is distinguished from one still genuinely pending. See the design
    spec's "Permanent vs. transient failures" section.
    """

    profile_published: bool = False
    managed_agent_published: bool = False
    add_policy_published: bool = False
    channels: dict[str, Literal["pending", "joined", "error"]] = Field(default_factory=dict)
    profile_error: str | None = None
    managed_agent_error: str | None = None
    add_policy_error: str | None = None
    channel_errors: dict[str, str] = Field(default_factory=dict)


class Agent(BaseModel):
    id: str
    community_id: str
    display_name: str
    harness: Literal["claude", "codex", "pi", "goose"]
    private_key: SecretStr
    public_key: str
    system_prompt_source: SystemPromptSource
    team_instructions: str | None = None
    model: str | None = None
    parallelism: int | None = None
    idle_timeout_seconds: int | None = None
    max_turn_duration_seconds: int | None = None
    respond_to_allowlist: list[str] | None = None
    # buzz-acp session scoping (spec 5.8): `thread` (default) isolates each
    # channel thread into its own provider session so a run is a shared,
    # memory-keeping session while the owner can DM the agent in parallel.
    # `channel` is buzz-acp's legacy one-session-per-channel, the rollback.
    session_policy: Literal["thread", "channel"] | None = None
    # Rotate an *active* session after N turns. Dormant sessions are handled
    # by the recycle timer (plan 2), not by this cap.
    max_turns_per_session: int | None = None
    # Seconds between buzz-acp heartbeat prompts; the agent-side delivery
    # recovery path (spec fact 6). 0 disables.
    heartbeat_interval_seconds: int | None = None
    # Agent directory (spec 5.10): published in the managed-agent record so
    # every machine and every agent can choose agents by role and capability.
    role: str | None = None
    capabilities: list[str] | None = None
    description: str | None = None
    channel_ids: list[str] | None = None
    channel_add_policy: Literal["anyone", "owner_only", "nobody"] | None = None
    visibility_managed: bool = False
    visibility_state: AgentVisibilityState = Field(default_factory=AgentVisibilityState)
    # Generic per-agent env-var passthrough (spec 5.13) — a registry token, a
    # deploy key, a database URL, or a harness-specific provider selector
    # (e.g. GOOSE_PROVIDER) with nowhere else to live. Written into the
    # agent's env file (systemd.write_agent_files) after the API keys. Never
    # published anywhere — see manager.py's content_fields for why.
    env: dict[str, SecretStr] | None = None
    # buzz-acp accepts exactly one stdio MCP server. Pi has no MCP support
    # of its own and gets it through the pi-mcp-adapter extension instead
    # (systemd.write_agent_files' Pi-specific handling).
    mcp_server: McpServer | None = None
    created_at: datetime

    @field_validator("env")
    @classmethod
    def _env_keys_are_safe(cls, value: dict[str, SecretStr] | None) -> dict[str, SecretStr] | None:
        for key in value or {}:
            validate_env_key(key)
        return value
