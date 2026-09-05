import json
import subprocess
from pathlib import Path

from buzz_fleet.signer_client import (
    add_member,
    archive_agent,
    check_connection,
    compute_auth_tag,
    generate_key,
    join_channel,
    leave_channel,
    publish_agent_add_policy,
    publish_agent_profile,
    publish_managed_agent,
    retract_managed_agent,
)


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


def test_compute_auth_tag_returns_the_tag_string() -> None:
    runner = FakeRunner(json.dumps({"ok": True, "auth_tag": '["auth","a","",  "b"]'}))
    assert compute_auth_tag(runner, "nsec1owner", "c" * 64) == '["auth","a","",  "b"]'
    assert runner.calls == [
        ["buzz-fleet-signer", "compute-auth-tag", "--owner-nsec", "nsec1owner", "--agent-pubkey", "c" * 64]
    ]


def test_publish_agent_profile_passes_all_flags() -> None:
    runner = FakeRunner(json.dumps({"ok": True}))
    publish_agent_profile(runner, "wss://r", "nsec1agent", "Display Name", '["auth","a","","b"]')
    assert runner.calls == [
        [
            "buzz-fleet-signer",
            "publish-agent-profile",
            "--relay",
            "wss://r",
            "--agent-nsec",
            "nsec1agent",
            "--display-name",
            "Display Name",
            "--auth-tag",
            '["auth","a","","b"]',
        ]
    ]


def test_publish_managed_agent_passes_content_file_path() -> None:
    runner = FakeRunner(json.dumps({"ok": True}))
    publish_managed_agent(runner, "wss://r", "nsec1owner", "c" * 64, Path("/tmp/content.json"))
    assert runner.calls == [
        [
            "buzz-fleet-signer",
            "publish-managed-agent",
            "--relay",
            "wss://r",
            "--owner-nsec",
            "nsec1owner",
            "--agent-pubkey",
            "c" * 64,
            "--content-file",
            "/tmp/content.json",
        ]
    ]


def test_retract_managed_agent_raises_on_failure() -> None:
    runner = FakeRunner(json.dumps({"ok": False, "error": "invalid: not found"}))
    try:
        retract_managed_agent(runner, "wss://r", "nsec1owner", "c" * 64)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "invalid: not found" in str(e)


def test_publish_agent_add_policy_passes_policy() -> None:
    runner = FakeRunner(json.dumps({"ok": True}))
    publish_agent_add_policy(runner, "wss://r", "nsec1agent", "owner_only", '["auth","a","","b"]')
    assert "--policy" in runner.calls[0] and "owner_only" in runner.calls[0]
    assert "--auth-tag" in runner.calls[0]


def test_join_channel_and_leave_channel_pass_channel_id_and_auth_tag() -> None:
    runner = FakeRunner(json.dumps({"ok": True}))
    join_channel(
        runner, "wss://r", "nsec1agent", "11111111-1111-1111-1111-111111111111", '["auth","a","","b"]'
    )
    leave_channel(
        runner, "wss://r", "nsec1agent", "11111111-1111-1111-1111-111111111111", '["auth","a","","b"]'
    )
    assert runner.calls[0][1] == "join-channel"
    assert runner.calls[1][1] == "leave-channel"
    assert "--auth-tag" in runner.calls[0] and "--auth-tag" in runner.calls[1]


def test_archive_agent_passes_owner_nsec_and_reason() -> None:
    runner = FakeRunner(json.dumps({"ok": True}))
    archive_agent(runner, "wss://r", "nsec1owner", "c" * 64, "retired", '["auth","a","","b"]')
    call = runner.calls[0]
    assert "--owner-nsec" in call and "nsec1owner" in call
    assert "--reason" in call and "retired" in call
