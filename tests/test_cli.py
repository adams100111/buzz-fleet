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
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
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
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
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


def test_agent_create_passes_session_flags(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeManager:
        def create_agent(self, **kwargs):
            captured.update(kwargs)
            return _agent()

    monkeypatch.setattr("buzz_fleet.cli.app._load_manager", lambda community: FakeManager())
    result = runner_cli.invoke(app, [
        "agent", "create", "--community", "e", "--display-name", "X", "--harness", "claude",
        "--prompt-file", "/dev/null", "--session-policy", "channel", "--max-turns-per-session", "7",
        "--heartbeat-interval-seconds", "0",
    ])
    assert result.exit_code == 0, result.output
    assert (captured["session_policy"], captured["max_turns_per_session"], captured["heartbeat_interval_seconds"]) == ("channel", 7, 0)


def test_agent_create_rejects_bad_session_policy(monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.cli.app._load_manager", lambda community: object())
    result = runner_cli.invoke(app, ["agent", "create", "--community", "e", "--display-name", "X",
                                     "--harness", "claude", "--prompt-file", "/dev/null", "--session-policy", "bogus"])
    assert result.exit_code == 1 and "thread, channel" in result.output


def test_agent_create_passes_directory_flags(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeManager:
        def create_agent(self, **kwargs):
            captured.update(kwargs)
            return _agent()

    monkeypatch.setattr("buzz_fleet.cli.app._load_manager", lambda community: FakeManager())
    result = runner_cli.invoke(app, ["agent", "create", "--community", "e", "--display-name", "X", "--harness", "claude",
                                     "--prompt-file", "/dev/null", "--role", "reviewer", "--capability", "laravel",
                                     "--capability", "docker-build", "--description", "Reviews."])
    assert result.exit_code == 0, result.output
    assert (captured["role"], captured["capabilities"], captured["description"]) == ("reviewer", ["laravel", "docker-build"], "Reviews.")


def test_agent_create_defaults_directory_fields_to_none(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeManager:
        def create_agent(self, **kwargs):
            captured.update(kwargs)
            return _agent()

    monkeypatch.setattr("buzz_fleet.cli.app._load_manager", lambda community: FakeManager())
    result = runner_cli.invoke(app, ["agent", "create", "--community", "e", "--display-name", "X", "--harness", "claude",
                                     "--prompt-file", "/dev/null"])
    assert result.exit_code == 0, result.output
    assert (captured["role"], captured["capabilities"], captured["description"]) == (None, None, None)


def test_agent_update_passes_directory_flags(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeManager:
        def update_agent(self, agent_id: str, **changes: object) -> object:
            calls["changes"] = changes
            return _agent()

    monkeypatch.setattr("buzz_fleet.cli.app._load_manager", lambda community: FakeManager())
    result = runner_cli.invoke(app, ["agent", "update", "--community", "e", "agent-1", "--role", "reviewer",
                                     "--capability", "laravel", "--capability", "docker-build",
                                     "--description", "Reviews."])
    assert result.exit_code == 0, result.output
    assert calls["changes"] == {"role": "reviewer", "capabilities": ["laravel", "docker-build"], "description": "Reviews."}


def test_agent_create_env_and_mcp_flags(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeManager:
        def create_agent(self, **kwargs):
            captured.update(kwargs)
            return _agent()

    monkeypatch.setattr("buzz_fleet.cli.app._load_manager", lambda community: FakeManager())
    result = runner_cli.invoke(app, ["agent", "create", "--community", "e", "--display-name", "X", "--harness", "claude",
                                     "--prompt-file", "/dev/null", "--env", "A=1", "--env", "B=2",
                                     "--mcp-name", "boost", "--mcp-command", "php", "--mcp-arg", "artisan", "--mcp-arg", "boost:mcp"])
    assert result.exit_code == 0, result.output
    assert captured["env"] == {"A": "1", "B": "2"}
    assert captured["mcp_server"].command == "php" and captured["mcp_server"].args == ["artisan", "boost:mcp"]


def test_agent_create_env_file_is_overridden_by_repeated_env_flag(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeManager:
        def create_agent(self, **kwargs):
            captured.update(kwargs)
            return _agent()

    monkeypatch.setattr("buzz_fleet.cli.app._load_manager", lambda community: FakeManager())
    env_file = tmp_path / "agent.env"
    env_file.write_text("A=from-file\nC=only-in-file\n")
    result = runner_cli.invoke(app, ["agent", "create", "--community", "e", "--display-name", "X", "--harness", "claude",
                                     "--prompt-file", "/dev/null", "--env-file", str(env_file), "--env", "A=1"])
    assert result.exit_code == 0, result.output
    assert captured["env"] == {"A": "1", "C": "only-in-file"}


def test_agent_create_rejects_env_without_equals(monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.cli.app._load_manager", lambda community: object())
    result = runner_cli.invoke(app, ["agent", "create", "--community", "e", "--display-name", "X", "--harness", "claude",
                                     "--prompt-file", "/dev/null", "--env", "NOTKEYVALUE"])
    assert result.exit_code == 1
    assert "KEY=VALUE" in result.output


def test_agent_create_rejects_mcp_command_without_mcp_name(monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.cli.app._load_manager", lambda community: object())
    result = runner_cli.invoke(app, ["agent", "create", "--community", "e", "--display-name", "X", "--harness", "claude",
                                     "--prompt-file", "/dev/null", "--mcp-command", "php"])
    assert result.exit_code == 1
    assert "--mcp-name and --mcp-command" in result.output


def test_agent_create_rejects_unsafe_mcp_name_with_message_not_traceback(monkeypatch) -> None:
    # Finding 5 (final review): _resolve_mcp_server was called outside the
    # try in agent_create, so an invalid --mcp-name raised a raw pydantic
    # ValidationError (out of McpServer's own constructor) and printed a
    # traceback instead of the message-plus-exit-1 every other validation
    # on this command produces.
    monkeypatch.setattr("buzz_fleet.cli.app._load_manager", lambda community: object())
    result = runner_cli.invoke(app, ["agent", "create", "--community", "e", "--display-name", "X", "--harness", "claude",
                                     "--prompt-file", "/dev/null", "--mcp-name", "../x", "--mcp-command", "php"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "not a safe filesystem path segment" in result.output


def test_agent_update_rejects_unsafe_mcp_name_with_message_not_traceback(monkeypatch) -> None:
    # Same bug, the agent_update half: agent_update had no try/except at all
    # around _resolve_mcp_server or manager.update_agent, so any ValueError
    # from either -- an unsafe --mcp-name, or an unsafe --env key -- printed
    # a raw traceback.
    monkeypatch.setattr("buzz_fleet.cli.app._load_manager", lambda community: object())
    result = runner_cli.invoke(app, ["agent", "update", "--community", "e", "agent-1",
                                     "--mcp-name", "../x", "--mcp-command", "php"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "not a safe filesystem path segment" in result.output


def test_agent_update_rejects_unsafe_env_key_with_message_not_traceback(monkeypatch) -> None:
    # agent_update's own try/except (added by this fix) must also catch a
    # ValueError raised by manager.update_agent itself (e.g. an unsafe
    # --env key -- see test_manager.py's
    # test_update_agent_rejects_unsafe_env_key for that check in isolation),
    # not just one raised earlier by _resolve_mcp_server.
    class FakeManager:
        def update_agent(self, agent_id, **changes):
            raise ValueError("env var name 'BUZZ_ACP_AGENT_OWNER' is reserved for buzz-fleet's own use")

    monkeypatch.setattr("buzz_fleet.cli.app._load_manager", lambda community: FakeManager())
    result = runner_cli.invoke(app, ["agent", "update", "--community", "e", "agent-1",
                                     "--env", "BUZZ_ACP_AGENT_OWNER=attacker"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "reserved" in result.output


def test_agent_create_without_env_or_mcp_flags_passes_none(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeManager:
        def create_agent(self, **kwargs):
            captured.update(kwargs)
            return _agent()

    monkeypatch.setattr("buzz_fleet.cli.app._load_manager", lambda community: FakeManager())
    result = runner_cli.invoke(app, ["agent", "create", "--community", "e", "--display-name", "X", "--harness", "claude",
                                     "--prompt-file", "/dev/null"])
    assert result.exit_code == 0, result.output
    assert captured["env"] is None
    assert captured["mcp_server"] is None


def test_agent_update_env_and_mcp_flags(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeManager:
        def update_agent(self, agent_id: str, **changes: object) -> object:
            calls["changes"] = changes
            return _agent()

    monkeypatch.setattr("buzz_fleet.cli.app._load_manager", lambda community: FakeManager())
    result = runner_cli.invoke(app, ["agent", "update", "--community", "e", "agent-1",
                                     "--env", "A=1", "--mcp-name", "boost", "--mcp-command", "php"])
    assert result.exit_code == 0, result.output
    changes = calls["changes"]
    assert changes["env"] == {"A": "1"}
    assert changes["mcp_server"].name == "boost" and changes["mcp_server"].command == "php"


def test_agent_update_without_env_or_mcp_flags_omits_them(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeManager:
        def update_agent(self, agent_id: str, **changes: object) -> object:
            calls["changes"] = changes
            return _agent()

    monkeypatch.setattr("buzz_fleet.cli.app._load_manager", lambda community: FakeManager())
    result = runner_cli.invoke(app, ["agent", "update", "--community", "e", "agent-1", "--role", "reviewer"])
    assert result.exit_code == 0, result.output
    assert "env" not in calls["changes"] and "mcp_server" not in calls["changes"]


def test_fleet_init_prints_channel_and_record(monkeypatch) -> None:
    from buzz_fleet.orchestration.record import FleetRecord

    class FakeManager:
        def __init__(self) -> None:
            self._last_retrieval_secret: str | None = None

        def init_fleet_channel(self, existing, host):
            self._last_retrieval_secret = "nsec1thesecretkeynevergetsstoredanywhere"
            return "6f1c0000-0000-4000-8000-000000000000", FleetRecord(retrieval_key="r" * 64, created_at=1)

    monkeypatch.setattr("buzz_fleet.cli.fleet_commands._load_manager", lambda community: FakeManager())
    result = runner_cli.invoke(app, ["fleet", "init", "--community", "e"])
    assert result.exit_code == 0, result.output
    assert "6f1c0000-0000-4000-8000-000000000000" in result.output and "r" * 64 in result.output
    # The retrieval secret is the entire security-relevant output of `fleet
    # init` — nothing stores it and nothing can regenerate it — so the CLI
    # must actually print it, not just the channel id and public key.
    assert "nsec1thesecretkeynevergetsstoredanywhere" in result.output


def test_fleet_init_surfaces_non_runtime_error_as_message_not_traceback(monkeypatch) -> None:
    # Finding 5 (final review): fleet_init caught only RuntimeError where
    # every sibling command uses fleet_commands._ERRORS (RuntimeError,
    # ValueError, JSONDecodeError, KeyError) -- _find_fleet_record indexes
    # a relay response with meta["channel_id"], which can raise KeyError on
    # a malformed relay reply. That must produce the same message-plus-
    # exit-1 contract as every other failure here, not a raw traceback.
    class FakeManager:
        def init_fleet_channel(self, existing, host):
            raise KeyError("channel_id")

    monkeypatch.setattr("buzz_fleet.cli.fleet_commands._load_manager", lambda community: FakeManager())
    result = runner_cli.invoke(app, ["fleet", "init", "--community", "e"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "channel_id" in result.output


def test_fleet_status_no_record_message_goes_to_stderr_like_its_sibling(monkeypatch) -> None:
    # Finding 5 (final review): the duplicate-record branch already wrote to
    # stderr, but the adjacent "No fleet record found" branch -- the *other*
    # half of the same `if/else` that ends in the same `raise
    # typer.Exit(code=1)` -- wrote to stdout instead. Both are the failure
    # output of the same command and must go to the same stream.
    class FakeManager:
        _last_fleet_error = None

        def ensure_fleet_record(self):
            return None

    monkeypatch.setattr("buzz_fleet.cli.fleet_commands._load_manager", lambda community: FakeManager())
    result = runner_cli.invoke(app, ["fleet", "status", "--community", "e"])
    assert result.exit_code == 1
    assert "No fleet record found" in result.stderr
    assert "No fleet record found" not in result.stdout


def test_fleet_status_reports_duplicate_record_error_instead_of_generic_message(monkeypatch) -> None:
    class FakeManager:
        def __init__(self) -> None:
            self._last_fleet_error = (
                "more than one channel carries a fleet record: chan-a, chan-b; archive all but one"
            )

        def ensure_fleet_record(self):
            return None

    monkeypatch.setattr("buzz_fleet.cli.fleet_commands._load_manager", lambda community: FakeManager())
    result = runner_cli.invoke(app, ["fleet", "status", "--community", "e"])
    assert result.exit_code == 1
    # Must surface the real problem, not the generic "run fleet init"
    # message — running fleet init again here would create a THIRD channel.
    assert "more than one channel carries a fleet record" in result.output
    assert "Run `buzz-fleet fleet init`" not in result.output
