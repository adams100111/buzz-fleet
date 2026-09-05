"""Pydantic models for buzz-fleet's local state."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr


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


class SystemPromptSource(BaseModel):
    kind: Literal["inline", "persona_file"]
    text: str | None = None
    path: Path | None = None


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
    channel_ids: list[str] | None = None
    channel_add_policy: Literal["anyone", "owner_only", "nobody"] | None = None
    visibility_managed: bool = False
    visibility_state: AgentVisibilityState = Field(default_factory=AgentVisibilityState)
    created_at: datetime
