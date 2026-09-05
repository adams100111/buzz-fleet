from buzz_fleet import harnesses


def _fake_which(available: set[str]):
    def which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in available else None

    return which


def test_detects_available_when_adapter_binary_present(monkeypatch) -> None:
    monkeypatch.setattr(harnesses.shutil, "which", _fake_which({"claude-agent-acp"}))

    availability = harnesses.detect_harness_availability()

    assert availability["claude"] == "available"


def test_detects_available_via_second_adapter_alias(monkeypatch) -> None:
    monkeypatch.setattr(harnesses.shutil, "which", _fake_which({"claude-code-acp"}))

    availability = harnesses.detect_harness_availability()

    assert availability["claude"] == "available"


def test_detects_adapter_missing_when_only_underlying_cli_present(monkeypatch) -> None:
    monkeypatch.setattr(harnesses.shutil, "which", _fake_which({"codex"}))

    availability = harnesses.detect_harness_availability()

    assert availability["codex"] == "adapter_missing"


def test_detects_not_installed_when_neither_present(monkeypatch) -> None:
    monkeypatch.setattr(harnesses.shutil, "which", _fake_which(set()))

    availability = harnesses.detect_harness_availability()

    assert availability["pi"] == "not_installed"


def test_goose_has_no_adapter_missing_state(monkeypatch) -> None:
    """goose's adapter command IS its underlying CLI — missing means not_installed, never adapter_missing."""
    monkeypatch.setattr(harnesses.shutil, "which", _fake_which(set()))

    availability = harnesses.detect_harness_availability()

    assert availability["goose"] == "not_installed"


def test_harness_select_options_sorts_available_first_and_labels_status(monkeypatch) -> None:
    monkeypatch.setattr(harnesses.shutil, "which", _fake_which({"codex-acp"}))

    options = harnesses.harness_select_options()

    assert options[0] == ("codex", "codex")
    labels = dict(options)
    assert labels["codex"] == "codex"
    claude_label = next(label for label, value in options if value == "claude")
    assert " (not installed)" in claude_label
    assert {value for _, value in options} == set(harnesses.HARNESSES)


def test_default_harness_picks_first_available(monkeypatch) -> None:
    monkeypatch.setattr(harnesses.shutil, "which", _fake_which({"pi-acp"}))

    assert harnesses.default_harness() == "pi"


def test_default_harness_falls_back_to_first_known_when_none_available(monkeypatch) -> None:
    monkeypatch.setattr(harnesses.shutil, "which", _fake_which(set()))

    assert harnesses.default_harness() == harnesses.HARNESSES[0]
