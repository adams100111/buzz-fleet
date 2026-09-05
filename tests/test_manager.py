import json
import subprocess
from pathlib import Path

import pytest

from buzz_fleet.manager import AgentManager
from buzz_fleet.models import Community, SystemPromptSource
from buzz_fleet.systemd import agent_env_path, agent_prompt_path

# Visibility subcommands whose FakeRunner response is a plain {"ok": True} —
# collected into a set (rather than one elif per subcommand) so the dispatch
# stays a single readable branch instead of N branches with identical bodies.
_SIGNER_OK_SUBCOMMANDS = {
    ("buzz-fleet-signer", "publish-agent-profile"),
    ("buzz-fleet-signer", "publish-managed-agent"),
    ("buzz-fleet-signer", "retract-managed-agent"),
    ("buzz-fleet-signer", "publish-agent-add-policy"),
    ("buzz-fleet-signer", "join-channel"),
    ("buzz-fleet-signer", "leave-channel"),
    ("buzz-fleet-signer", "archive-agent"),
}


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:2] == ["buzz-fleet-signer", "generate-key"]:
            stdout = json.dumps({"public_key": "ab" * 32, "secret_key": "nsec1agent"})
        elif args[:2] == ["buzz-fleet-signer", "pubkey-from-nsec"]:
            stdout = json.dumps({"ok": True, "public_key": "c" * 64})
        elif args[:2] == ["buzz-fleet-signer", "compute-auth-tag"]:
            stdout = json.dumps({"ok": True, "auth_tag": json.dumps(["auth", "d" * 64, "", "e" * 128])})
        elif "add-member" in args or "remove-member" in args or tuple(args[:2]) in _SIGNER_OK_SUBCOMMANDS:
            stdout = json.dumps({"ok": True})
        elif args[:2] == ["loginctl", "show-user"]:
            stdout = "yes"  # already lingering — the common case in these tests
        elif args[:2] == ["loginctl", "enable-linger"]:
            stdout = ""
        else:
            stdout = "active\n"
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")


def _community() -> Community:
    return Community(id="eltahir", relay_url="wss://buzz.eltahir.me", relay_admin_nsec="nsec1admin")


@pytest.fixture(autouse=True)
def _buzz_acp_already_installed(tmp_path: Path, monkeypatch) -> None:
    """Every test in this file exercises `create_agent`, which now calls
    `ensure_runtime_ready()` -> `buzz_acp.ensure_buzz_acp_installed()`.
    Without this fixture, every test run would perform a REAL network
    download into the real user's home directory — pre-seed an
    already-installed, executable stub so it's a safe no-op by default.
    Tests that specifically want the "just installed" self-heal path
    override `buzz_acp.ensure_buzz_acp_installed` directly instead.
    """
    from buzz_fleet import buzz_acp

    acp_dir = tmp_path / "buzz-acp-bin"
    acp_dir.mkdir()
    stub = acp_dir / "buzz-acp"
    stub.write_bytes(b"stub")
    stub.chmod(0o755)
    monkeypatch.setattr(buzz_acp, "BUZZ_ACP_DIR", acp_dir)
    monkeypatch.setattr(buzz_acp, "BUZZ_ACP_PATH", stub)


def test_create_agent_mints_key_registers_and_starts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())

    agent = manager.create_agent(
        display_name="Laravel Backend Dev",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="You are the dev."),
    )

    assert agent.id == "laravel-backend-dev"
    assert agent.public_key == "ab" * 32
    add_member_call = next(c for c in runner.calls if "add-member" in c)
    assert "--pubkey" in add_member_call and "ab" * 32 in add_member_call
    assert ["systemctl", "--user", "enable", "--now", "buzz-agent@laravel-backend-dev"] in runner.calls
    assert manager.list_agents() == [agent]


def test_delete_agent_removes_member_and_stops_unit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Throwaway",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )

    manager.delete_agent(agent.id)

    assert ["systemctl", "--user", "disable", "--now", "buzz-agent@throwaway"] in runner.calls
    assert any("remove-member" in c for c in runner.calls)
    assert manager.list_agents() == []


