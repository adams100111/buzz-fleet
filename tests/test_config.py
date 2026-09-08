"""config.toml is read-only to the app: nothing in src/ writes it, so comments
and formatting survive and no TOML writer dependency is needed."""

import pytest
from typer.testing import CliRunner

from buzz_fleet import config, paths
from buzz_fleet.cli.app import app

runner_cli = CliRunner()


def _write(tmp_path, body: str, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    target = paths.config_dir() / "config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)


def test_defaults_apply_when_no_file_exists(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = config.load()
    assert cfg.default_community is None
    assert cfg.refresh_interval_ms == 2000
    assert cfg.default_view == "agents"
    assert cfg.confirm_destructive is True
    assert cfg.default_harness == "claude"
    assert cfg.herdr_report_agents is False


def test_values_are_read(monkeypatch, tmp_path) -> None:
    _write(
        tmp_path,
        """
[general]
default_community = "eltahir"

[ui]
refresh_interval_ms = 500
default_view = "runs"
confirm_destructive = false

[defaults]
harness = "codex"
""",
        monkeypatch,
    )
    cfg = config.load()
    assert cfg.default_community == "eltahir"
    assert cfg.refresh_interval_ms == 500
    assert cfg.default_view == "runs"
    assert cfg.confirm_destructive is False
    assert cfg.default_harness == "codex"


def test_partial_file_keeps_other_defaults(monkeypatch, tmp_path) -> None:
    _write(tmp_path, '[ui]\ntheme = "mono"\n', monkeypatch)
    cfg = config.load()
    assert cfg.theme == "mono"
    assert cfg.refresh_interval_ms == 2000


def test_repr_does_not_leak_the_resolved_ntfy_token(monkeypatch, tmp_path) -> None:
    """Final whole-branch review FIX 3: `Config` is a frozen dataclass with
    the auto-generated `__repr__`, so `repr(cfg)`/`print(cfg)`/a log line
    carrying the object would expose the resolved token. Nothing does that
    today, but Phase B's JSON-RPC daemon will serialise Config objects."""
    _write(tmp_path, '[notifier]\nntfy_token = "env:MY_TOKEN"\n', monkeypatch)
    monkeypatch.setenv("MY_TOKEN", "t0ken-super-secret")
    cfg = config.load()
    assert cfg.ntfy_token == "t0ken-super-secret"
    assert "t0ken-super-secret" not in repr(cfg)


def test_env_indirection_resolves(monkeypatch, tmp_path) -> None:
    _write(tmp_path, '[notifier]\nntfy_token = "env:MY_TOKEN"\n', monkeypatch)
    monkeypatch.setenv("MY_TOKEN", "t0ken")
    assert config.load().ntfy_token == "t0ken"


def test_env_indirection_to_an_unset_variable_is_none(monkeypatch, tmp_path) -> None:
    _write(tmp_path, '[notifier]\nntfy_token = "env:MISSING"\n', monkeypatch)
    monkeypatch.delenv("MISSING", raising=False)
    assert config.load().ntfy_token is None


def test_literal_secret_is_refused(monkeypatch, tmp_path) -> None:
    """A secret-typed field must indirect through the environment. Accepting a
    literal would put a token in a file the user is invited to edit and share."""
    _write(tmp_path, '[notifier]\nntfy_token = "tk_literal"\n', monkeypatch)
    with pytest.raises(ValueError, match="must use env:"):
        config.load()


def test_literal_secret_error_does_not_leak_the_token(monkeypatch, tmp_path) -> None:
    """FIX 1: the error naming the problem must never echo the pasted value —
    that's the exact path a mis-pasted secret takes to a traceback or stderr."""
    _write(tmp_path, '[notifier]\nntfy_token = "tk_super_secret_literal"\n', monkeypatch)
    with pytest.raises(ValueError) as exc_info:
        config.load()
    assert "tk_super_secret_literal" not in str(exc_info.value)


def test_config_show_does_not_leak_a_literal_secret(monkeypatch, tmp_path) -> None:
    """FIX 1: `config show` must not let a ValueError from a bad secret
    propagate as an unhandled traceback (which would print the token) — it
    must be caught and reported like every other CLI error, with a nonzero
    exit and no traceback."""
    _write(tmp_path, '[notifier]\nntfy_token = "tk_super_secret_literal"\n', monkeypatch)
    result = runner_cli.invoke(app, ["config", "show"])
    assert result.exit_code == 1
    assert "tk_super_secret_literal" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_malformed_toml_names_the_file(monkeypatch, tmp_path) -> None:
    _write(tmp_path, "[ui\n", monkeypatch)
    with pytest.raises(ValueError, match="config.toml"):
        config.load()


def test_unknown_default_view_is_refused(monkeypatch, tmp_path) -> None:
    _write(tmp_path, '[ui]\ndefault_view = "nonsense"\n', monkeypatch)
    with pytest.raises(ValueError, match="default_view"):
        config.load()


def test_confirm_destructive_string_is_refused(monkeypatch, tmp_path) -> None:
    """FIX 2: a quoted 'false' is valid TOML and an easy hand-edit slip, but
    is truthy under bool() — it must be rejected, not silently coerced to
    True (the opposite of what the user wrote)."""
    _write(tmp_path, '[ui]\nconfirm_destructive = "false"\n', monkeypatch)
    with pytest.raises(ValueError, match="confirm_destructive"):
        config.load()


def test_herdr_report_agents_string_is_refused(monkeypatch, tmp_path) -> None:
    """FIX 2, same coercion bug on the other boolean field."""
    _write(tmp_path, '[herdr]\nreport_agents = "true"\n', monkeypatch)
    with pytest.raises(ValueError, match="report_agents"):
        config.load()


def test_unknown_default_harness_is_refused(monkeypatch, tmp_path) -> None:
    """FIX 3: default_harness must be validated against harnesses.HARNESSES."""
    _write(tmp_path, '[defaults]\nharness = "nonsense"\n', monkeypatch)
    with pytest.raises(ValueError, match="harness"):
        config.load()


def test_refresh_interval_ms_zero_is_refused(monkeypatch, tmp_path) -> None:
    """FIX 3: refresh_interval_ms drives a UI refresh loop — zero or negative
    is nonsense and must be rejected, not silently accepted."""
    _write(tmp_path, "[ui]\nrefresh_interval_ms = 0\n", monkeypatch)
    with pytest.raises(ValueError, match="refresh_interval_ms"):
        config.load()


def test_refresh_interval_ms_negative_is_refused(monkeypatch, tmp_path) -> None:
    _write(tmp_path, "[ui]\nrefresh_interval_ms = -500\n", monkeypatch)
    with pytest.raises(ValueError, match="refresh_interval_ms"):
        config.load()


def test_refresh_interval_ms_non_numeric_names_field_and_file(monkeypatch, tmp_path) -> None:
    """FIX 4: int("soon") must not surface as a bare, unattributed ValueError."""
    _write(tmp_path, '[ui]\nrefresh_interval_ms = "soon"\n', monkeypatch)
    with pytest.raises(ValueError, match="refresh_interval_ms") as exc_info:
        config.load()
    assert "config.toml" in str(exc_info.value)


@pytest.mark.parametrize("section_name", ["general", "ui", "defaults", "notifier", "herdr"])
def test_scalar_section_is_refused_not_a_crash(monkeypatch, tmp_path, section_name) -> None:
    """Final whole-branch review FIX 2: `ui = 5` is valid TOML but not a
    valid section — every section reader below `load()` calls `.get()` on
    whatever it's handed, and an `int` has no `.get()`. Before this fix that
    was a bare, unattributed `AttributeError`, which is not a `ValueError`
    and so slipped past every caller's `except ValueError`/`except
    (RuntimeError, ValueError)` guard (the TUI's `on_mount`, `config show`,
    `community list`/`show`'s `_active_or_none()`), crashing the TUI at
    mount on a config file this design explicitly invites the user to
    hand-edit."""
    _write(tmp_path, f"{section_name} = 5\n", monkeypatch)
    with pytest.raises(ValueError, match=section_name):
        config.load()


def test_unknown_keys_are_collected_not_rejected(monkeypatch, tmp_path) -> None:
    """FIX 5: a typo'd or forward-compatible key must not hard-fail an older
    binary reading a newer machine's config — it's reported, not raised."""
    _write(
        tmp_path,
        '[ui]\ndeafult_view = "runs"\n\n[future_section]\nsomething = 1\n',
        monkeypatch,
    )
    cfg = config.load()
    assert cfg.default_view == "agents"  # the typo didn't take effect
    assert "ui.deafult_view" in cfg.unknown_keys
    assert "future_section" in cfg.unknown_keys


def test_no_unknown_keys_when_file_is_well_formed(monkeypatch, tmp_path) -> None:
    _write(tmp_path, '[ui]\ntheme = "mono"\n', monkeypatch)
    assert config.load().unknown_keys == ()


def test_config_show_reports_unknown_keys(monkeypatch, tmp_path) -> None:
    """FIX 5: the diagnostic must surface exactly where a user goes looking
    for it — `config show` — even though `load()` itself stays silent."""
    _write(tmp_path, '[ui]\ndeafult_view = "runs"\n', monkeypatch)
    result = runner_cli.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    assert "unrecognised keys (ignored)" in result.output
    assert "ui.deafult_view" in result.output
