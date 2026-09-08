"""Read-only view of `config.toml`.

Nothing in `src/` writes this file. That is deliberate: the user is invited to
edit and comment it, and a round-trip through a TOML writer would destroy both
— and would need a dependency, since `tomllib` reads only. Anything the *app*
sets (the active community, for one) is machine state, not config, and lives
under `paths.state_dir()` instead.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass

from buzz_fleet import paths

_VIEWS = ("agents", "runs", "tasks")
_ENV_PREFIX = "env:"


@dataclass(frozen=True)
class Config:
    default_community: str | None = None
    refresh_interval_ms: int = 2000
    default_view: str = "agents"
    theme: str = "buzz-fleet"
    confirm_destructive: bool = True
    default_harness: str = "claude"
    ntfy_url: str | None = None
    ntfy_token: str | None = None
    herdr_report_agents: bool = False


def resolve_secret(raw: str | None) -> str | None:
    """Secret-typed fields must indirect through the environment.

    A literal is refused rather than accepted-with-a-warning: config.toml is a
    file the user is encouraged to edit and hand around, and a token pasted
    into it would be the one plaintext secret outside the secrets tree.
    """
    if raw is None:
        return None
    if not raw.startswith(_ENV_PREFIX):
        raise ValueError(f"secret config values must use env:NAME, got {raw!r}")
    return os.environ.get(raw[len(_ENV_PREFIX) :]) or None


def load() -> Config:
    path = paths.config_dir() / "config.toml"
    if not path.exists():
        return Config()
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path} is not valid TOML: {e}") from e

    general = raw.get("general", {})
    ui = raw.get("ui", {})
    defaults = raw.get("defaults", {})
    notifier = raw.get("notifier", {})
    herdr = raw.get("herdr", {})

    view = ui.get("default_view", "agents")
    if view not in _VIEWS:
        raise ValueError(f"[ui] default_view must be one of {', '.join(_VIEWS)}, got {view!r}")

    return Config(
        default_community=general.get("default_community"),
        refresh_interval_ms=int(ui.get("refresh_interval_ms", 2000)),
        default_view=view,
        theme=ui.get("theme", "buzz-fleet"),
        confirm_destructive=bool(ui.get("confirm_destructive", True)),
        default_harness=defaults.get("harness", "claude"),
        ntfy_url=notifier.get("ntfy_url"),
        ntfy_token=resolve_secret(notifier.get("ntfy_token")),
        herdr_report_agents=bool(herdr.get("report_agents", False)),
    )
