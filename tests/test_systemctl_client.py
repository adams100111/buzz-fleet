import subprocess

from buzz_fleet.systemctl_client import AgentStatus, enable_now, restart, status, tail_logs


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
    enable_now(runner, "laravel-backend-dev")
    assert runner.calls == [["systemctl", "--user", "enable", "--now", "buzz-agent@laravel-backend-dev"]]


def test_restart_invokes_systemctl_restart() -> None:
    runner = FakeRunner()
    restart(runner, "laravel-backend-dev")
    assert runner.calls == [["systemctl", "--user", "restart", "buzz-agent@laravel-backend-dev"]]


def test_status_active_maps_to_running() -> None:
    runner = FakeRunner(stdout="active\n")
    assert status(runner, "laravel-backend-dev") == AgentStatus.RUNNING


def test_status_failed_maps_to_failed() -> None:
    runner = FakeRunner(stdout="failed\n")
    assert status(runner, "laravel-backend-dev") == AgentStatus.FAILED


def test_status_inactive_maps_to_stopped() -> None:
    runner = FakeRunner(stdout="inactive\n")
    assert status(runner, "laravel-backend-dev") == AgentStatus.STOPPED


def test_tail_logs_returns_stdout() -> None:
    runner = FakeRunner(stdout="log line 1\nlog line 2\n")
    output = tail_logs(runner, "laravel-backend-dev", lines=50)
    assert output == "log line 1\nlog line 2\n"
    assert runner.calls == [["journalctl", "--user", "-u", "buzz-agent@laravel-backend-dev", "-n", "50", "--no-pager"]]
