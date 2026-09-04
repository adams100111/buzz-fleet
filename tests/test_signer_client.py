import json
import subprocess

from buzz_fleet.signer_client import add_member, check_connection, generate_key


class FakeRunner:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        return subprocess.CompletedProcess(args, self.returncode, stdout=self.stdout, stderr="")


def test_generate_key_parses_json_output() -> None:
    runner = FakeRunner(json.dumps({"public_key": "ab" * 32, "secret_key": "nsec1xyz"}))

    public_key, secret_key = generate_key(runner)

    assert public_key == "ab" * 32
    assert secret_key == "nsec1xyz"
    assert runner.calls == [["buzz-fleet-signer", "generate-key"]]


def test_check_connection_true_on_ok() -> None:
    runner = FakeRunner(json.dumps({"ok": True}))
    assert check_connection(runner, "wss://relay.example", "nsec1abc") is True


def test_check_connection_false_on_failure_exit_code() -> None:
    runner = FakeRunner(json.dumps({"ok": False, "error": "bad key"}), returncode=1)
    assert check_connection(runner, "wss://relay.example", "nsec1bad") is False


def test_add_member_passes_role_flag_when_given() -> None:
    runner = FakeRunner(json.dumps({"ok": True}))
    add_member(runner, "wss://relay.example", "nsec1admin", "cd" * 32, role="admin")
    assert runner.calls == [
        [
            "buzz-fleet-signer",
            "add-member",
            "--relay",
            "wss://relay.example",
            "--admin-nsec",
            "nsec1admin",
            "--pubkey",
            "cd" * 32,
            "--role",
            "admin",
        ]
    ]
