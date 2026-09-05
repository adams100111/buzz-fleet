"""Detect which Buzz agent harnesses (ACP adapters) are actually installed.

Mirrors Buzz Desktop's runtime-discovery model (`desktop/src-tauri/src/managed_agents/discovery.rs`
and `presets.rs`): each harness has an adapter binary buzz-acp actually shells out to
(`_ADAPTER_COMMANDS` below — the single source of truth `systemd.write_agent_files` also
uses via `resolve_adapter_command`), and an "underlying CLI" a user is more likely to have
installed on its own. Missing the adapter but having the underlying CLI is a distinct,
more encouraging state ("adapter_missing") than having neither ("not_installed") — the
same distinction Desktop draws. Unlike Desktop, this runs inside the user's own shell/PATH
already (buzz-fleet is a CLI/TUI tool, not a GUI app launched outside one), so a plain
`shutil.which()` is sufficient — no login-shell spawn or sidecar-path search is needed.

`resolve_adapter_command` matters for a real reason beyond detection: systemd's `--user`
manager has its own fixed, minimal PATH (`/usr/local/bin:/usr/bin:...`) that does NOT
include a version manager's per-tool install directories (mise, nvm, asdf, volta, ...) —
so a `buzz-agent@*.service` unit can fail to find an adapter that's genuinely installed
and on the *user's* PATH. Resolving to an absolute path once, here, at the point
`buzz-fleet` itself writes the agent's env file (inheriting whatever PATH the user
actually launched it with) sidesteps that entirely — no PATH list to guess or maintain
inside the unit file.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from buzz_fleet.proc import CommandRunner

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

# Verified against buzz's own desktop/src-tauri/src/managed_agents/discovery/{catalog,presets}.rs
# and desktop/src/features/agents/ui/runtimeAvailabilityWarning.ts — the exact commands Buzz
# Desktop's own "adapter missing" hint shows. codex-acp is pinned to 1.x: buzz's CHANGELOG notes
# a minimum-version gate added when 1.x shipped (PR #1750) — an unpinned install can resolve 0.x.
# goose has no known automated install here (not an npm package at all) — None means "point the
# user at manual install instructions" rather than fabricate a command.
_INSTALL_COMMANDS: dict[str, list[list[str]] | None] = {
    "claude": [["npm", "install", "-g", "@agentclientprotocol/claude-agent-acp"]],
    "codex": [["npm", "install", "-g", "@agentclientprotocol/codex-acp@^1"]],
    "pi": [
        ["npm", "install", "-g", "--ignore-scripts", "@earendil-works/pi-coding-agent"],
        ["npm", "install", "-g", "pi-acp"],
    ],
    "goose": None,
}


def resolve_adapter_command(harness: str) -> str:
    """The command to put in `BUZZ_ACP_AGENT_COMMAND` for `harness`.

    Returns an absolute path when `shutil.which()` can resolve one of the
    harness's adapter aliases from here — the environment `buzz-fleet`
    itself is running in — otherwise the bare, first-listed alias name
    unresolved, so `buzz-acp`'s own (much more limited) PATH lookup still
    gets a chance rather than writing nothing at all.
    """
    for command in _ADAPTER_COMMANDS[harness]:
        resolved = shutil.which(command)
        if resolved:
            return resolved
    return _ADAPTER_COMMANDS[harness][0]


def install_commands(harness: str) -> list[list[str]] | None:
    """The npm command(s) that install `harness`'s adapter, or None if none is known."""
    return _INSTALL_COMMANDS[harness]


def install_adapter(runner: CommandRunner, harness: str) -> None:
    """Run `harness`'s install command(s) in order. Raises RuntimeError on any failure."""
    commands = install_commands(harness)
    if commands is None:
        raise RuntimeError(
            f"No automated install is known for '{harness}' — see https://github.com/block/goose"
            if harness == "goose"
            else f"No automated install is known for '{harness}'."
        )
    for command in commands:
        result = runner.run(command)
        if result.returncode != 0:
            raise RuntimeError(
                f"Installing {harness}'s adapter failed (`{' '.join(command)}`): "
                f"{result.stderr.strip()}"
            )


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
