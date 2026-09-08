"""BuzzFleetApp — the Textual application shell."""

from __future__ import annotations

import os

from textual.app import App

from buzz_fleet import state
from buzz_fleet.orchestration.identity import resolve_community_id
from buzz_fleet.tui.screens.connect import ConnectScreen
from buzz_fleet.tui.screens.dashboard import DashboardScreen
from buzz_fleet.tui.theme import BUZZ_FLEET_THEME


class BuzzFleetApp(App):
    def on_mount(self) -> None:
        self.register_theme(BUZZ_FLEET_THEME)
        self.theme = "buzz-fleet"
        try:
            community_id = resolve_community_id(os.environ, None)
        except (RuntimeError, ValueError) as e:
            # RuntimeError: no community yet, or several with nothing
            # selected. ValueError: config.toml itself is malformed —
            # config.load() (called inside resolve_community_id) raises on
            # bad TOML, an invalid enum value, or a non-bool where one is
            # required, and this is the first thing in the TUI that ever
            # reads config.toml at all, so a typo there must not crash the
            # app at mount. Connecting is the answer to "no community yet";
            # the picker (Task 10) is the answer to "several, none active";
            # either way, showing *why* beats a silently blank "Connect a
            # community" screen.
            self.notify(str(e), severity="warning")
            self.push_screen(ConnectScreen())
            return
        if state.load_community(community_id) is None:
            # A pointer (an explicit BUZZ_FLEET_COMMUNITY, the persisted
            # active-community file, or config.toml's default) can name a
            # community that no longer exists locally. resolve_community_id
            # deliberately does not existence-check a stated intent like
            # BUZZ_FLEET_COMMUNITY itself (see its docstring) — so that check
            # belongs here, on the resolved id, before it's handed to
            # DashboardScreen. Without it, a stale pointer would silently
            # push an empty, broken-looking dashboard instead of a clear way
            # to reconnect.
            self.notify(
                f"'{community_id}' is no longer a local community; connect it again.",
                severity="warning",
            )
            self.push_screen(ConnectScreen())
            return
        self.push_screen(DashboardScreen(community_id))
