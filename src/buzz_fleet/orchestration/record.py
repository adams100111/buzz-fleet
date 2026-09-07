"""The owner-signed fleet record stored in the fleet channel's `about` (spec 5.9)."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

ABOUT_HEADER = "buzz-fleet orchestration channel — do not edit"


class ConductorEntry(BaseModel):
    pubkey: str
    host: str


class Limits(BaseModel):
    open_adhoc_per_requester: int = 5
    chain_depth: int = 4
    max_rework: int = 3
    max_tasks: int = 20


class FleetRecord(BaseModel):
    version: int = 1
    retrieval_key: str
    conductors: dict[str, ConductorEntry] = Field(default_factory=dict)
    limits: Limits = Field(default_factory=Limits)
    budget: dict | None = None
    retention: str = "keep"
    versions: dict[str, str] = Field(default_factory=dict)
    created_at: int


def encode_about(rec: FleetRecord) -> str:
    return ABOUT_HEADER + "\n" + json.dumps(rec.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)


def decode_about(about: str | None) -> FleetRecord | None:
    if not about:
        return None
    lines = about.split("\n", 1)
    if lines[0].strip() != ABOUT_HEADER or len(lines) < 2:
        return None
    try:
        return FleetRecord.model_validate_json(lines[1])
    except ValueError:
        return None
