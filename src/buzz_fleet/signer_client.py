"""Thin wrapper over the buzz-fleet-signer binary — the only Nostr key/event code path."""

from __future__ import annotations

import json

from buzz_fleet.proc import CommandRunner

BINARY = "buzz-fleet-signer"


def generate_key(runner: CommandRunner) -> tuple[str, str]:
    result = runner.run([BINARY, "generate-key"])
    data = json.loads(result.stdout)
    return data["public_key"], data["secret_key"]


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