def test_update_agent_restarts_without_re_registering(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Throwaway",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    add_member_calls_before = len([c for c in runner.calls if "add-member" in c])

    updated = manager.update_agent(agent.id, system_prompt_source=SystemPromptSource(kind="inline", text="y"))

    add_member_calls_after = len([c for c in runner.calls if "add-member" in c])
    assert add_member_calls_after == add_member_calls_before
    assert ["systemctl", "--user", "restart", "buzz-agent@throwaway"] in runner.calls
    assert updated.system_prompt_source.text == "y"


def test_create_agent_with_missing_persona_file_fails_before_add_member(tmp_path: Path, monkeypatch) -> None:
    """Regression test for Fix 3.

    A missing/invalid persona_file path must fail loudly before any relay-side
    effect (add-member) happens — otherwise the relay membership is published
    but never recorded locally, orphaning it with no way to revoke it.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())

    with pytest.raises(FileNotFoundError):
        manager.create_agent(
            display_name="Broken Persona",
            harness="claude",
            system_prompt_source=SystemPromptSource(kind="persona_file", path=Path("/nonexistent/persona.md")),
        )

    assert not any("add-member" in c for c in runner.calls)


def test_delete_agent_removes_env_and_prompt_files(tmp_path: Path, monkeypatch) -> None:
    """Regression test for Fix 4: deleting an agent must remove its
    private-key-bearing .env file and its .prompt.md file, not just the state
    JSON — otherwise the secret survives "deletion" on disk.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Throwaway",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    assert agent_env_path(agent.id).exists()
    assert agent_prompt_path(agent.id).exists()

    manager.delete_agent(agent.id)

    assert not agent_env_path(agent.id).exists()
    assert not agent_prompt_path(agent.id).exists()


def test_update_agent_preserves_previously_set_api_keys(tmp_path: Path, monkeypatch) -> None:
    """Regression test for Fix 9: update_agent must not wipe a previously-set
    ANTHROPIC_API_KEY/OPENAI_API_KEY when the update doesn't touch keys at all.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Keyed Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
        anthropic_api_key="sk-ant-test",
    )
    assert "ANTHROPIC_API_KEY=sk-ant-test" in agent_env_path(agent.id).read_text()

    manager.update_agent(agent.id, display_name="Keyed Agent Renamed")

    env_content = agent_env_path(agent.id).read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-test" in env_content


class FailingEnableRunner(FakeRunner):
    """Fails only `systemctl ... enable --now ...` — e.g. no `loginctl
    enable-linger` yet on a fresh host, the literal first-run condition.
    """

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if "enable" in args and "--now" in args:
            self.calls.append(args)
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="Failed to connect to bus: No medium found"
            )
        return super().run(args)


def test_create_agent_is_recorded_locally_even_if_enable_now_fails(tmp_path: Path, monkeypatch) -> None:
    """Regression test: a failed `enable_now` must not orphan the relay
    membership + private-key env file that were already published/written —
    the agent must still be discoverable (and therefore deletable/retryable)
    via `list_agents()` even though its unit never actually started.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FailingEnableRunner()
    manager = AgentManager(runner, _community())

    with pytest.raises(RuntimeError):
        manager.create_agent(
            display_name="Orphan Test",
            harness="claude",
            system_prompt_source=SystemPromptSource(kind="inline", text="x"),
        )

    recorded = manager.list_agents()
    assert len(recorded) == 1
    assert recorded[0].id == "orphan-test"
    add_member_call = next(c for c in runner.calls if "add-member" in c)
    assert add_member_call  # membership was published — the local record above is what makes it revocable


class LingerCantEnableRunner(FakeRunner):
    """Fails only `loginctl enable-linger` — e.g. a non-active SSH session
    without polkit permission to self-enable it.
    """

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["loginctl", "show-user"]:
            self.calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="no", stderr="")
        if args[:2] == ["loginctl", "enable-linger"]:
            self.calls.append(args)
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="Interactive authentication required."
            )
        return super().run(args)


def test_create_agent_fails_before_any_side_effect_when_linger_cannot_be_enabled(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression test: if lingering can't be auto-enabled (needs a manual
    one-time `sudo`), create_agent must fail immediately with a clear
    message — before minting a key, publishing relay membership, or writing
    any files — not fail confusingly later at enable_now.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = LingerCantEnableRunner()
    manager = AgentManager(runner, _community())

    with pytest.raises(RuntimeError, match="sudo loginctl enable-linger"):
        manager.create_agent(
            display_name="Never Created",
            harness="claude",
            system_prompt_source=SystemPromptSource(kind="inline", text="x"),
        )

    assert manager.list_agents() == []
    assert not any("generate-key" in c for c in runner.calls)
    assert not any("add-member" in c for c in runner.calls)


