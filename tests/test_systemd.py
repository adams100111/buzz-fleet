import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from buzz_fleet.models import Agent, Community, SystemPromptSource
from buzz_fleet.systemd import (
    TEMPLATE_UNIT,
    agent_env_path,
    agent_prompt_path,
    ensure_linger_enabled,
    ensure_template_unit_installed,
    write_agent_files,
    write_mcp_wrapper,
)


def _agent() -> Agent:
    return Agent(
        id="laravel-backend-dev",
        community_id="eltahir",
        display_name="Laravel Backend Dev",
        harness="claude",
        private_key="nsec1agent",
        public_key="a" * 64,
        system_prompt_source=SystemPromptSource(kind="inline", text="You are the Laravel dev."),
        team_instructions="Team-wide rules here.",
        model=None,
        created_at=datetime.now(UTC),
    )


def _community() -> Community:
    return Community(id="eltahir", relay_url="wss://buzz.eltahir.me", relay_admin_nsec="nsec1admin")


def test_write_agent_files_creates_env_and_prompt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    # Deterministic regardless of what's actually on this machine's PATH —
    # the absolute-path-resolution behavior itself is covered separately
    # below and in test_harnesses.py.
    monkeypatch.setattr("buzz_fleet.harnesses.shutil.which", lambda cmd: None)
    agent = _agent()

    write_agent_files(agent, _community(), anthropic_api_key="sk-ant-test", openai_api_key=None)

    env_content = agent_env_path(agent.id).read_text()
    assert "BUZZ_PRIVATE_KEY=nsec1agent" in env_content
    assert "BUZZ_RELAY_URL=wss://buzz.eltahir.me" in env_content
    assert "BUZZ_ACP_AGENT_COMMAND=claude-agent-acp" in env_content
    assert "ANTHROPIC_API_KEY=sk-ant-test" in env_content
    assert f"BUZZ_ACP_SYSTEM_PROMPT_FILE={agent_prompt_path(agent.id)}" in env_content
    # Quoted, not bare: every agent's team instructions now always carry the
    # appended fleet coordination block (instructions.apply_coordination_block),
    # which is itself multi-line — so even a single-line operator value like
    # this one is written in systemd's quoted multi-line form (env_line).
    assert 'BUZZ_ACP_TEAM_INSTRUCTIONS="Team-wide rules here.' in env_content
    assert agent_prompt_path(agent.id).read_text() == "You are the Laravel dev."


