import pytest


@pytest.fixture(autouse=True)
def _isolated_buzz_fleet_config_dir(tmp_path, monkeypatch) -> None:
    """Every TUI test constructs a real `BuzzFleetApp()`, whose `on_mount()`

    calls `identity.resolve_community_id(os.environ, None)` (which in turn
    reads `state.list_community_ids()`/`state.load_active_community()`) to
    decide whether to auto-push a `DashboardScreen` — and
    `DashboardScreen.refresh_agents()`
    self-heals via a real `AgentManager` whenever a community IS connected,
    up to and including shelling out to real `systemctl`/`loginctl`/
    `buzz-fleet-signer`. On a dev machine that happens to have a real
    "eltahir" community already saved (this one does, from earlier manual
    testing), every test in this directory would silently act on it.

    Point `XDG_STATE_HOME` at an empty per-test directory by default — using
    the real `state.load_community` implementation throughout, never
    mocked — so `state.load_community(...)` genuinely, correctly returns
    None rather than reporting a fake value. Tests that want a specific
    saved community write one into this same `tmp_path` themselves (most
    already set `XDG_STATE_HOME` to their own `tmp_path` explicitly, which
    is the same object pytest hands this fixture, so the two compose
    without conflict).
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "buzz-fleet-state"))