def test_create_agent_stores_new_optional_fields(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())

    agent = manager.create_agent(
        display_name="Test Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="hi"),
        model="claude-sonnet-5",
        parallelism=3,
        idle_timeout_seconds=120,
        max_turn_duration_seconds=600,
        respond_to_allowlist=["a" * 64],
    )

    assert agent.model == "claude-sonnet-5"
    assert agent.parallelism == 3
    assert agent.idle_timeout_seconds == 120
    assert agent.max_turn_duration_seconds == 600
    assert agent.respond_to_allowlist == ["a" * 64]


def test_create_agent_publishes_visibility_events_in_order(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())

    agent = manager.create_agent(
        display_name="Visible Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="You are visible."),
        channel_ids=["11111111-1111-1111-1111-111111111111"],
    )

    assert agent.visibility_managed is True
    assert agent.visibility_state.profile_published is True
    assert agent.visibility_state.managed_agent_published is True
    assert agent.visibility_state.add_policy_published is True
    assert agent.visibility_state.channels["11111111-1111-1111-1111-111111111111"] == "joined"
    subcommands = [c[1] for c in runner.calls if c[0] == "buzz-fleet-signer"]
    assert subcommands.index("compute-auth-tag") < subcommands.index("publish-agent-profile")
    assert "join-channel" in subcommands


def test_create_agent_records_permanent_channel_error_without_failing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")

    class BadChannelRunner(FakeRunner):
        def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:2] == ["buzz-fleet-signer", "join-channel"]:
                self.calls.append(args)
                return subprocess.CompletedProcess(
                    args, 0, stdout=json.dumps({"ok": False, "error": "invalid: channel not found"}), stderr=""
                )
            return super().run(args)

    runner = BadChannelRunner()
    manager = AgentManager(runner, _community())

    agent = manager.create_agent(
        display_name="Bad Channel Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
        channel_ids=["22222222-2222-2222-2222-222222222222"],
    )

    # create_agent must not raise despite the channel join failing.
    assert agent.visibility_state.channel_errors["22222222-2222-2222-2222-222222222222"] == (
        "join-channel failed: invalid: channel not found"
    )
    assert agent.visibility_state.channels["22222222-2222-2222-2222-222222222222"] == "error"
    assert agent.visibility_state.profile_published is True  # unrelated steps still succeeded