def test_write_agent_files_resolves_adapter_command_to_absolute_path_when_on_path(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression test for a real incident: systemd's `--user` manager has

    its own fixed, minimal PATH that excludes version-manager install dirs
    (mise/nvm/asdf/...) — an agent's unit could never find an adapter that
    was genuinely installed and on the *user's* own PATH. Resolving to an
    absolute path here, at write time (inheriting buzz-fleet's own PATH),
    sidesteps that entirely.
    """
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(
        "buzz_fleet.harnesses.shutil.which",
        lambda cmd: "/home/dev/.local/share/mise/installs/node/22/bin/claude-agent-acp"
        if cmd == "claude-agent-acp"
        else None,
    )
    agent = _agent()

    write_agent_files(agent, _community(), anthropic_api_key=None, openai_api_key=None)

    env_content = agent_env_path(agent.id).read_text()
    assert (
        "BUZZ_ACP_AGENT_COMMAND=/home/dev/.local/share/mise/installs/node/22/bin/claude-agent-acp"
        in env_content
    )


def test_env_file_is_mode_0600(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    write_agent_files(_agent(), _community(), anthropic_api_key="sk-ant-test", openai_api_key=None)

    mode = stat.S_IMODE(agent_env_path("laravel-backend-dev").stat().st_mode)
    assert mode == 0o600


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def test_ensure_template_unit_installed_writes_file_and_reloads(tmp_path: Path, monkeypatch) -> None:
    unit_path = tmp_path / "systemd" / "buzz-agent@.service"
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", unit_path)
    runner = FakeRunner()

    ensure_template_unit_installed(runner)

    assert unit_path.read_text() == TEMPLATE_UNIT
    assert ["systemctl", "--user", "daemon-reload"] in runner.calls


def test_ensure_template_unit_installed_is_a_noop_when_already_current(tmp_path: Path, monkeypatch) -> None:
    unit_path = tmp_path / "systemd" / "buzz-agent@.service"
    unit_path.parent.mkdir(parents=True)
    unit_path.write_text(TEMPLATE_UNIT)
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", unit_path)
    runner = FakeRunner()

    ensure_template_unit_installed(runner)

    assert runner.calls == []


class LingerRunner:
    """FakeRunner variant that answers loginctl calls; other args get a
    generic OK, matching what ensure_linger_enabled's two calls need.
    """

    def __init__(self, already_lingering: bool, enable_succeeds: bool = True) -> None:
        self.already_lingering = already_lingering
        self.enable_succeeds = enable_succeeds
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:2] == ["loginctl", "show-user"]:
            return subprocess.CompletedProcess(
                args, 0, stdout="yes" if self.already_lingering else "no", stderr=""
            )
        if args[:2] == ["loginctl", "enable-linger"]:
            if self.enable_succeeds:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="Interactive authentication required."
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def test_ensure_linger_enabled_is_a_noop_when_already_enabled() -> None:
    runner = LingerRunner(already_lingering=True)

    ensure_linger_enabled(runner)

    assert not any(c[:2] == ["loginctl", "enable-linger"] for c in runner.calls)


def test_ensure_linger_enabled_enables_it_when_not_set() -> None:
    runner = LingerRunner(already_lingering=False)

    ensure_linger_enabled(runner)

    assert any(c[:2] == ["loginctl", "enable-linger"] for c in runner.calls)


def test_ensure_linger_enabled_raises_clear_error_when_enable_fails() -> None:
    runner = LingerRunner(already_lingering=False, enable_succeeds=False)

    try:
        ensure_linger_enabled(runner)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "sudo loginctl enable-linger" in str(e)


def test_resolve_prompt_text_strips_frontmatter_from_persona_file(tmp_path: Path) -> None:
    from buzz_fleet.systemd import resolve_prompt_text

    persona_path = tmp_path / "laravel.persona.md"
    persona_path.write_text(
        "---\n"
        "display_name: Laravel Backend Dev\n"
        "runtime: claude\n"
        "---\n"
        "You are the Laravel dev.\n"
    )
    agent = _agent().model_copy(
        update={"system_prompt_source": SystemPromptSource(kind="persona_file", path=persona_path)}
    )

    assert resolve_prompt_text(agent) == "You are the Laravel dev.\n"


def test_resolve_prompt_text_returns_whole_file_when_no_frontmatter(tmp_path: Path) -> None:
    from buzz_fleet.systemd import resolve_prompt_text

    plain_path = tmp_path / "plain.md"
    plain_path.write_text("Just a plain prompt, no frontmatter.\n")
    agent = _agent().model_copy(
        update={"system_prompt_source": SystemPromptSource(kind="persona_file", path=plain_path)}
    )

    assert resolve_prompt_text(agent) == "Just a plain prompt, no frontmatter.\n"


def test_write_agent_files_emits_model_when_set(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    agent = _agent().model_copy(update={"model": "claude-sonnet-5"})

    write_agent_files(agent, _community(), anthropic_api_key=None, openai_api_key=None)

    env_content = agent_env_path(agent.id).read_text()
    assert "BUZZ_ACP_MODEL=claude-sonnet-5" in env_content


def test_write_agent_files_omits_optional_fields_when_unset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    agent = _agent()

    write_agent_files(agent, _community(), anthropic_api_key=None, openai_api_key=None)

    env_content = agent_env_path(agent.id).read_text()
    for key in (
        "BUZZ_ACP_MODEL",
        "BUZZ_ACP_AGENTS",
        "BUZZ_ACP_IDLE_TIMEOUT",
        "BUZZ_ACP_MAX_TURN_DURATION",
        "BUZZ_ACP_RESPOND_TO",
        "BUZZ_ACP_RESPOND_TO_ALLOWLIST",
        "BUZZ_AUTH_TAG",
    ):
        assert key not in env_content


def test_write_agent_files_emits_auth_tag_when_given(tmp_path: Path, monkeypatch) -> None:
    """Regression test for a real production incident: without BUZZ_AUTH_TAG,
    buzz-acp never attaches a NIP-OA auth tag to its own NIP-42 AUTH event,
    so the relay's `agent_owner_pubkey` column is never populated — a
    third-party kind:9000 add (e.g. a human adding the agent to a channel
    from Desktop) against a channel_add_policy=owner_only agent then fails
    with "policy:owner_only — agent has no owner set", even though every
    visibility event (kind:0/30177/10100) published fine. buzz-acp already
    reads BUZZ_AUTH_TAG and attaches it to its own AUTH event on every
    connect (confirmed in buzz's own source) — buzz-fleet just needed to
    write it into the env file.
    """
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    agent = _agent()
    auth_tag = '["auth","' + "b" * 64 + '","","' + "c" * 128 + '"]'

    write_agent_files(agent, _community(), anthropic_api_key=None, openai_api_key=None, auth_tag=auth_tag)

    env_content = agent_env_path(agent.id).read_text()
    assert f"BUZZ_AUTH_TAG={auth_tag}" in env_content


def test_write_agent_files_emits_parallelism_idle_timeout_max_turn_duration(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    agent = _agent().model_copy(
        update={"parallelism": 3, "idle_timeout_seconds": 120, "max_turn_duration_seconds": 600}
    )

    write_agent_files(agent, _community(), anthropic_api_key=None, openai_api_key=None)

    env_content = agent_env_path(agent.id).read_text()
    assert "BUZZ_ACP_AGENTS=3" in env_content
    assert "BUZZ_ACP_IDLE_TIMEOUT=120" in env_content
    assert "BUZZ_ACP_MAX_TURN_DURATION=600" in env_content


def test_write_agent_files_sets_respond_to_allowlist_mode_when_list_non_empty(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    agent = _agent().model_copy(update={"respond_to_allowlist": ["a" * 64, "b" * 64]})

    write_agent_files(agent, _community(), anthropic_api_key=None, openai_api_key=None)

    env_content = agent_env_path(agent.id).read_text()
    assert f"BUZZ_ACP_RESPOND_TO_ALLOWLIST={'a' * 64},{'b' * 64}" in env_content
    assert "BUZZ_ACP_RESPOND_TO=allowlist" in env_content


def test_write_agent_files_session_and_heartbeat_defaults(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.resolve_adapter_command", lambda harness: "/usr/bin/x")

    write_agent_files(_agent(), _community(), None, None)

    env = agent_env_path("laravel-backend-dev").read_text()
    for line in ("BUZZ_ACP_SESSION_POLICY=thread\n", "BUZZ_ACP_MAX_TURNS_PER_SESSION=40\n", "BUZZ_ACP_HEARTBEAT_INTERVAL=900\n"):
        assert line in env


def test_write_agent_files_session_overrides(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.resolve_adapter_command", lambda harness: "/usr/bin/x")
    agent = _agent().model_copy(update={"session_policy": "channel", "max_turns_per_session": 5, "heartbeat_interval_seconds": 0})

    write_agent_files(agent, _community(), None, None)

    env = agent_env_path("laravel-backend-dev").read_text()
    for line in ("BUZZ_ACP_SESSION_POLICY=channel\n", "BUZZ_ACP_MAX_TURNS_PER_SESSION=5\n", "BUZZ_ACP_HEARTBEAT_INTERVAL=0\n"):
        assert line in env


def test_template_unit_sets_path_and_workdir() -> None:
    from buzz_fleet.buzz_acp import BUZZ_ACP_DIR
    from buzz_fleet.systemd import WORK_DIR

    assert f"Environment=PATH={BUZZ_ACP_DIR}:/usr/local/bin:/usr/bin:/bin" in TEMPLATE_UNIT
    assert f"WorkingDirectory={WORK_DIR}/%i" in TEMPLATE_UNIT


def test_ensure_template_unit_installed_returns_changed_flag(tmp_path: Path, monkeypatch) -> None:
    unit_path = tmp_path / "buzz-agent@.service"
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", unit_path)
    calls: list[list[str]] = []

    class Runner:
        def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    assert ensure_template_unit_installed(Runner()) is True
    assert calls == [["systemctl", "--user", "daemon-reload"]]
    assert ensure_template_unit_installed(Runner()) is False


def test_env_line_single_line_unquoted() -> None:
    from buzz_fleet.systemd import env_line

    assert env_line("BUZZ_RELAY_URL", "wss://buzz.example") == "BUZZ_RELAY_URL=wss://buzz.example"


def test_env_line_quotes_and_escapes_multiline() -> None:
    from buzz_fleet.systemd import env_line

    value = 'line one\nsays "hi" \\ back\nline three'
    assert env_line("K", value) == 'K="line one\nsays \\"hi\\" \\\\ back\nline three"'


def test_write_agent_files_quotes_multiline_team_instructions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.resolve_adapter_command", lambda harness: "/usr/bin/claude-agent-acp")
    agent = _agent().model_copy(update={"team_instructions": "# Rules\n\n- one\n- two"})

    write_agent_files(agent, _community(), None, None)

    assert 'BUZZ_ACP_TEAM_INSTRUCTIONS="# Rules\n\n- one\n- two' in agent_env_path(agent.id).read_text()


def test_write_agent_files_injects_coordination_block(tmp_path: Path, monkeypatch) -> None:
    from buzz_fleet.orchestration.instructions import BLOCK_START

    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.resolve_adapter_command", lambda harness: "/usr/bin/x")

    write_agent_files(_agent().model_copy(update={"team_instructions": None}), _community(), None, None)

    assert BLOCK_START in agent_env_path("laravel-backend-dev").read_text()


def test_write_agent_files_exports_fleet_env_when_known(tmp_path: Path, monkeypatch) -> None:
    from buzz_fleet.orchestration.record import FleetRecord

    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.resolve_adapter_command", lambda harness: "/usr/bin/x")
    community = _community().model_copy(update={
        "fleet_channel_id": "6f1c0000-0000-4000-8000-000000000000",
        "fleet_record": FleetRecord(retrieval_key="r" * 64, created_at=1),
    })

    write_agent_files(_agent(), community, None, None)

    env = agent_env_path("laravel-backend-dev").read_text()
    assert "BUZZ_FLEET_CHANNEL=6f1c0000-0000-4000-8000-000000000000\n" in env
    assert f"BUZZ_FLEET_RETRIEVAL_KEY={'r' * 64}\n" in env


def test_write_agent_files_env_and_mcp_wrapper(tmp_path: Path, monkeypatch) -> None:
    from buzz_fleet.models import McpServer

    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.resolve_adapter_command", lambda harness: "/usr/bin/x")
    agent = _agent().model_copy(update={"env": {"DATABASE_URL": "postgres://x"},
                                        "mcp_server": McpServer(name="boost", command="php", args=["artisan", "boost:mcp"], env={"TOKEN": "t"})})

    write_agent_files(agent, _community(), None, None)

    env = agent_env_path(agent.id).read_text()
    wrapper = tmp_path / "work" / agent.id / "mcp-boost.sh"
    assert "DATABASE_URL=postgres://x\n" in env and f"BUZZ_ACP_MCP_COMMAND={wrapper}\n" in env
    assert wrapper.stat().st_mode & 0o777 == 0o700
    body = wrapper.read_text()
    assert "export TOKEN='t'" in body and "exec 'php' 'artisan' 'boost:mcp'" in body


def test_write_agent_files_bare_mcp_server_writes_no_wrapper(tmp_path: Path, monkeypatch) -> None:
    """A bare command with empty args and env needs no wrapper at all — the
    wrapper exists only because buzz-acp itself cannot pass args/env, so
    when neither is needed BUZZ_ACP_MCP_COMMAND must point straight at the
    command, not at a wrapper script that does nothing but exec it.
    """
    from buzz_fleet.models import McpServer

    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.resolve_adapter_command", lambda harness: "/usr/bin/x")
    agent = _agent().model_copy(update={"mcp_server": McpServer(name="boost", command="php")})

    write_agent_files(agent, _community(), None, None)

    env = agent_env_path(agent.id).read_text()
    assert "BUZZ_ACP_MCP_COMMAND=php\n" in env
    assert write_mcp_wrapper(agent) is None
    assert not (tmp_path / "work" / agent.id / "mcp-boost.sh").exists()


def test_write_mcp_wrapper_returns_none_for_bare_command() -> None:
    from buzz_fleet.models import McpServer

    agent = _agent().model_copy(update={"mcp_server": McpServer(name="boost", command="php")})

    assert write_mcp_wrapper(agent) is None


def test_write_mcp_wrapper_writes_when_only_args_are_set(tmp_path: Path, monkeypatch) -> None:
    """Args alone (no env) still need the wrapper — buzz-acp cannot pass
    them either.
    """
    from buzz_fleet.models import McpServer

    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    agent = _agent().model_copy(update={"mcp_server": McpServer(name="boost", command="php", args=["artisan"])})

    path = write_mcp_wrapper(agent)

    assert path is not None and path.exists()


def test_write_mcp_wrapper_defends_against_traversal_even_if_validation_is_bypassed(
    tmp_path: Path, monkeypatch
) -> None:
    """`McpServer.name` is validated at construction time (see
    test_models.py), so this can no longer happen through normal
    construction — this test demonstrates the second, independent guard in
    `_mcp_wrapper_path` itself, for a future code path that bypasses model
    validation (e.g. `model_construct`, or a direct attribute assignment
    after construction — pydantic does not re-validate on assignment by
    default). Confirms the traversal attempt fails loudly rather than
    silently writing outside the agent's own directory.
    """
    import pytest

    from buzz_fleet.models import McpServer

    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    # Non-empty args so this actually reaches wrapper-path construction
    # rather than short-circuiting via the "bare command" fast path.
    bypassed = McpServer.model_construct(name="../../../pwned", command="php", args=["artisan"], env={})
    agent = _agent().model_copy(update={"mcp_server": bypassed})

    with pytest.raises(ValueError, match="unsafe MCP server name"):
        write_mcp_wrapper(agent)

    # And no file was written anywhere outside (or inside) the work dir.
    assert not (tmp_path / "pwned.sh").exists()
    work_dir = tmp_path / "work"
    assert not work_dir.exists() or not any(work_dir.rglob("*.sh"))


def test_write_agent_files_pi_gets_private_agent_dir_and_mcp_json(tmp_path: Path, monkeypatch) -> None:
    import json as _json

    from buzz_fleet.models import McpServer

    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.resolve_adapter_command", lambda harness: "/usr/bin/pi-acp")
    # Isolate from the real ~/.local/share/buzz-fleet/pi-agent-template/ —
    # without this, _write_pi_agent_dir stats the real host's shared
    # template dir, which is harmless while empty but host-dependent the
    # moment `harness install pi` has ever actually been run there.
    monkeypatch.setattr("buzz_fleet.systemd.PI_AGENT_TEMPLATE_DIR", tmp_path / "pi-template-unused")
    agent = _agent().model_copy(update={"harness": "pi", "mcp_server": McpServer(name="boost", command="php", args=["artisan", "boost:mcp"])})

    write_agent_files(agent, _community(), None, None)

    pi_dir = tmp_path / "work" / agent.id / ".pi-agent"
    assert f"PI_CODING_AGENT_DIR={pi_dir}\n" in agent_env_path(agent.id).read_text()
    settings = _json.loads((pi_dir / "settings.json").read_text())
    assert settings["defaultProjectTrust"] == "always" and settings["packages"][0].startswith("npm:pi-mcp-adapter@")
    assert _json.loads((pi_dir / "mcp.json").read_text())["mcpServers"]["boost"]["args"] == ["artisan", "boost:mcp"]


def test_write_mcp_wrapper_quotes_values_with_spaces(tmp_path: Path, monkeypatch) -> None:
    """Discriminating test for the `shlex.quote` requirement: an unquoted
    value containing a space would silently split into two shell words,
    corrupting the exported env var or the exec'd command line.
    """
    from buzz_fleet.models import McpServer

    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    agent = _agent().model_copy(update={
        "mcp_server": McpServer(name="boost", command="php", args=["artisan", "boost:mcp"], env={"NOTE": "has space"})
    })

    from buzz_fleet.systemd import write_mcp_wrapper

    path = write_mcp_wrapper(agent)

    assert path is not None
    body = path.read_text()
    assert "export NOTE='has space'" in body


def test_write_agent_files_no_mcp_server_writes_no_wrapper_or_mcp_command(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.resolve_adapter_command", lambda harness: "/usr/bin/x")

    write_agent_files(_agent(), _community(), None, None)

    env = agent_env_path("laravel-backend-dev").read_text()
    assert "BUZZ_ACP_MCP_COMMAND" not in env
    assert not (tmp_path / "work" / "laravel-backend-dev" / "mcp-boost.sh").exists()


def test_write_agent_files_clearing_mcp_server_removes_stale_wrapper(tmp_path: Path, monkeypatch) -> None:
    # Finding 2 (final review): `write_agent_files` only ever wrote a wrapper
    # when `agent.mcp_server is not None` and never removed a stale one — the
    # TUI's documented "you can clear it" path (blank the MCP fields and save)
    # left the secret-bearing `mcp-<name>.sh` on disk forever.
    from buzz_fleet.models import McpServer

    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.resolve_adapter_command", lambda harness: "/usr/bin/x")
    agent = _agent().model_copy(update={
        "mcp_server": McpServer(name="boost", command="php", args=["artisan", "boost:mcp"], env={"TOKEN": "t"})
    })
    write_agent_files(agent, _community(), None, None)
    wrapper = tmp_path / "work" / agent.id / "mcp-boost.sh"
    assert wrapper.exists()

    cleared = agent.model_copy(update={"mcp_server": None})
    write_agent_files(cleared, _community(), None, None)

    assert not wrapper.exists()
    env = agent_env_path(agent.id).read_text()
    assert "BUZZ_ACP_MCP_COMMAND" not in env


def test_write_agent_files_renaming_mcp_server_removes_old_wrapper(tmp_path: Path, monkeypatch) -> None:
    # Finding 2 (final review): renaming an MCP server orphans
    # `mcp-<oldname>.sh` forever -- the new name gets its own wrapper, but
    # nothing ever removed the old one.
    from buzz_fleet.models import McpServer

    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.resolve_adapter_command", lambda harness: "/usr/bin/x")
    agent = _agent().model_copy(update={
        "mcp_server": McpServer(name="boost", command="php", args=["artisan", "boost:mcp"], env={"TOKEN": "t"})
    })
    write_agent_files(agent, _community(), None, None)
    old_wrapper = tmp_path / "work" / agent.id / "mcp-boost.sh"
    assert old_wrapper.exists()

    renamed = agent.model_copy(update={
        "mcp_server": McpServer(name="renamed", command="php", args=["artisan", "boost:mcp"], env={"TOKEN": "t"})
    })
    write_agent_files(renamed, _community(), None, None)

    new_wrapper = tmp_path / "work" / agent.id / "mcp-renamed.sh"
    assert new_wrapper.exists() and not old_wrapper.exists()


def test_write_agent_files_editing_mcp_server_to_bare_command_removes_old_wrapper(tmp_path: Path, monkeypatch) -> None:
    # Finding 2 (final review): editing the same-named server down to a bare
    # command (no args/env) makes `write_mcp_wrapper` correctly return None --
    # but a wrapper written before the edit must still be cleaned up, or
    # buzz-acp keeps a secret script on disk it no longer even points at.
    from buzz_fleet.models import McpServer

    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.resolve_adapter_command", lambda harness: "/usr/bin/x")
    agent = _agent().model_copy(update={
        "mcp_server": McpServer(name="boost", command="php", args=["artisan", "boost:mcp"], env={"TOKEN": "t"})
    })
    write_agent_files(agent, _community(), None, None)
    wrapper = tmp_path / "work" / agent.id / "mcp-boost.sh"
    assert wrapper.exists()

    bare = agent.model_copy(update={"mcp_server": McpServer(name="boost", command="php")})
    write_agent_files(bare, _community(), None, None)

    assert not wrapper.exists()
    env = agent_env_path(agent.id).read_text()
    assert "BUZZ_ACP_MCP_COMMAND=php\n" in env


def test_write_agent_files_pi_without_mcp_server_writes_no_mcp_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.resolve_adapter_command", lambda harness: "/usr/bin/pi-acp")
    # See the sibling test above for why this must not touch the real host's
    # shared template dir.
    monkeypatch.setattr("buzz_fleet.systemd.PI_AGENT_TEMPLATE_DIR", tmp_path / "pi-template-unused")
    agent = _agent().model_copy(update={"harness": "pi"})

    write_agent_files(agent, _community(), None, None)

    pi_dir = tmp_path / "work" / agent.id / ".pi-agent"
    assert pi_dir.is_dir()
    assert not (pi_dir / "mcp.json").exists()


def test_write_agent_files_pi_copies_shared_template_npm_dir(tmp_path: Path, monkeypatch) -> None:
    """The whole point of the shared template dir (harnesses.install_adapter)
    is that a brand-new Pi agent's first turn needs no network — verify the
    template's npm/ payload actually lands inside the agent's own
    .pi-agent/npm/ rather than being left behind.
    """
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.WORK_DIR", tmp_path / "work")
    monkeypatch.setattr("buzz_fleet.systemd.resolve_adapter_command", lambda harness: "/usr/bin/pi-acp")
    template_dir = tmp_path / "pi-template"
    (template_dir / "npm" / "pi-mcp-adapter").mkdir(parents=True)
    (template_dir / "npm" / "pi-mcp-adapter" / "package.json").write_text("{}")
    monkeypatch.setattr("buzz_fleet.systemd.PI_AGENT_TEMPLATE_DIR", template_dir)
    agent = _agent().model_copy(update={"harness": "pi"})

    write_agent_files(agent, _community(), None, None)

    copied = tmp_path / "work" / agent.id / ".pi-agent" / "npm" / "pi-mcp-adapter" / "package.json"
    assert copied.is_file()
