"""Stable ids for tasks, attempts, runs; short display; prefix lookup."""

from __future__ import annotations

import uuid
from collections.abc import Iterable


def new_id() -> str:
    return str(uuid.uuid4())


def short(full: str) -> str:
    return full[:8]


def match_prefix(prefix: str, candidates: Iterable[str]) -> str:
    matches = [c for c in candidates if c.startswith(prefix)]
    if not matches:
        raise ValueError(f"unknown id {prefix!r}")
    if len(matches) > 1:
        raise ValueError(f"ambiguous id {prefix!r}: " + ", ".join(short(m) for m in matches))
    return matches[0]
