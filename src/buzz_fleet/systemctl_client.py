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


def status(runner: CommandRunner, key: str) -> AgentStatus:
    result = runner.run(["systemctl", "--user", "is-active", _unit(key)])
    return _STATE_MAP.get(result.stdout.strip(), AgentStatus.UNKNOWN)


def tail_logs(runner: CommandRunner, key: str, lines: int = 200) -> str:
    result = runner.run(["journalctl", "--user", "-u", _unit(key), "-n", str(lines), "--no-pager"])
    return result.stdout
