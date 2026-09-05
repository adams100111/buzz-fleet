import json
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace

from typer.testing import CliRunner

from buzz_fleet.cli.app import app
from buzz_fleet.models import Agent, AgentVisibilityState, SystemPromptSource

runner_cli = CliRunner()


def _agent(**overrides: object) -> Agent:
    defaults: dict[str, object] = {
        "id": "test-agent",
        "community_id": "eltahir",
        "display_name": "Test Agent",
        "harness": "claude",
        "private_key": "nsec1x",
        "public_key": "a" * 64,
        "system_prompt_source": SystemPromptSource(kind="inline", text="You are a test agent."),
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Agent(**defaults)


class FakeRunner:
    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[1:2] == ["pubkey-from-nsec"]:
            payload = {"ok": True, "public_key": "a" * 64}
        else:
            payload = {"ok": True}
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")


def test_version_flag_prints_version_and_exits() -> None:
    from buzz_fleet import __version__

    result = runner_cli.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_connect_saves_community_on_success(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.cli.app.RealCommandRunner", lambda: FakeRunner())

    result = runner_cli.invoke(
        app,
        ["connect", "--id", "eltahir", "--relay", "wss://buzz.eltahir.me", "--admin-nsec", "nsec1abc"],
    )

    assert result.exit_code == 0
    from buzz_fleet.state import load_community

    saved = load_community("eltahir")
    assert saved is not None
    assert saved.relay_url == "wss://buzz.eltahir.me"


def test_connect_prompts_for_admin_nsec_with_masked_input_when_omitted(tmp_path, monkeypatch) -> None:
    """Regression test for Fix 6(a): --admin-nsec must be promptable (masked)
    rather than required as a plain CLI argument, to keep the owner's nsec out
    of shell history and /proc/<pid>/cmdline.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.cli.app.RealCommandRunner", lambda: FakeRunner())

    result = runner_cli.invoke(
        app,
        ["connect", "--id", "eltahir", "--relay", "wss://buzz.eltahir.me"],
        input="nsec1abc\n",
    )

    assert result.exit_code == 0, result.output
    from buzz_fleet.state import load_community

    saved = load_community("eltahir")
    assert saved is not None
    assert saved.relay_admin_nsec.get_secret_value() == "nsec1abc"


def test_agent_update_calls_manager_with_changes(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeAgentManager:
        def __init__(self, runner: object, community: object) -> None:
            pass

        def update_agent(self, agent_id: str, **changes: object) -> object:
            calls["agent_id"] = agent_id
            calls["changes"] = changes
            return SimpleNamespace(id=agent_id)

    monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)

    result = runner_cli.invoke(
        app,
        ["agent", "update", "--community", "eltahir", "agent-1", "--display-name", "New Name"],
    )

    assert result.exit_code == 0, result.output
    assert calls["agent_id"] == "agent-1"
    assert calls["changes"] == {"display_name": "New Name"}


def test_agent_update_with_no_changes_exits_with_error(monkeypatch) -> None:
    class FakeAgentManager:
        def __init__(self, runner: object, community: object) -> None:
            pass

        def update_agent(self, agent_id: str, **changes: object) -> object:
            raise AssertionError("update_agent should not be called when there are no changes")

    monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)

    result = runner_cli.invoke(app, ["agent", "update", "--community", "eltahir", "agent-1"])

    assert result.exit_code == 1
    assert "Nothing to update" in result.output


def test_agent_create_with_blank_display_name_exits_cleanly(tmp_path, monkeypatch) -> None:
    """Regression test: agent_slug raising ValueError on a blank/punctuation-only
    display name must exit 1 with a clear message, not crash with a raw traceback.
    """

    class FakeAgentManager:
        def __init__(self, runner: object, community: object) -> None:
            pass

        def create_agent(self, **kwargs: object) -> object:
            raise ValueError("display_name must contain at least one alphanumeric character")

    monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)

    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("You are an agent.")

    result = runner_cli.invoke(
        app,
        [
            "agent",
            "create",
            "--community",
            "eltahir",
            "--display-name",
            "!!!",
            "--harness",
            "claude",
            "--prompt-file",
            str(prompt_file),
        ],
    )

    assert result.exit_code == 1
    assert "alphanumeric" in result.output


def test_agent_create_passes_new_optional_fields(tmp_path, monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeAgentManager:
        def __init__(self, runner: object, community: object) -> None:
            pass

        def create_agent(self, **kwargs: object) -> object:
            calls.update(kwargs)
            return SimpleNamespace(id="test-agent", public_key="ab" * 32)

    monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)

    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("You are an agent.")

    result = runner_cli.invoke(
        app,
        [
            "agent", "create",
            "--community", "eltahir",
            "--display-name", "Test Agent",
            "--harness", "claude",
            "--prompt-file", str(prompt_file),
            "--team-instructions", "Test-first always.",
            "--model", "claude-sonnet-5",
            "--parallelism", "3",
            "--idle-timeout-seconds", "120",
            "--max-turn-duration-seconds", "600",
            "--respond-to-allowlist", f"{'a' * 64},{'b' * 64}",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["team_instructions"] == "Test-first always."
    assert calls["model"] == "claude-sonnet-5"
    assert calls["parallelism"] == 3
    assert calls["idle_timeout_seconds"] == 120
    assert calls["max_turn_duration_seconds"] == 600
    assert calls["respond_to_allowlist"] == ["a" * 64, "b" * 64]


def test_agent_update_passes_new_optional_fields(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeAgentManager:
        def __init__(self, runner: object, community: object) -> None:
            pass

        def update_agent(self, agent_id: str, **changes: object) -> object:
            calls["agent_id"] = agent_id
            calls["changes"] = changes
            return SimpleNamespace(id=agent_id)

    monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)

    result = runner_cli.invoke(
        app,
        [
            "agent", "update", "--community", "eltahir", "agent-1",
            "--team-instructions", "Test-first always.",
            "--model", "claude-sonnet-5",
            "--respond-to-allowlist", "a" * 64,
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["changes"] == {
        "team_instructions": "Test-first always.",
        "model": "claude-sonnet-5",
        "respond_to_allowlist": ["a" * 64],
    }


def test_agent_update_with_empty_respond_to_allowlist_clears_it(monkeypatch) -> None:
    """Regression test: `--respond-to-allowlist ""` must clear the allowlist
    (None), not silently lock the agent out with a truthy [''] that matches
    no real pubkey.
    """
    calls: dict[str, object] = {}

    class FakeAgentManager:
        def __init__(self, runner: object, community: object) -> None:
            pass

        def update_agent(self, agent_id: str, **changes: object) -> object:
            calls["agent_id"] = agent_id
            calls["changes"] = changes
            return SimpleNamespace(id=agent_id)

    monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)

    result = runner_cli.invoke(
        app,
        ["agent", "update", "--community", "eltahir", "agent-1", "--respond-to-allowlist", ""],
    )

    assert result.exit_code == 0, result.output
    assert calls["changes"] == {"respond_to_allowlist": None}
    assert calls["changes"]["respond_to_allowlist"] is None


def test_agent_create_strips_whitespace_around_respond_to_allowlist_entries(tmp_path, monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeAgentManager:
        def __init__(self, runner: object, community: object) -> None:
            pass

        def create_agent(self, **kwargs: object) -> object:
            calls.update(kwargs)
            return SimpleNamespace(id="test-agent", public_key="ab" * 32)

    monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)

    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("You are an agent.")

    result = runner_cli.invoke(
        app,
        [
            "agent", "create",
            "--community", "eltahir",
            "--display-name", "Test Agent",
            "--harness", "claude",
            "--prompt-file", str(prompt_file),
            "--respond-to-allowlist", f"{'a' * 64}, {'b' * 64}",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["respond_to_allowlist"] == ["a" * 64, "b" * 64]


def test_agent_update_strips_whitespace_around_respond_to_allowlist_entries(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeAgentManager:
        def __init__(self, runner: object, community: object) -> None:
            pass

        def update_agent(self, agent_id: str, **changes: object) -> object:
            calls["agent_id"] = agent_id
            calls["changes"] = changes
            return SimpleNamespace(id=agent_id)

    monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)

    result = runner_cli.invoke(
        app,
        [
            "agent", "update", "--community", "eltahir", "agent-1",
            "--respond-to-allowlist", f"{'a' * 64}, {'b' * 64}",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["changes"] == {"respond_to_allowlist": ["a" * 64, "b" * 64]}


def test_harness_list_prints_availability(monkeypatch) -> None:
    from buzz_fleet import harnesses

    monkeypatch.setattr(
        harnesses.shutil, "which", lambda cmd: "/usr/bin/x" if cmd in ("claude-agent-acp", "codex") else None
    )

    result = runner_cli.invoke(app, ["harness", "list"])

    assert result.exit_code == 0, result.output
    assert "claude\tavailable" in result.output
    assert "codex\tadapter_missing" in result.output


def test_harness_install_runs_install_and_reports_success(monkeypatch) -> None:
    from buzz_fleet import harnesses

    calls: list[tuple[object, str]] = []

    def fake_install_adapter(runner: object, name: str) -> None:
        calls.append((runner, name))

    monkeypatch.setattr(harnesses, "install_adapter", fake_install_adapter)

    result = runner_cli.invoke(app, ["harness", "install", "codex"])

    assert result.exit_code == 0, result.output
    assert "Installed codex" in result.output
    assert calls == [(calls[0][0], "codex")]


def test_harness_install_rejects_unknown_harness() -> None:
    result = runner_cli.invoke(app, ["harness", "install", "bogus"])

    assert result.exit_code == 1
    assert "Unknown harness" in result.output


def test_harness_install_reports_failure(monkeypatch) -> None:
    from buzz_fleet import harnesses

    def fake_install_adapter(runner: object, name: str) -> None:
        raise RuntimeError("network error")

    monkeypatch.setattr(harnesses, "install_adapter", fake_install_adapter)

    result = runner_cli.invoke(app, ["harness", "install", "codex"])

    assert result.exit_code == 1
    assert "network error" in result.output


def test_agent_create_rejects_malformed_channel_id(tmp_path, monkeypatch) -> None:
    class FakeAgentManager:
        def __init__(self, runner: object, community: object) -> None:
            pass

        def create_agent(self, **kwargs: object) -> object:
            raise AssertionError("create_agent should not be called for an invalid channel id")

    monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)

    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("You are a test agent.")

    result = runner_cli.invoke(
        app,
        [
            "agent", "create",
            "--community", "eltahir",
            "--display-name", "Bad Channel",
            "--harness", "claude",
            "--prompt-file", str(prompt_file),
            "--channel-ids", "not-a-uuid",
        ],
    )

    assert result.exit_code != 0
    assert "channel" in result.output.lower()


def test_agent_create_accepts_channel_add_policy_choice(tmp_path, monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeAgentManager:
        def __init__(self, runner: object, community: object) -> None:
            pass

        def create_agent(self, **kwargs: object) -> object:
            calls.update(kwargs)
            return SimpleNamespace(id="test-agent", public_key="ab" * 32)

    monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)

    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("You are a test agent.")

    result = runner_cli.invoke(
        app,
        [
            "agent", "create",
            "--community", "eltahir",
            "--display-name", "Policy Agent",
            "--harness", "claude",
            "--prompt-file", str(prompt_file),
            "--channel-add-policy", "nobody",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["channel_add_policy"] == "nobody"


def test_agent_update_rejects_invalid_channel_add_policy(monkeypatch) -> None:
    class FakeAgentManager:
        def __init__(self, runner: object, community: object) -> None:
            pass

        def update_agent(self, agent_id: str, **changes: object) -> object:
            raise AssertionError("update_agent should not be called for an invalid policy")

    monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)

    result = runner_cli.invoke(
        app,
        ["agent", "update", "--community", "eltahir", "agent-1", "--channel-add-policy", "everyone"],
    )

    assert result.exit_code == 1
    assert "channel-add-policy" in result.output


def test_agent_update_parses_channel_ids(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeAgentManager:
        def __init__(self, runner: object, community: object) -> None:
            pass

        def update_agent(self, agent_id: str, **changes: object) -> object:
            calls["changes"] = changes
            return SimpleNamespace(id=agent_id)

    monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)

    channel_id = "12345678-1234-5678-1234-567812345678"
    result = runner_cli.invoke(
        app,
        ["agent", "update", "--community", "eltahir", "agent-1", "--channel-ids", channel_id],
    )

    assert result.exit_code == 0, result.output
    assert calls["changes"] == {"channel_ids": [channel_id]}


def test_agent_list_shows_visibility_status(monkeypatch) -> None:
    """Every row now gets a fourth, status column driven by
    `visibility.visibility_status_text`. Two agents in different visibility
    states are listed to prove the loop renders a status for each row, not
    just the first.
    """
    unmanaged = _agent(id="agent-unmanaged", display_name="Unmanaged")
    synced = _agent(
        id="agent-synced",
        display_name="Synced",
        visibility_managed=True,
        visibility_state=AgentVisibilityState(
            profile_published=True,
            managed_agent_published=True,
            add_policy_published=True,
        ),
    )

    class FakeAgentManager:
        def __init__(self, runner: object, community: object) -> None:
            pass

        def ensure_runtime_ready(self) -> None:
            pass

        def list_agents(self) -> list[object]:
            return [unmanaged, synced]

    monkeypatch.setattr("buzz_fleet.cli.app.state.load_community", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr("buzz_fleet.cli.app.AgentManager", FakeAgentManager)

    result = runner_cli.invoke(app, ["agent", "list", "--community", "eltahir"])

    assert result.exit_code == 0, result.output
    assert "agent-unmanaged\tUnmanaged\tclaude\t—" in result.output
    assert "agent-synced\tSynced\tclaude\tsynced" in result.output
