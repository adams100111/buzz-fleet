"""Every location buzz-fleet owns, resolved once.

Before this module, six others each built their own
`Path.home() / ".config" / "buzz-fleet"` and none of them honoured XDG. These
are functions rather than module constants so a test can drive them with
`monkeypatch.setenv` and get a different answer on the next call — patching a
constant only works if every caller re-reads it, which they didn't.
"""

from __future__ import annotations

import os
from pathlib import Path

APP = "buzz-fleet"


def _xdg(var: str, fallback: Path) -> Path:
    raw = os.environ.get(var)
    return (Path(raw) if raw else fallback) / APP


def config_dir() -> Path:
    """User-editable configuration. `config.toml` and `personas/`."""
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config")


def state_dir() -> Path:
    """Machine-owned state. Communities, agents, the active-community pointer."""
    return _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state")


def data_dir() -> Path:
    """Installed artefacts and working directories: bin/, work/, templates."""
    return _xdg("XDG_DATA_HOME", Path.home() / ".local" / "share")


def runtime_dir() -> Path:
    """Sockets and pidfiles. Unlike the others, XDG_RUNTIME_DIR is genuinely
    absent in non-login contexts, so this falls back inside state_dir()."""
    raw = os.environ.get("XDG_RUNTIME_DIR")
    return Path(raw) / APP if raw else state_dir() / "run"


def secrets_dir() -> Path:
    """Key material only, mirroring the state tree's shape. Mode 0700."""
    return state_dir() / "secrets"


def legacy_dir() -> Path:
    """The pre-migration tree. Only `migrate` may read this; it names where
    files are on an already-installed machine, so it ignores XDG on purpose."""
    return Path.home() / ".config" / APP
