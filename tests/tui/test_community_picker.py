"""The picker writes the same active-community pointer that
`buzz-fleet community use` writes. One source of truth, two front doors."""

from unittest.mock import MagicMock

from pydantic import SecretStr

from buzz_fleet import paths, state
from buzz_fleet.models import Community
from buzz_fleet.tui.app import BuzzFleetApp
from buzz_fleet.tui.screens.community_picker import CommunityPickerScreen


def _connect(monkeypatch, tmp_path, cid: str) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    state.save_community(
        Community(id=cid, relay_url=f"wss://{cid}.example", relay_admin_nsec=SecretStr("nsec1x"))
    )
    # BuzzFleetApp.on_mount auto-pushes a DashboardScreen underneath the
    # picker the moment any community is resolvable, and
    # DashboardScreen.refresh_agents() calls a real
    # AgentManager(RealCommandRunner(), community).ensure_runtime_ready() --
    # unmocked, that shells out to the real systemctl/loginctl. This test
    # only exercises the picker itself (list/mark/choose/cancel), never the
    # dashboard's self-healing, so it's mocked out exactly the way
    # tests/tui/test_dashboard.py and tests/tui/test_connect.py already do
    # for the same reason.
    monkeypatch.setattr(
        "buzz_fleet.tui.screens.dashboard.AgentManager", lambda runner, community: MagicMock()
    )


async def test_lists_every_community(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await app.push_screen(CommunityPickerScreen("eltahir"))
        await pilot.pause()
        table = app.screen.query_one("#community-table")
        shown = {str(table.get_row_at(row)[1]) for row in range(table.row_count)}
        assert shown == {"eltahir", "acme"}


async def test_marks_the_active_community(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await app.push_screen(CommunityPickerScreen("acme"))
        await pilot.pause()
        table = app.screen.query_one("#community-table")
        rows = {str(table.get_row_at(r)[1]): str(table.get_row_at(r)[0]) for r in range(table.row_count)}
        assert rows["acme"].strip() == "*"
        assert rows["eltahir"].strip() == ""


async def test_choosing_saves_the_active_community(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    state.save_active_community("eltahir")
    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await app.push_screen(CommunityPickerScreen("eltahir"))
        await pilot.pause()
        table = app.screen.query_one("#community-table")
        target = next(r for r in range(table.row_count) if str(table.get_row_at(r)[1]) == "acme")
        table.move_cursor(row=target)
        await pilot.press("enter")
        await pilot.pause()
    assert state.load_active_community() == "acme"


async def test_escape_cancels_without_changing_anything(monkeypatch, tmp_path) -> None:
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    state.save_active_community("eltahir")
    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await app.push_screen(CommunityPickerScreen("eltahir"))
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert state.load_active_community() == "eltahir"


async def test_corrupt_community_state_file_still_appears_and_is_selectable(
    monkeypatch, tmp_path
) -> None:
    # Genuine garbage, not valid JSON with a bad value -- load_community
    # raises json.JSONDecodeError (a ValueError) before it ever reaches
    # pydantic. The row must still show up, still be selectable, and
    # choosing it must still write the pointer -- exactly what a user whose
    # file got mangled needs in order to switch away from it.
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    (paths.state_dir() / "communities" / "acme.json").write_text("not valid json {{{")

    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await app.push_screen(CommunityPickerScreen("eltahir"))
        await pilot.pause()
        table = app.screen.query_one("#community-table")
        rows = {str(table.get_row_at(r)[1]): r for r in range(table.row_count)}
        assert rows.keys() == {"eltahir", "acme"}
        assert str(table.get_row_at(rows["acme"])[2]) == "<unreadable>"

        table.move_cursor(row=rows["acme"])
        await pilot.press("enter")
        await pilot.pause()

    assert state.load_active_community() == "acme"


async def test_corrupt_agent_file_shows_unreadable_agent_count(monkeypatch, tmp_path) -> None:
    # A corrupt agent file must degrade only its own community's agent
    # count, not take out the whole picker -- the community's own state is
    # fine, so its relay URL still renders normally.
    _connect(monkeypatch, tmp_path, "eltahir")
    _connect(monkeypatch, tmp_path, "acme")
    agents_dir = paths.state_dir() / "communities" / "acme" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "broken.json").write_text("not valid json {{{")

    app = BuzzFleetApp()
    async with app.run_test() as pilot:
        await app.push_screen(CommunityPickerScreen("eltahir"))
        await pilot.pause()
        table = app.screen.query_one("#community-table")
        rows = {str(table.get_row_at(r)[1]): r for r in range(table.row_count)}
        assert str(table.get_row_at(rows["acme"])[2]) == "wss://acme.example"
        assert str(table.get_row_at(rows["acme"])[3]) == "?"
