"""Thin wrapper over the buzz-fleet-signer binary — the only Nostr key/event code path."""

from __future__ import annotations

import json
from pathlib import Path

from buzz_fleet.proc import CommandRunner

BINARY = "buzz-fleet-signer"


def generate_key(runner: CommandRunner) -> tuple[str, str]:
    result = runner.run([BINARY, "generate-key"])
    data = json.loads(result.stdout)
    return data["public_key"], data["secret_key"]


def pubkey_from_nsec(runner: CommandRunner, nsec: str) -> str:
    result = runner.run([BINARY, "pubkey-from-nsec", "--nsec", nsec])
    payload = json.loads(result.stdout)
    if not payload["ok"]:
        raise RuntimeError(f"pubkey-from-nsec failed: {payload.get('error')}")
    pubkey: str = payload["public_key"]
    return pubkey


def check_connection(runner: CommandRunner, relay_url: str, nsec: str) -> bool:
    result = runner.run([BINARY, "check-connection", "--relay", relay_url, "--nsec", nsec])
    ok: bool = json.loads(result.stdout)["ok"]
    return ok


def add_member(
    runner: CommandRunner,
    relay_url: str,
    admin_nsec: str,
    pubkey: str,
    role: str | None = None,
) -> None:
    args = [BINARY, "add-member", "--relay", relay_url, "--admin-nsec", admin_nsec, "--pubkey", pubkey]
    if role is not None:
        args += ["--role", role]
    result = runner.run(args)
    payload = json.loads(result.stdout)
    if not payload["ok"]:
        raise RuntimeError(f"add-member failed: {payload.get('error')}")


def remove_member(runner: CommandRunner, relay_url: str, admin_nsec: str, pubkey: str) -> None:
    args = [BINARY, "remove-member", "--relay", relay_url, "--admin-nsec", admin_nsec, "--pubkey", pubkey]
    result = runner.run(args)
    payload = json.loads(result.stdout)
    if not payload["ok"]:
        raise RuntimeError(f"remove-member failed: {payload.get('error')}")


def compute_auth_tag(runner: CommandRunner, owner_nsec: str, agent_pubkey: str) -> str:
    args = [BINARY, "compute-auth-tag", "--owner-nsec", owner_nsec, "--agent-pubkey", agent_pubkey]
    result = runner.run(args)
    payload = json.loads(result.stdout)
    if not payload["ok"]:
        raise RuntimeError(f"compute-auth-tag failed: {payload.get('error')}")
    auth_tag: str = payload["auth_tag"]
    return auth_tag


def publish_agent_profile(
    runner: CommandRunner, relay_url: str, agent_nsec: str, display_name: str, auth_tag: str
) -> None:
    args = [
        BINARY,
        "publish-agent-profile",
        "--relay",
        relay_url,
        "--agent-nsec",
        agent_nsec,
        "--display-name",
        display_name,
        "--auth-tag",
        auth_tag,
    ]
    result = runner.run(args)
    payload = json.loads(result.stdout)
    if not payload["ok"]:
        raise RuntimeError(f"publish-agent-profile failed: {payload.get('error')}")


def publish_managed_agent(
    runner: CommandRunner, relay_url: str, owner_nsec: str, agent_pubkey: str, content_file: Path
) -> None:
    args = [
        BINARY,
        "publish-managed-agent",
        "--relay",
        relay_url,
        "--owner-nsec",
        owner_nsec,
        "--agent-pubkey",
        agent_pubkey,
        "--content-file",
        str(content_file),
    ]
    result = runner.run(args)
    payload = json.loads(result.stdout)
    if not payload["ok"]:
        raise RuntimeError(f"publish-managed-agent failed: {payload.get('error')}")


def retract_managed_agent(runner: CommandRunner, relay_url: str, owner_nsec: str, agent_pubkey: str) -> None:
    args = [
        BINARY,
        "retract-managed-agent",
        "--relay",
        relay_url,
        "--owner-nsec",
        owner_nsec,
        "--agent-pubkey",
        agent_pubkey,
    ]
    result = runner.run(args)
    payload = json.loads(result.stdout)
    if not payload["ok"]:
        raise RuntimeError(f"retract-managed-agent failed: {payload.get('error')}")


def publish_agent_add_policy(
    runner: CommandRunner, relay_url: str, agent_nsec: str, policy: str, auth_tag: str
) -> None:
    args = [
        BINARY,
        "publish-agent-add-policy",
        "--relay",
        relay_url,
        "--agent-nsec",
        agent_nsec,
        "--policy",
        policy,
        "--auth-tag",
        auth_tag,
    ]
    result = runner.run(args)
    payload = json.loads(result.stdout)
    if not payload["ok"]:
        raise RuntimeError(f"publish-agent-add-policy failed: {payload.get('error')}")


def join_channel(runner: CommandRunner, relay_url: str, agent_nsec: str, channel_id: str, auth_tag: str) -> None:
    args = [
        BINARY,
        "join-channel",
        "--relay",
        relay_url,
        "--agent-nsec",
        agent_nsec,
        "--channel-id",
        channel_id,
        "--auth-tag",
        auth_tag,
    ]
    result = runner.run(args)
    payload = json.loads(result.stdout)
    if not payload["ok"]:
        raise RuntimeError(f"join-channel failed: {payload.get('error')}")


def leave_channel(runner: CommandRunner, relay_url: str, agent_nsec: str, channel_id: str, auth_tag: str) -> None:
    args = [
        BINARY,
        "leave-channel",
        "--relay",
        relay_url,
        "--agent-nsec",
        agent_nsec,
        "--channel-id",
        channel_id,
        "--auth-tag",
        auth_tag,
    ]
    result = runner.run(args)
    payload = json.loads(result.stdout)
    if not payload["ok"]:
        raise RuntimeError(f"leave-channel failed: {payload.get('error')}")


def archive_agent(
    runner: CommandRunner, relay_url: str, owner_nsec: str, agent_pubkey: str, reason: str, auth_tag: str
) -> None:
    args = [
        BINARY,
        "archive-agent",
        "--relay",
        relay_url,
        "--owner-nsec",
        owner_nsec,
        "--agent-pubkey",
        agent_pubkey,
        "--reason",
        reason,
        "--auth-tag",
        auth_tag,
    ]
    result = runner.run(args)
    payload = json.loads(result.stdout)
    if not payload["ok"]:
        raise RuntimeError(f"archive-agent failed: {payload.get('error')}")
