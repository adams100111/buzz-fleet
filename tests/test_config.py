"""config.toml is read-only to the app: nothing in src/ writes it, so comments
and formatting survive and no TOML writer dependency is needed."""

import pytest

from buzz_fleet import config, paths


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


def test_malformed_toml_names_the_file(monkeypatch, tmp_path) -> None:
    _write(tmp_path, "[ui\n", monkeypatch)
    with pytest.raises(ValueError, match="config.toml"):
        config.load()


def test_unknown_default_view_is_refused(monkeypatch, tmp_path) -> None:
    _write(tmp_path, '[ui]\ndefault_view = "nonsense"\n', monkeypatch)
    with pytest.raises(ValueError, match="default_view"):
        config.load()
