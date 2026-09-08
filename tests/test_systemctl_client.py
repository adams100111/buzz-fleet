import subprocess

import pytest

from buzz_fleet.systemctl_client import (
    AgentStatus,
    disable_now,
    enable_now,
    restart,
    status,
    stop,
    tail_logs,
)


class FakeRunner:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        return subprocess.CompletedProcess(args, self.returncode, stdout=self.stdout, stderr="")


def test_enable_now_invokes_systemctl_with_instance_unit() -> None:
    runner = FakeRunner()
    enable_now(runner, "acme:laravel-backend-dev")
    assert runner.calls == [["systemctl", "--user", "enable", "--now", "buzz-agent@acme:laravel-backend-dev.service"]]


def test_restart_invokes_systemctl_restart() -> None:
    runner = FakeRunner()
    restart(runner, "acme:laravel-backend-dev")
    assert runner.calls == [["systemctl", "--user", "restart", "buzz-agent@acme:laravel-backend-dev.service"]]


def test_status_active_maps_to_running() -> None:
    runner = FakeRunner(stdout="active\n")
    assert status(runner, "acme:laravel-backend-dev") == AgentStatus.RUNNING


def test_status_failed_maps_to_failed() -> None:
    runner = FakeRunner(stdout="failed\n")
    assert status(runner, "acme:laravel-backend-dev") == AgentStatus.FAILED


def test_status_inactive_maps_to_stopped() -> None:
    runner = FakeRunner(stdout="inactive\n")
    assert status(runner, "acme:laravel-backend-dev") == AgentStatus.STOPPED


def test_status_activating_maps_to_starting() -> None:
    """Regression test: a unit crash-looping under Restart=on-failure spends

    real time in "activating" between restart attempts — this used to fall
    through to UNKNOWN, hiding an actively-failing agent behind a status
    that reads as "nothing to see here" (real incident: an agent whose exec
    target didn't exist crash-looped 700+ times reporting "unknown").
    """
    runner = FakeRunner(stdout="activating\n")
    assert status(runner, "acme:laravel-backend-dev") == AgentStatus.STARTING


def test_status_reloading_maps_to_starting() -> None:
    runner = FakeRunner(stdout="reloading\n")
    assert status(runner, "acme:laravel-backend-dev") == AgentStatus.STARTING


def test_status_deactivating_maps_to_stopped() -> None:
    runner = FakeRunner(stdout="deactivating\n")
    assert status(runner, "acme:laravel-backend-dev") == AgentStatus.STOPPED


def test_status_genuinely_unrecognized_output_maps_to_unknown() -> None:
    runner = FakeRunner(stdout="\n")
    assert status(runner, "acme:laravel-backend-dev") == AgentStatus.UNKNOWN


def test_tail_logs_returns_stdout() -> None:
    runner = FakeRunner(stdout="log line 1\nlog line 2\n")
    output = tail_logs(runner, "acme:laravel-backend-dev", lines=50)
    assert output == "log line 1\nlog line 2\n"
    assert runner.calls == [["journalctl", "--user", "-u", "buzz-agent@acme:laravel-backend-dev.service", "-n", "50", "--no-pager"]]


# Regression tests for Fix 5(a): a failed systemctl call must not be silently
# swallowed — create_agent would otherwise report success even when the unit
# never actually started.


def test_enable_now_raises_on_nonzero_returncode() -> None:
    runner = FakeRunner(returncode=1)
    with pytest.raises(RuntimeError):
        enable_now(runner, "acme:laravel-backend-dev")


def test_disable_now_raises_on_nonzero_returncode() -> None:
    runner = FakeRunner(returncode=1)
    with pytest.raises(RuntimeError):
        disable_now(runner, "acme:laravel-backend-dev")


def test_restart_raises_on_nonzero_returncode() -> None:
    runner = FakeRunner(returncode=1)
    with pytest.raises(RuntimeError):
        restart(runner, "acme:laravel-backend-dev")


def test_stop_raises_on_nonzero_returncode() -> None:
    runner = FakeRunner(returncode=1)
    with pytest.raises(RuntimeError):
        stop(runner, "acme:laravel-backend-dev")


# Task 4 regression tests: agent ids are unique only within a community, but
# unit names are global — before instance keys, two communities each with a
# "reviewer" agent addressed the same buzz-agent@reviewer.service unit.

from buzz_fleet import systemctl_client, units


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, args):
        import subprocess

        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="active", stderr="")


def test_unit_name_is_community_qualified() -> None:
    runner = RecordingRunner()
    systemctl_client.restart(runner, units.instance_key("eltahir", "reviewer"))
    assert runner.calls == [
        ["systemctl", "--user", "restart", "buzz-agent@eltahir:reviewer.service"]
    ]


def test_two_communities_address_different_units() -> None:
    runner = RecordingRunner()
    systemctl_client.stop(runner, units.instance_key("eltahir", "reviewer"))
    systemctl_client.stop(runner, units.instance_key("acme", "reviewer"))
    assert runner.calls[0] != runner.calls[1]


def test_status_reads_the_qualified_unit() -> None:
    runner = RecordingRunner()
    assert systemctl_client.status(runner, "eltahir:reviewer") is systemctl_client.AgentStatus.RUNNING
    assert runner.calls[0][-1] == "buzz-agent@eltahir:reviewer.service"


def test_tail_logs_reads_the_qualified_unit() -> None:
    runner = RecordingRunner()
    systemctl_client.tail_logs(runner, "eltahir:reviewer", lines=10)
    assert "buzz-agent@eltahir:reviewer.service" in runner.calls[0]


def test_unqualified_agent_id_is_refused() -> None:
    """The exact pre-migration bug: a bare agent id reaching a unit call
    would silently address whichever community's unit happened to share
    that agent id. Must fail loudly instead.
    """
    runner = RecordingRunner()
    with pytest.raises(ValueError):
        systemctl_client.restart(runner, "reviewer")