def test_ensure_runtime_ready_restarts_existing_agents_when_buzz_acp_just_installed(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression test for the real incident: buzz-fleet never installed
    buzz-acp itself, so every agent's unit crash-looped forever with no way
    to notice short of a human running `systemctl --user status` by hand.
    ensure_runtime_ready() must restart already-existing agents the moment
    it (re)installs buzz-acp, so a previously-broken agent heals itself the
    next time the dashboard loads or any agent command runs — never a
    restart when buzz-acp was already fine (that would restart healthy
    agents on every single call, not just the one that matters).
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    first = manager.create_agent(
        display_name="First Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    second = manager.create_agent(
        display_name="Second Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    runner.calls.clear()

    from buzz_fleet import buzz_acp

    monkeypatch.setattr(buzz_acp, "ensure_buzz_acp_installed", lambda: True)

    manager.ensure_runtime_ready()

    assert ["systemctl", "--user", "restart", f"buzz-agent@{first.id}"] in runner.calls
    assert ["systemctl", "--user", "restart", f"buzz-agent@{second.id}"] in runner.calls


def test_ensure_runtime_ready_does_not_restart_agents_when_buzz_acp_already_installed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    manager.create_agent(
        display_name="First Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    runner.calls.clear()

    manager.ensure_runtime_ready()

    assert not any(c[:3] == ["systemctl", "--user", "restart"] for c in runner.calls)


def test_ensure_runtime_ready_refreshes_agent_whose_adapter_command_is_now_resolvable(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression test for the real, second half of the incident: installing

    a harness adapter *after* an agent already exists doesn't help that
    agent on its own — its .env file still has the stale command written
    before the adapter existed, and systemd's own PATH won't pick up a
    version-manager-installed binary regardless. ensure_runtime_ready()
    must notice the now-resolvable command differs from what's on disk and
    heal it, without needing buzz-acp itself to have just been installed.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")

    from buzz_fleet import harnesses

    monkeypatch.setattr(harnesses.shutil, "which", lambda cmd: None)  # not resolvable at create time
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Codex Bot",
        harness="codex",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    env_path = agent_env_path(agent.id)
    assert "BUZZ_ACP_AGENT_COMMAND=codex-acp" in env_path.read_text()
    runner.calls.clear()

    # The adapter is "installed" now — resolvable to an absolute path.
    monkeypatch.setattr(
        harnesses.shutil,
        "which",
        lambda cmd: "/home/dev/.local/share/mise/installs/node/22/bin/codex-acp"
        if cmd == "codex-acp"
        else None,
    )

    manager.ensure_runtime_ready()

    assert (
        "BUZZ_ACP_AGENT_COMMAND=/home/dev/.local/share/mise/installs/node/22/bin/codex-acp"
        in env_path.read_text()
    )
    assert ["systemctl", "--user", "restart", f"buzz-agent@{agent.id}"] in runner.calls


def test_ensure_runtime_ready_continues_healing_other_agents_if_one_restart_fails(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")

    class FlakyRestartRunner(FakeRunner):
        def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:3] == ["systemctl", "--user", "restart"] and "first-agent" in args[3]:
                self.calls.append(args)
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")
            return super().run(args)

    runner = FlakyRestartRunner()
    manager = AgentManager(runner, _community())
    manager.create_agent(
        display_name="First Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    second = manager.create_agent(
        display_name="Second Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    runner.calls.clear()

    from buzz_fleet import buzz_acp

    monkeypatch.setattr(buzz_acp, "ensure_buzz_acp_installed", lambda: True)

    manager.ensure_runtime_ready()  # must not raise despite the first restart failing

    assert ["systemctl", "--user", "restart", f"buzz-agent@{second.id}"] in runner.calls


def test_ensure_runtime_ready_never_touches_agent_with_visibility_managed_false(tmp_path: Path, monkeypatch) -> None:
    """Regression test for the old-agent exemption — the single most
    important invariant in this feature. An agent created before this
    feature existed (visibility_managed=False) must never have any
    visibility signer subcommand invoked against it, ever.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Old Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    # Simulate a pre-feature record: flip visibility_managed back to False
    # and re-save, as if this agent had been loaded from disk before the
    # field existed (Pydantic's own default, never explicitly True).
    from buzz_fleet import state as state_module

    old_style = agent.model_copy(update={"visibility_managed": False})
    state_module.save_agent(old_style)
    runner.calls.clear()

    manager.ensure_runtime_ready()

    # archive-agent is delete-only (never called by ensure_runtime_ready/
    # _sync_visibility) and correctly excluded; retract-managed-agent is a
    # real subcommand a future unpublish path could call and must be
    # included so this test still catches that regression if it ever
    # happens.
    visibility_subcommands = {
        "compute-auth-tag",
        "publish-agent-profile",
        "publish-managed-agent",
        "retract-managed-agent",
        "publish-agent-add-policy",
        "join-channel",
        "leave-channel",
    }
    assert not any(len(c) > 1 and c[1] in visibility_subcommands for c in runner.calls)


def test_ensure_runtime_ready_retries_a_still_pending_visibility_step(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")

    class FlakyProfileRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.profile_calls = 0

        def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:2] == ["buzz-fleet-signer", "publish-agent-profile"]:
                self.profile_calls += 1
                self.calls.append(args)
                if self.profile_calls == 1:
                    return subprocess.CompletedProcess(args, 1, stdout="", stderr="connection refused")
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"ok": True}), stderr="")
            return super().run(args)

    runner = FlakyProfileRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Flaky Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    assert agent.visibility_state.profile_published is False  # first attempt failed transiently

    manager.ensure_runtime_ready()

    reloaded = next(a for a in manager.list_agents() if a.id == agent.id)
    assert reloaded.visibility_state.profile_published is True


def test_update_agent_republishes_managed_agent_on_display_name_change(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Old Name",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    runner.calls.clear()

    updated = manager.update_agent(agent.id, display_name="New Name")

    assert updated.visibility_state.profile_published is True
    assert updated.visibility_state.managed_agent_published is True
    subcommands = [c[1] for c in runner.calls if c[0] == "buzz-fleet-signer"]
    assert "publish-agent-profile" in subcommands
    assert "publish-managed-agent" in subcommands


def test_update_agent_joins_new_channel_and_leaves_removed_one(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Channel Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
        channel_ids=["11111111-1111-1111-1111-111111111111"],
    )
    runner.calls.clear()

    updated = manager.update_agent(
        agent.id, channel_ids=["22222222-2222-2222-2222-222222222222"]
    )

    leave_calls = [c for c in runner.calls if c[:2] == ["buzz-fleet-signer", "leave-channel"]]
    join_calls = [c for c in runner.calls if c[:2] == ["buzz-fleet-signer", "join-channel"]]
    assert any("11111111-1111-1111-1111-111111111111" in c for c in leave_calls)
    assert any("22222222-2222-2222-2222-222222222222" in c for c in join_calls)
    assert "11111111-1111-1111-1111-111111111111" not in updated.visibility_state.channels
    assert updated.visibility_state.channels["22222222-2222-2222-2222-222222222222"] == "joined"


def test_update_agent_does_not_touch_visibility_for_old_agent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Old Name",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    from buzz_fleet import state as state_module

    old_style = agent.model_copy(update={"visibility_managed": False})
    state_module.save_agent(old_style)
    runner.calls.clear()

    manager.update_agent(agent.id, display_name="New Name")

    visibility_subcommands = {"compute-auth-tag", "publish-agent-profile", "publish-managed-agent"}
    assert not any(len(c) > 1 and c[1] in visibility_subcommands for c in runner.calls)


def test_update_agent_with_unchanged_display_name_does_not_republish(tmp_path: Path, monkeypatch) -> None:
    """Regression test for Item 6: the TUI always submits the entire form on
    every save, so `update_agent` must only reset/re-publish a visibility
    step when the new value actually differs from the current one — not
    merely because the field was present in `changes`. Presence-only checks
    force a republish (and silently clear any recorded permanent error) on
    every TUI edit, even a no-op one.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Stable Name",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    assert agent.visibility_state.profile_published is True
    assert agent.visibility_state.managed_agent_published is True
    runner.calls.clear()

    updated = manager.update_agent(agent.id, display_name="Stable Name")

    subcommands = [c[1] for c in runner.calls if c[0] == "buzz-fleet-signer"]
    assert "publish-agent-profile" not in subcommands
    assert "publish-managed-agent" not in subcommands
    assert updated.visibility_state.profile_published is True
    assert updated.visibility_state.managed_agent_published is True


def test_update_agent_drops_caller_supplied_visibility_managed(tmp_path: Path, monkeypatch) -> None:
    """Regression test for Item 7: `visibility_managed` is the single most
    safety-critical invariant in the visibility feature (it permanently
    exempts pre-existing agents from any retroactive backfill). No current
    caller passes it through `update_agent`, but it must be silently
    dropped — never honored, and never an error either — so a future/
    careless caller can't flip it.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Old Name",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    from buzz_fleet import state as state_module

    old_style = agent.model_copy(update={"visibility_managed": False})
    state_module.save_agent(old_style)

    updated = manager.update_agent(old_style.id, visibility_managed=True, display_name="New Name")

    assert updated.visibility_managed is False
    assert updated.display_name == "New Name"


def test_delete_agent_leaves_channels_retracts_and_archives(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Doomed Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
        channel_ids=[
            "33333333-3333-3333-3333-333333333333",
            "44444444-4444-4444-4444-444444444444",
        ],
    )
    runner.calls.clear()

    manager.delete_agent(agent.id)

    leave_calls = [c for c in runner.calls if c[:2] == ["buzz-fleet-signer", "leave-channel"]]
    # Both channels must be left, not just the first — proves the loop
    # actually iterates every channel_id rather than leaving one and
    # stopping (the exact regression a single-channel test can't catch).
    assert any("33333333-3333-3333-3333-333333333333" in c for c in leave_calls)
    assert any("44444444-4444-4444-4444-444444444444" in c for c in leave_calls)
    subcommands = [c[1] for c in runner.calls if c[0] == "buzz-fleet-signer"]
    assert "retract-managed-agent" in subcommands
    assert "archive-agent" in subcommands
    archive_call = next(c for c in runner.calls if c[:2] == ["buzz-fleet-signer", "archive-agent"])
    assert "--owner-nsec" in archive_call
    assert "retired" in archive_call


def test_delete_agent_continues_leaving_other_channels_if_one_leave_fails(tmp_path: Path, monkeypatch) -> None:
    """Regression test: a failure leaving one channel must not stop the
    loop before it reaches the others, and must not block retract/archive
    afterward — this is the specific fault-isolation behavior a
    single-channel test cannot exercise.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")

    class FlakyLeaveRunner(FakeRunner):
        def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:2] == ["buzz-fleet-signer", "leave-channel"] and "33333333-3333-3333-3333-333333333333" in args:
                self.calls.append(args)
                return subprocess.CompletedProcess(
                    args, 0, stdout=json.dumps({"ok": False, "error": "invalid: channel not found"}), stderr=""
                )
            return super().run(args)

    runner = FlakyLeaveRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Doomed Agent Two",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
        channel_ids=[
            "33333333-3333-3333-3333-333333333333",
            "44444444-4444-4444-4444-444444444444",
        ],
    )
    runner.calls.clear()

    manager.delete_agent(agent.id)  # must not raise despite the first leave-channel failing

    leave_calls = [c for c in runner.calls if c[:2] == ["buzz-fleet-signer", "leave-channel"]]
    assert any("44444444-4444-4444-4444-444444444444" in c for c in leave_calls)
    subcommands = [c[1] for c in runner.calls if c[0] == "buzz-fleet-signer"]
    assert "retract-managed-agent" in subcommands
    assert "archive-agent" in subcommands


def test_ensure_runtime_ready_survives_deleted_persona_file(tmp_path: Path, monkeypatch) -> None:
    """Regression test for Item 1: a persona_file whose path is moved/deleted
    after agent creation makes `systemd.resolve_prompt_text` (called via
    `visibility.managed_agent_content`) raise `FileNotFoundError`, an
    `OSError` subclass NOT covered by the original
    `except (RuntimeError, json.JSONDecodeError, KeyError)` tuple in
    `_sync_visibility`'s managed-agent step. Since `_sync_visibility` runs
    unconditionally at the top of `ensure_runtime_ready`'s per-agent loop,
    an uncaught `FileNotFoundError` here used to crash `agent list`, the
    dashboard refresh, and every future `create_agent` call. This must
    instead be swallowed and classified as a transient failure (the file
    could reappear), leaving `managed_agent_published=False` and
    `managed_agent_error=None` so a future call retries it.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    persona_path = tmp_path / "persona.md"
    persona_path.write_text("You are a persona-backed agent.")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Persona Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="persona_file", path=persona_path),
    )
    assert agent.visibility_state.managed_agent_published is True  # published fine while the file existed

    # Simulate the file being moved/deleted after creation, and force a
    # re-publish attempt by clearing the previously-recorded success.
    persona_path.unlink()
    from buzz_fleet import state as state_module

    stale = agent.model_copy(
        update={"visibility_state": agent.visibility_state.model_copy(update={"managed_agent_published": False})}
    )
    state_module.save_agent(stale)

    manager.ensure_runtime_ready()  # must not raise despite the missing persona file

    reloaded = next(a for a in manager.list_agents() if a.id == agent.id)
    assert reloaded.visibility_state.managed_agent_published is False
    assert reloaded.visibility_state.managed_agent_error is None


def test_delete_agent_skips_visibility_teardown_for_old_agent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.TEMPLATE_UNIT_PATH", tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Old Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    from buzz_fleet import state as state_module

    old_style = agent.model_copy(update={"visibility_managed": False})
    state_module.save_agent(old_style)
    runner.calls.clear()

    manager.delete_agent(agent.id)

    visibility_subcommands = {"retract-managed-agent", "archive-agent", "leave-channel"}
    assert not any(len(c) > 1 and c[1] in visibility_subcommands for c in runner.calls)
