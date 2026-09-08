"""XDG resolution. Every location buzz-fleet writes comes from here."""

from pathlib import Path

from buzz_fleet import paths


def test_config_dir_honours_xdg_config_home(monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg/cfg")
    assert paths.config_dir() == Path("/xdg/cfg/buzz-fleet")


def test_config_dir_falls_back_to_dot_config(monkeypatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/u")))
    assert paths.config_dir() == Path("/home/u/.config/buzz-fleet")


def test_state_dir_honours_xdg_state_home(monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "/xdg/state")
    assert paths.state_dir() == Path("/xdg/state/buzz-fleet")


def test_state_dir_falls_back_to_local_state(monkeypatch) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/u")))
    assert paths.state_dir() == Path("/home/u/.local/state/buzz-fleet")


def test_data_dir_falls_back_to_local_share(monkeypatch) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/u")))
    assert paths.data_dir() == Path("/home/u/.local/share/buzz-fleet")


def test_secrets_dir_lives_under_state(monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "/xdg/state")
    assert paths.secrets_dir() == Path("/xdg/state/buzz-fleet/secrets")


def test_runtime_dir_falls_back_under_state_when_unset(monkeypatch) -> None:
    """XDG_RUNTIME_DIR is genuinely absent in some non-login contexts (cron,
    a bare `ssh host cmd`), so it needs a fallback the others don't."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", "/xdg/state")
    assert paths.runtime_dir() == Path("/xdg/state/buzz-fleet/run")


def test_legacy_dir_is_the_pre_migration_tree(monkeypatch) -> None:
    """Fixed, and deliberately ignores XDG: it names where files actually are
    on a machine installed before this change, not where they should be."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg/cfg")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/u")))
    assert paths.legacy_dir() == Path("/home/u/.config/buzz-fleet")
