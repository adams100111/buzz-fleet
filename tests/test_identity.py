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


"""Resolution order (spec 5.2). Steps 1, 2, 5 and 6 are today's behaviour and
must not change; 3 and 4 are what make a toggle possible."""

from buzz_fleet import paths
from buzz_fleet.orchestration import identity


def _community(monkeypatch, tmp_path, cid: str) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from pydantic import SecretStr

    from buzz_fleet.models import Community

    state.save_community(
        Community(id=cid, relay_url="wss://r", relay_admin_nsec=SecretStr("nsec1x"))
    )


def test_explicit_argument_wins(monkeypatch, tmp_path) -> None:
    _community(monkeypatch, tmp_path, "eltahir")
    _community(monkeypatch, tmp_path, "acme")
    state.save_active_community("acme")
    assert identity.resolve_community_id({"BUZZ_FLEET_COMMUNITY": "eltahir"}, "acme") == "acme"


def test_env_var_beats_active_and_config(monkeypatch, tmp_path) -> None:
    _community(monkeypatch, tmp_path, "eltahir")
    _community(monkeypatch, tmp_path, "acme")
    state.save_active_community("acme")
    assert identity.resolve_community_id({"BUZZ_FLEET_COMMUNITY": "eltahir"}, None) == "eltahir"


def test_active_community_beats_config_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    cfg = paths.config_dir() / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('[general]\ndefault_community = "eltahir"\n')
    _community(monkeypatch, tmp_path, "eltahir")
    _community(monkeypatch, tmp_path, "acme")
    state.save_active_community("acme")
    assert identity.resolve_community_id({}, None) == "acme"


def test_config_default_used_when_nothing_active(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    cfg = paths.config_dir() / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('[general]\ndefault_community = "eltahir"\n')
    _community(monkeypatch, tmp_path, "eltahir")
    _community(monkeypatch, tmp_path, "acme")
    assert identity.resolve_community_id({}, None) == "eltahir"


def test_single_community_needs_no_pointer(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    _community(monkeypatch, tmp_path, "eltahir")
    assert identity.resolve_community_id({}, None) == "eltahir"


def test_several_communities_and_no_pointer_is_an_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    _community(monkeypatch, tmp_path, "eltahir")
    _community(monkeypatch, tmp_path, "acme")
    with pytest.raises(RuntimeError, match="acme, eltahir"):
        identity.resolve_community_id({}, None)


def test_no_community_at_all_is_an_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    with pytest.raises(RuntimeError, match="buzz-fleet connect"):
        identity.resolve_community_id({}, None)


def test_a_stale_pointer_is_ignored(monkeypatch, tmp_path) -> None:
    """A community deleted while it was active must not wedge every command."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    _community(monkeypatch, tmp_path, "eltahir")
    state.save_active_community("deleted-one")
    assert identity.resolve_community_id({}, None) == "eltahir"
