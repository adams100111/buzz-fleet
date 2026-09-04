"""Systemd-instance-safe agent id slugs."""

from __future__ import annotations

import re

_INVALID = re.compile(r"[^a-z0-9-]+")
_DASHES = re.compile(r"-+")


def _base_slug(display_name: str) -> str:
    lowered = display_name.lower()
    stripped = _INVALID.sub("-", lowered)
    collapsed = _DASHES.sub("-", stripped).strip("-")
    return collapsed


def agent_slug(display_name: str, existing_ids: set[str]) -> str:
    base = _base_slug(display_name)
    if not base:
        raise ValueError("display_name must contain at least one alphanumeric character")
    if base not in existing_ids:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing_ids:
        suffix += 1
    return f"{base}-{suffix}"
