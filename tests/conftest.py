"""Repo-wide test isolation for the one path that lives outside buzz-fleet's
own XDG-aware tree.

`systemd.template_unit_path()` is deliberately `Path.home() / ".config" /
"systemd" / "user" / "buzz-agent@.service"` — it lives in systemd's own
namespace, not buzz-fleet's, so it does not (and should not) go through
paths.py's XDG_*_HOME-aware functions. That means isolating XDG_STATE_HOME/
XDG_CONFIG_HOME/XDG_DATA_HOME alone, however carefully, does nothing to stop
a test that reaches `ensure_template_unit_installed` from writing into the
developer's REAL `~/.config/systemd/user/buzz-agent@.service`.

Real incident: `tests/tui/test_connect.py::
test_connecting_a_second_community_adds_rather_than_overwrites` drove
`connect_and_save` -> `DashboardScreen.refresh_agents` -> a real
`AgentManager(RealCommandRunner(), community).ensure_runtime_ready()` ->
`ensure_template_unit_installed`, which overwrote this machine's live
template unit with `EnvironmentFile=<pytest tmp_path>/.../%i.env`.
`EnvironmentFile=` with no leading `-` is fatal if the file is missing, so
all three of this machine's real agents would have failed their next
restart. The file was repaired by hand; this fixture is what stops it from
happening again.

Autouse and placed here (repo root), not as a per-file patch in
`tests/tui/conftest.py`: the hole isn't specific to that one test or that one
directory — ANY test anywhere that reaches `ensure_runtime_ready`/
`ensure_template_unit_installed` without patching `Path.home` itself has the
exact same exposure, today or in a test not yet written. Patches `Path.home`
alongside every `XDG_*_HOME` var so both halves of "where does buzz-fleet
read/write" are covered by one fixture, for every test in the suite.

Composes with a test's own `Path.home` patch (`tests/test_migrate.py`'s
`_legacy_tree`, `tests/test_paths.py`'s fallback tests): this fixture's
`monkeypatch.setattr` runs during fixture setup, before the test body runs,
so a test that calls `monkeypatch.setattr(Path, "home", ...)` itself simply
overwrites it again for the rest of that test — both go through the same
per-test `monkeypatch` fixture instance (pytest caches it once per test), so
teardown unwinds correctly either way. See `tests/test_home_isolation.py`
for the direct proof of both halves of this (the real incident's exact call
chain closed, and the composition guarantee).

Deliberately NOT nested under the requesting test's own `tmp_path`: several
existing tests assert `tmp_path` holds exactly (and only) what THAT test
itself wrote (`test_atomic.py`'s `[p.name for p in tmp_path.iterdir()] ==
[...]`, `test_connect_validation.py`'s `list(tmp_path.rglob("*")) == []`).
Creating this fixture's own directories inside that same `tmp_path` made
those assertions fail — a real regression this fixture caused when it first
used `tmp_path` directly. `tmp_path_factory.mktemp` gives each of these its
own unrelated temp directory instead, so a test's `tmp_path` reflects only
that test's own writes, exactly as before this fixture existed.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_real_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_home = tmp_path_factory.mktemp("fake-home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("xdg-state")))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg-config")))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path_factory.mktemp("xdg-data")))
    # Genuinely absent in some real, non-login contexts (paths.runtime_dir()
    # falls back to state_dir()/"run" for exactly that reason) — deleting it
    # here means a test isn't accidentally isolated-or-not depending on
    # whether the developer's own shell happens to export it.
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
