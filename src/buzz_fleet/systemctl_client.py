"""Wrap systemctl/journalctl for buzz-agent@<id> instance units."""

from __future__ import annotations

from enum import Enum, auto

from buzz_fleet.proc import CommandRunner


class AgentStatus(Enum):
    RUNNING = auto()
    STOPPED = auto()
    FAILED = auto()
    UNKNOWN = auto()


def _unit(agent_id: str) -> str:
    return f"buzz-agent@{agent_id}"


def _run_or_raise(runner: CommandRunner, args: list[str]) -> None:
    result = runner.run(args)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {result.stderr}")


def enable_now(runner: CommandRunner, agent_id: str) -> None:
    _run_or_raise(runner, ["systemctl", "--user", "enable", "--now", _unit(agent_id)])


def disable_now(runner: CommandRunner, agent_id: str) -> None:
    _run_or_raise(runner, ["systemctl", "--user", "disable", "--now", _unit(agent_id)])


def restart(runner: CommandRunner, agent_id: str) -> None:
    _run_or_raise(runner, ["systemctl", "--user", "restart", _unit(agent_id)])


def stop(runner: CommandRunner, agent_id: str) -> None:
    _run_or_raise(runner, ["systemctl", "--user", "stop", _unit(agent_id)])


_STATE_MAP = {
    "active": AgentStatus.RUNNING,
    "inactive": AgentStatus.STOPPED,
    "failed": AgentStatus.FAILED,
}


def status(runner: CommandRunner, agent_id: str) -> AgentStatus:
    result = runner.run(["systemctl", "--user", "is-active", _unit(agent_id)])
    return _STATE_MAP.get(result.stdout.strip(), AgentStatus.UNKNOWN)


def tail_logs(runner: CommandRunner, agent_id: str, lines: int = 200) -> str:
    result = runner.run(["journalctl", "--user", "-u", _unit(agent_id), "-n", str(lines), "--no-pager"])
    return result.stdout
