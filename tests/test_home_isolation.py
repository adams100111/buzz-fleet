"""Regression coverage for the real incident `conftest.py`'s `_isolate_real_home`
closes — see that fixture's docstring for the full incident.

Deliberately outside `tests/tui/`: the vulnerable call chain (`AgentManager.
ensure_runtime_ready()` -> `systemd.ensure_template_unit_installed()`) has
nothing to do with the TUI itself, and driving it through `AgentManager`
directly here, with a `FakeRunner` and a pre-seeded `buzz-acp` stub (matching
`tests/test_manager.py`'s own established pattern for the same reason —
without it, this test would attempt a REAL network download), gives a fast,
network-free, subprocess-free regression test for the exact path that
escaped isolation.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from buzz_fleet import buzz_acp, systemd
from buzz_fleet.manager import AgentManager
from buzz_fleet.models import Community


class FakeRunner:
    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout="inactive", stderr="")


def _community() -> Community:
    # owner_pubkey set explicitly so `ensure_runtime_ready` skips
    # `_ensure_owner_pubkey`'s `signer_client.pubkey_from_nsec` call --
    # this test's `FakeRunner` only needs to answer the systemctl/loginctl
    # calls the vulnerable chain actually makes, not buzz-fleet-signer's.
    return Community(
        id="eltahir",
        relay_url="wss://buzz.eltahir.me",
        relay_admin_nsec="nsec1admin",
        owner_pubkey="a" * 64,
    )


def test_ensure_runtime_ready_never_touches_the_real_home(tmp_path, monkeypatch) -> None:
    """Drives the exact chain the real incident took —
    `AgentManager.ensure_runtime_ready()` -> `ensure_template_unit_installed()`
    -- and proves the write lands under THIS test's isolated home, not a real
    one. `conftest.py`'s repo-wide `_isolate_real_home` fixture is what makes
    this true with no per-test patch of its own; a test that only sets
    `XDG_*_HOME` (as the real incident's test did) would still fail this,
    since `template_unit_path()` never consults those.
    """
    fake_home = Path.home()  # already patched by conftest.py's autouse fixture

    acp_dir = tmp_path / "acp"
    acp_dir.mkdir()
    stub = acp_dir / "buzz-acp"
    stub.write_bytes(b"stub")
    stub.chmod(0o755)
    monkeypatch.setattr(buzz_acp, "buzz_acp_dir", lambda: acp_dir)
    monkeypatch.setattr(buzz_acp, "buzz_acp_path", lambda: stub)
    monkeypatch.setattr(buzz_acp, "buzz_cli_path", lambda: acp_dir / "buzz")

    AgentManager(FakeRunner(), _community()).ensure_runtime_ready()

    # `os.path.expanduser` is not monkeypatched by conftest.py's fixture (only
    # `Path.home` is), so this is the ACTUAL real home directory -- proving
    # the fake one is genuinely different, not just "a Path object".
    real_home = Path(os.path.expanduser("~"))
    assert fake_home != real_home

    template_path = systemd.template_unit_path()
    assert template_path.is_relative_to(fake_home)
    assert not template_path.is_relative_to(real_home)
    assert template_path.exists()
    assert "EnvironmentFile=" in template_path.read_text()


def test_a_tests_own_home_patch_overrides_the_global_fixture(tmp_path, monkeypatch) -> None:
    """The composition guarantee FIX 0 depends on: a test that patches
    `Path.home` itself (as `tests/test_migrate.py`'s `_legacy_tree` and
    `tests/test_paths.py`'s fallback tests do) must win over the repo-wide
    default, not fight it.
    """
    default_home = Path.home()  # from conftest.py's autouse fixture
    own_home = tmp_path / "own-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: own_home))
    assert Path.home() == own_home
    assert Path.home() != default_home
