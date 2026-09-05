"""Detect which Buzz agent harnesses (ACP adapters) are actually installed.

Mirrors Buzz Desktop's runtime-discovery model (`desktop/src-tauri/src/managed_agents/discovery.rs`
and `presets.rs`): each harness has an adapter binary buzz-acp actually shells out to
(`_HARNESS_COMMAND` in `systemd.py`), and an "underlying CLI" a user is more likely to have
installed on its own. Missing the adapter but having the underlying CLI is a distinct,
more encouraging state ("adapter_missing") than having neither ("not_installed") — the
same distinction Desktop draws. Unlike Desktop, this runs inside the user's own shell/PATH
already (buzz-fleet is a CLI/TUI tool, not a GUI app launched outside one), so a plain
`shutil.which()` is sufficient — no login-shell spawn or sidecar-path search is needed.
"""

from __future__ import annotations

import shutil
from typing import Literal

HarnessAvailability = Literal["available", "adapter_missing", "not_installed"]

HARNESSES = ["claude", "codex", "pi", "goose"]

_ADAPTER_COMMANDS: dict[str, list[str]] = {
    "claude": ["claude-agent-acp", "claude-code-acp"],
    "codex": ["codex-acp"],
    "pi": ["pi-acp"],
    "goose": ["goose"],
}

_UNDERLYING_CLI: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "pi": "pi",
    "goose": "goose",
}

_STATUS_SUFFIX: dict[HarnessAvailability, str] = {
    "available": "",
    "adapter_missing": " (adapter missing)",
    "not_installed": " (not installed)",
}

_SORT_RANK: dict[HarnessAvailability, int] = {
    "available": 0,
    "adapter_missing": 1,
    "not_installed": 2,
}


def detect_harness_availability() -> dict[str, HarnessAvailability]:
    """Check each known harness's adapter binary (and underlying CLI) on PATH."""
    availability: dict[str, HarnessAvailability] = {}
    for harness in HARNESSES:
        if any(shutil.which(cmd) for cmd in _ADAPTER_COMMANDS[harness]):
            availability[harness] = "available"
        elif shutil.which(_UNDERLYING_CLI[harness]):
            availability[harness] = "adapter_missing"
        else:
            availability[harness] = "not_installed"
    return availability


def harness_select_options() -> list[tuple[str, str]]:
    """(label, value) pairs for the harness Select, available harnesses first.

    The value in each pair is always the plain harness name (e.g. "claude"),
    never the decorated label — callers set/read `Select.value` by that name.
    """
    availability = detect_harness_availability()
    ordered = sorted(HARNESSES, key=lambda h: _SORT_RANK[availability[h]])
    return [(f"{h}{_STATUS_SUFFIX[availability[h]]}", h) for h in ordered]


def default_harness() -> str:
    """The first available harness, or the first known harness if none are available."""
    availability = detect_harness_availability()
    for harness in HARNESSES:
        if availability[harness] == "available":
            return harness
    return HARNESSES[0]
