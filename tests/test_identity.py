import json
import subprocess

import pytest

from buzz_fleet import state
from buzz_fleet.models import Community
from buzz_fleet.orchestration.identity import resolve_identity
from buzz_fleet.orchestration.record import FleetRecord

CH, RK = "6f1c0000-0000-4000-8000-000000000000", "r" * 64


class FakeRunner:
    def run(self, args):
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"ok": True, "public_key": "b" * 64}), stderr="")


def test_agent_identity_from_env() -> None:
    env = {"BUZZ_PRIVATE_KEY": "nsec1agent", "BUZZ_RELAY_URL": "wss://r", "BUZZ_AUTH_TAG": '["auth"]',
           "BUZZ_FLEET_CHANNEL": CH, "BUZZ_FLEET_RETRIEVAL_KEY": RK, "BUZZ_ACP_AGENT_OWNER": "0" * 64}
    ident = resolve_identity(env, FakeRunner(), community_id=None)
    assert (ident.nsec, ident.pubkey, ident.relay_url, ident.auth_tag) == ("nsec1agent", "b" * 64, "wss://r", '["auth"]')
    assert (ident.fleet_channel, ident.retrieval_key, ident.owner_pubkey, ident.is_owner) == (CH, RK, "0" * 64, False)


def test_owner_identity_from_local_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_community(Community(id="e", relay_url="wss://r", relay_admin_nsec="nsec1admin", owner_pubkey="0" * 64,
                                   fleet_channel_id=CH, fleet_record=FleetRecord(retrieval_key=RK, created_at=1)))
    ident = resolve_identity({}, FakeRunner(), community_id=None)
    assert ident.is_owner and ident.pubkey == "0" * 64 and ident.retrieval_key == RK and ident.record is not None


def test_owner_identity_errors(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    with pytest.raises(RuntimeError, match="connect"):
        resolve_identity({}, FakeRunner(), community_id=None)
    for cid in ("a", "b"):
        state.save_community(Community(id=cid, relay_url="wss://r", relay_admin_nsec="nsec1x", owner_pubkey="0" * 64))
    with pytest.raises(RuntimeError, match="--community"):
        resolve_identity({}, FakeRunner(), community_id=None)
