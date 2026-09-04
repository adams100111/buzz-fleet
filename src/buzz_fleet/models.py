"""Pydantic models for buzz-fleet's local state."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, SecretStr


class Community(BaseModel):
    id: str
    relay_url: str
    relay_admin_nsec: SecretStr
    display_name: str | None = None


class SystemPromptSource(BaseModel):
    kind: Literal["inline", "persona_file"]
    text: str | None = None
    path: Path | None = None


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
    created_at: datetime
