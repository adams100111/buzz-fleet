import json
import subprocess
from pathlib import Path

import pytest

from buzz_fleet.signer_client import (
    add_member,
    archive_agent,
    channel_members,
    check_connection,
    compute_auth_tag,
    create_channel,
    generate_key,
    join_channel,
    leave_channel,
    post_message,
    publish_agent_add_policy,
    publish_agent_profile,
    publish_managed_agent,
    query,
    read_channel_meta,
    read_managed_agents,
    read_presence,
    retract_managed_agent,
    write_channel_about,
)

CH = "6f1c0000-0000-4000-8000-000000000000"


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


def test_post_message_argv_and_event_id() -> None:
    runner = FakeRunner(json.dumps({"ok": True, "event_id": "e" * 64}))
    event_id = post_message(runner, "wss://r", "nsec1a", CH, "hello", mentions=["b" * 64], root="c" * 64,
                            parent="d" * 64, tags=[("t", "fleet"), ("fleet", '{"type":"x"}')], auth_tag='["auth","a","","b"]')
    assert event_id == "e" * 64
    assert runner.calls == [[
        "buzz-fleet-signer", "post-message", "--relay", "wss://r", "--nsec", "nsec1a", "--auth-tag", '["auth","a","","b"]',
        "--channel", CH, "--content", "hello", "--mention", "b" * 64, "--root", "c" * 64, "--parent", "d" * 64,
        "--tag", "t=fleet", "--tag", 'fleet={"type":"x"}',
    ]]


def test_post_message_raises_on_rejection() -> None:
    runner = FakeRunner(json.dumps({"ok": False, "error": "restricted"}), returncode=1)
    with pytest.raises(RuntimeError, match="restricted"):
        post_message(runner, "wss://r", "nsec1a", CH, "x", mentions=[], root=None, parent=None, tags=[], auth_tag=None)


def test_query_parses_json_lines_and_errors() -> None:
    runner = FakeRunner(json.dumps({"id": "1", "kind": 9}) + "\n" + json.dumps({"id": "2", "kind": 9}) + "\n")
    events = query(runner, "wss://r", "nsec1a", {"kinds": [9], "#p": ["a" * 64]}, auth_tag=None)
    assert [e["id"] for e in events] == ["1", "2"]
    assert json.loads(runner.calls[0][runner.calls[0].index("--filter") + 1]) == {"kinds": [9], "#p": ["a" * 64]}

    runner = FakeRunner(json.dumps({"ok": False, "error": "closed"}) + "\n", returncode=2)
    with pytest.raises(RuntimeError, match="closed"):
        query(runner, "wss://r", "nsec1a", {"kinds": [9]}, auth_tag=None)


def test_channel_members_and_meta_and_about() -> None:
    runner = FakeRunner(json.dumps({"ok": True, "members": [{"pubkey": "a" * 64, "display_name": "Reviewer"},
                                                          {"pubkey": "b" * 64, "display_name": None}]}))
    assert channel_members(runner, "wss://r", "nsec1a", CH, auth_tag=None) == [("a" * 64, "Reviewer"), ("b" * 64, None)]

    runner = FakeRunner(json.dumps({"ok": True, "channels": [{"channel_id": CH, "name": "fleet", "about": "x", "archived": False}]}))
    assert read_channel_meta(runner, "wss://r", "nsec1a", channel_id=None, auth_tag=None)[0]["name"] == "fleet"
    assert "--channel" not in runner.calls[0]

    runner = FakeRunner(json.dumps({"ok": True}))
    write_channel_about(runner, "wss://r", "nsec1owner", CH, "line\n{}")
    assert runner.calls == [["buzz-fleet-signer", "write-channel-about", "--relay", "wss://r", "--owner-nsec", "nsec1owner",
                             "--channel", CH, "--about", "line\n{}"]]


def test_create_channel() -> None:
    runner = FakeRunner(json.dumps({"ok": True, "channel_id": CH}))
    assert create_channel(runner, "wss://r", "nsec1owner", "fleet", about="Fleet") == CH
    assert runner.calls[0][-4:] == ["--name", "fleet", "--about", "Fleet"]


def test_read_managed_agents_argv_and_parsing() -> None:
    content = {"role": "reviewer", "capabilities": ["laravel"], "harness": "claude", "version": "0.8.0"}
    runner = FakeRunner(json.dumps({"ok": True, "agents": [{"pubkey": "b" * 64, "content": content}]}))
    agents = read_managed_agents(runner, "wss://r", "nsec1a", owner="a" * 64, auth_tag=None)
    assert agents == [{"pubkey": "b" * 64, "content": content}]
    assert runner.calls == [["buzz-fleet-signer", "read-managed-agents", "--relay", "wss://r", "--nsec", "nsec1a",
                             "--owner", "a" * 64]]


def test_read_managed_agents_passes_auth_tag() -> None:
    runner = FakeRunner(json.dumps({"ok": True, "agents": []}))
    read_managed_agents(runner, "wss://r", "nsec1a", owner="a" * 64, auth_tag='["auth","a","","b"]')
    assert "--auth-tag" in runner.calls[0] and '["auth","a","","b"]' in runner.calls[0]


def test_read_managed_agents_raises_on_error() -> None:
    runner = FakeRunner(json.dumps({"ok": False, "error": "restricted"}), returncode=1)
    with pytest.raises(RuntimeError, match="restricted"):
        read_managed_agents(runner, "wss://r", "nsec1a", owner="a" * 64, auth_tag=None)


def test_read_presence_argv_and_parsing() -> None:
    runner = FakeRunner(json.dumps({"ok": True, "presence": [{"pubkey": "b" * 64, "status": "online", "updated_at": 1700}]}))
    presence = read_presence(runner, "wss://r", "nsec1a", pubkeys=["b" * 64, "c" * 64], auth_tag=None)
    assert presence == [{"pubkey": "b" * 64, "status": "online", "updated_at": 1700}]
    assert runner.calls == [["buzz-fleet-signer", "read-presence", "--relay", "wss://r", "--nsec", "nsec1a",
                             "--pubkey", "b" * 64, "--pubkey", "c" * 64]]


def test_read_presence_passes_auth_tag_and_empty_pubkeys() -> None:
    runner = FakeRunner(json.dumps({"ok": True, "presence": []}))
    assert read_presence(runner, "wss://r", "nsec1a", pubkeys=[], auth_tag='["auth","a","","b"]') == []
    assert runner.calls == [["buzz-fleet-signer", "read-presence", "--relay", "wss://r", "--nsec", "nsec1a", "--auth-tag",
                             '["auth","a","","b"]']]


def test_read_presence_raises_on_error() -> None:
    runner = FakeRunner(json.dumps({"ok": False, "error": "restricted"}), returncode=1)
    with pytest.raises(RuntimeError, match="restricted"):
        read_presence(runner, "wss://r", "nsec1a", pubkeys=["b" * 64], auth_tag=None)
