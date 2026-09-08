"""Wrap systemctl/journalctl for buzz-agent@<community>:<agent> instance units."""

from __future__ import annotations

from enum import Enum, auto

from buzz_fleet import units
from buzz_fleet.proc import CommandRunner


class AgentStatus(Enum):
    RUNNING = auto()
    STARTING = auto()
    STOPPED = auto()
    FAILED = auto()
    UNKNOWN = auto()


def _unit(key: str) -> str:
    """`key` is a community-qualified instance key (see units.instance_key).

    Before this took a bare agent id, and two communities with the same agent
    name addressed one another's units.
    """
    units.split_key(key)  # refuse an unqualified id loudly
    return units.unit_name(key)


def _run_or_raise(runner: CommandRunner, args: list[str]) -> None:
    result = runner.run(args)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {result.stderr}")


def enable_now(runner: CommandRunner, key: str) -> None:
    _run_or_raise(runner, ["systemctl", "--user", "enable", "--now", _unit(key)])


def disable_now(runner: CommandRunner, key: str) -> None:
    _run_or_raise(runner, ["systemctl", "--user", "disable", "--now", _unit(key)])


def restart(runner: CommandRunner, key: str) -> None:
    _run_or_raise(runner, ["systemctl", "--user", "restart", _unit(key)])


def stop(runner: CommandRunner, key: str) -> None:
    _run_or_raise(runner, ["systemctl", "--user", "stop", _unit(key)])


_STATE_MAP = {
    "active": AgentStatus.RUNNING,
    # A unit crash-looping under Restart=on-failure spends real time in
    # "activating" between restart attempts — this used to fall through to
    # UNKNOWN, hiding an actively-failing agent behind a status that reads
    # as "nothing to see here". "reloading" is the analogous transient
    # state for units that support reload. Real incident: an agent whose
    # exec target didn't exist crash-looped 700+ times reporting "unknown".
    "activating": AgentStatus.STARTING,
    "reloading": AgentStatus.STARTING,
    "inactive": AgentStatus.STOPPED,
    "deactivating": AgentStatus.STOPPED,
    "failed": AgentStatus.FAILED,
}


def raw_state_of_unit(runner: CommandRunner, unit: str) -> str:
    """The literal, unclassified `systemctl is-active` answer for `unit`.

    For the one caller (`migrate.py`'s active-unit refusal) that must NOT
    go through `_STATE_MAP`'s coarsening: `AgentStatus.STOPPED` folds
    "inactive" and "deactivating" together for display purposes, but a
    migration refusal needs to tell them apart — a "deactivating" unit is
    mid-cycle on an auto-restarting (`Restart=on-failure`) crash loop just
    as much as "activating" is (a live agent transits `deactivating ->
    activating -> active` on every restart), so accepting it as safe closes
    only half of the exact crash-loop hazard this refusal exists for.
    Migrate builds its own explicit allowlist directly from this raw
    string rather than from any `AgentStatus` bucket, so "safe" never
    depends on how `_STATE_MAP` happens to be grouped for a different
    purpose.
    """
    return runner.run(["systemctl", "--user", "is-active", unit]).stdout.strip()


def status_of_unit(runner: CommandRunner, unit: str) -> AgentStatus:
    """As `status`, but takes a literal systemd unit name rather than a
    community-qualified instance key.

    The one caller that needs this is `migrate.py`'s active-unit refusal: it
    has to query a *legacy*, unqualified `buzz-agent@<agent-id>.service` name
    (that's genuinely what it's called before migration), and `status`'s own
    `_unit()` call would reject that outright via `units.split_key`. Anything
    unrecognised (an empty string from a broken/unreachable systemctl
    included, since it isn't a key in `_STATE_MAP`) maps to `UNKNOWN` here
    exactly as it does for `status` — callers that need "safe to act on"
    rather than "for display" should treat `UNKNOWN` as unsafe, not as a
    stand-in for `STOPPED`.
    """
    result = runner.run(["systemctl", "--user", "is-active", unit])
    return _STATE_MAP.get(result.stdout.strip(), AgentStatus.UNKNOWN)


def status(runner: CommandRunner, key: str) -> AgentStatus:
    return status_of_unit(runner, _unit(key))


def tail_logs(runner: CommandRunner, key: str, lines: int = 200) -> str:
    result = runner.run(["journalctl", "--user", "-u", _unit(key), "-n", str(lines), "--no-pager"])
    return result.stdout
