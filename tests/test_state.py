import stat
from pathlib import Path

from buzz_fleet.models import Community
from buzz_fleet.state import load_community, save_community


def test_save_and_load_community_round_trips(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    community = Community(id="eltahir", relay_url="wss://buzz.eltahir.me", relay_admin_nsec="nsec1abc")

    save_community(community)
    loaded = load_community("eltahir")

    assert loaded is not None
    assert loaded.relay_url == "wss://buzz.eltahir.me"
    assert loaded.relay_admin_nsec.get_secret_value() == "nsec1abc"


def test_saved_community_file_is_mode_0600(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    save_community(Community(id="eltahir", relay_url="wss://buzz.eltahir.me", relay_admin_nsec="nsec1abc"))

    path = tmp_path / "communities" / "eltahir.json"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
