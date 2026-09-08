"""Validation of a community id at the point it's created.

Task 3's ruling: `Community.id` is unvalidated free-form operator input that
flows into a state file path, an advisory lock filename, and a systemd unit
instance name — a bad value (`..`, `/`, `:`, a space) must be refused here,
at `connect_and_save`, rather than failing late in one of those three
consumers (or, for `..`, escaping the state directory silently).
"""

from __future__ import annotations

import json
import subprocess

import pytest

from buzz_fleet import state
from buzz_fleet.connect import connect_and_save, validate_community_id


class FakeRunner:
    """Would-succeed relay connection — used to prove a rejected id never
    even reaches the network call, let alone disk."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[1:2] == ["pubkey-from-nsec"]:
            payload = {"ok": True, "public_key": "a" * 64}
        else:
            payload = {"ok": True}
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")


def test_valid_id_passes_validation() -> None:
    validate_community_id("eltahir")
    validate_community_id("Acme-2.local_test")


@pytest.mark.parametrize(
    "bad_id",
    ["..", "a/b", "a:b", "", ".hidden", "a b"],
)
def test_invalid_ids_are_refused(bad_id: str) -> None:
    with pytest.raises(ValueError):
        validate_community_id(bad_id)


@pytest.mark.parametrize(
    "bad_id",
    ["..", "a/b", "a:b", "", ".hidden", "a b"],
)
def test_connect_and_save_refuses_bad_id_and_writes_nothing(bad_id, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    runner = FakeRunner()

    with pytest.raises(ValueError):
        connect_and_save(runner, bad_id, "wss://buzz.eltahir.me", "nsec1abc")

    # The bad id never even reached the (network) relay check.
    assert runner.calls == []
    # Nothing landed anywhere under the state tree.
    assert list(tmp_path.rglob("*")) == []
    assert state.list_community_ids() == []
