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
from dataclasses import dataclass, field
from pathlib import Path

from buzz_fleet import harnesses, paths

_VIEWS = ("agents", "runs", "tasks")
_ENV_PREFIX = "env:"

# A refresh interval of zero or less would spin the UI loop; this is a floor,
# not a recommendation.
_MIN_REFRESH_INTERVAL_MS = 100

# The recognised shape of config.toml, section -> known keys. Anything outside
# this shape is reported (Config.unknown_keys), never rejected — the fleet
# spans machines with acknowledged version drift, and a config key a newer
# binary understands but an older one doesn't must stay forward-compatible
# rather than bricking the older machine mid-upgrade.
_KNOWN_SECTIONS: dict[str, set[str]] = {
    "general": {"default_community"},
    "ui": {"refresh_interval_ms", "default_view", "theme", "confirm_destructive"},
    "defaults": {"harness"},
    "notifier": {"ntfy_url", "ntfy_token"},
    "herdr": {"report_agents"},
}


@dataclass(frozen=True)
class Config:
    default_community: str | None = None
    refresh_interval_ms: int = 2000
    default_view: str = "agents"
    theme: str = "buzz-fleet"
    confirm_destructive: bool = True
    default_harness: str = "claude"
    ntfy_url: str | None = None
    # repr=False: this is the resolved secret value (post env: indirection),
    # not the "env:NAME" reference stored in config.toml -- Phase B's
    # JSON-RPC daemon will serialise Config objects, and nothing today stops
    # a log line or an error message from carrying a whole Config via its
    # default, auto-generated __repr__.
    ntfy_token: str | None = field(default=None, repr=False)
    herdr_report_agents: bool = False
    unknown_keys: tuple[str, ...] = ()


def resolve_secret(raw: str | None, *, field: str = "value") -> str | None:
    """Secret-typed fields must indirect through the environment.

    A literal is refused rather than accepted-with-a-warning: config.toml is a
    file the user is encouraged to edit and hand around, and a token pasted
    into it would be the one plaintext secret outside the secrets tree.

    The literal itself is never interpolated into the error message — a user
    who mis-pastes a real token into config.toml must not have it echoed back
    on stderr (or in a traceback) by the very command that's supposed to be
    checking it.
    """
    if raw is None:
        return None
    if not raw.startswith(_ENV_PREFIX):
        raise ValueError(f"{field} must use env:NAME, got a literal value instead")
    return os.environ.get(raw[len(_ENV_PREFIX) :]) or None


def _require_table(raw: dict, section_name: str, *, path: Path) -> dict:
    """A section of `config.toml` must be a TOML table.

    `ui = 5` is valid TOML but not a valid section — every parser below this
    (`_parse_refresh_interval`, `_require_bool`, etc.) calls `.get()` on
    whatever `raw.get(section_name, {})` returns, and an `int` has no
    `.get()`. Left unchecked that's a bare `AttributeError`, which is not a
    `ValueError` and so slips straight past every caller's `except
    (RuntimeError, ValueError)`/`except ValueError` guard — `config.toml` is
    a file this design explicitly invites the user to hand-edit, and a typo
    like this must not crash the TUI at mount or `config show`.
    """
    value = raw.get(section_name, {})
    if not isinstance(value, dict):
        # ValueError, not TypeError -- matches _require_bool below and every
        # other config validation error in this module.
        raise ValueError(  # noqa: TRY004
            f"[{section_name}] must be a table, got {value!r} ({type(value).__name__}) in {path}"
        )
    return value


def _require_bool(section: dict, key: str, default: bool, *, section_name: str, path: Path) -> bool:
    if key not in section:
        return default
    value = section[key]
    if not isinstance(value, bool):
        # ValueError, not TypeError: every other config validation error in
        # this module is a ValueError (matching the CLI's blanket `except
        # ValueError` and this module's own tests) regardless of whether the
        # underlying problem is a bad type or a bad value.
        raise ValueError(  # noqa: TRY004
            f"[{section_name}] {key} must be true or false, got {value!r} "
            f"({type(value).__name__}) in {path}"
        )
    return value


def _parse_refresh_interval(ui: dict, *, path: Path) -> int:
    raw = ui.get("refresh_interval_ms", 2000)
    try:
        value = int(raw)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"[ui] refresh_interval_ms must be an integer, got {raw!r} in {path}"
        ) from e
    if isinstance(raw, bool) or value < _MIN_REFRESH_INTERVAL_MS:
        raise ValueError(
            f"[ui] refresh_interval_ms must be an integer >= {_MIN_REFRESH_INTERVAL_MS}, "
            f"got {raw!r} in {path}"
        )
    return value


def _parse_default_harness(defaults: dict, *, path: Path) -> str:
    harness = defaults.get("harness", "claude")
    if harness not in harnesses.HARNESSES:
        raise ValueError(
            f"[defaults] harness must be one of {', '.join(harnesses.HARNESSES)}, "
            f"got {harness!r} in {path}"
        )
    return harness


def _parse_default_view(ui: dict, *, path: Path) -> str:
    view = ui.get("default_view", "agents")
    if view not in _VIEWS:
        raise ValueError(
            f"[ui] default_view must be one of {', '.join(_VIEWS)}, got {view!r} in {path}"
        )
    return view


def _find_unknown_keys(raw: dict) -> tuple[str, ...]:
    """Collect section/key names outside the recognised shape, without ever
    raising on them — see `_KNOWN_SECTIONS` for why this is a report, not a
    rejection.
    """
    unknown: list[str] = []
    for section_name, section_value in raw.items():
        if section_name not in _KNOWN_SECTIONS:
            unknown.append(section_name)
            continue
        if not isinstance(section_value, dict):
            continue
        for key in section_value:
            if key not in _KNOWN_SECTIONS[section_name]:
                unknown.append(f"{section_name}.{key}")
    return tuple(unknown)


def load() -> Config:
    path = paths.config_dir() / "config.toml"
    if not path.exists():
        return Config()
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path} is not valid TOML: {e}") from e

    general = _require_table(raw, "general", path=path)
    ui = _require_table(raw, "ui", path=path)
    defaults = _require_table(raw, "defaults", path=path)
    notifier = _require_table(raw, "notifier", path=path)
    herdr = _require_table(raw, "herdr", path=path)

    return Config(
        default_community=general.get("default_community"),
        refresh_interval_ms=_parse_refresh_interval(ui, path=path),
        default_view=_parse_default_view(ui, path=path),
        theme=ui.get("theme", "buzz-fleet"),
        confirm_destructive=_require_bool(
            ui, "confirm_destructive", True, section_name="ui", path=path
        ),
        default_harness=_parse_default_harness(defaults, path=path),
        ntfy_url=notifier.get("ntfy_url"),
        ntfy_token=resolve_secret(notifier.get("ntfy_token"), field="[notifier] ntfy_token"),
        herdr_report_agents=_require_bool(
            herdr, "report_agents", False, section_name="herdr", path=path
        ),
        unknown_keys=_find_unknown_keys(raw),
    )
