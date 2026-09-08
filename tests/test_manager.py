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


FLEET = "6f1c0000-0000-4000-8000-000000000000"


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.channels: list[dict] = []
        self.members: list[dict] = []

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:2] == ["buzz-fleet-signer", "generate-key"]:
            stdout = json.dumps({"public_key": "ab" * 32, "secret_key": "nsec1agent"})
        elif args[:2] == ["buzz-fleet-signer", "pubkey-from-nsec"]:
            stdout = json.dumps({"ok": True, "public_key": "c" * 64})
        elif args[:2] == ["buzz-fleet-signer", "compute-auth-tag"]:
            stdout = json.dumps({"ok": True, "auth_tag": json.dumps(["auth", "d" * 64, "", "e" * 128])})
        elif args[:2] == ["buzz-fleet-signer", "read-channel-meta"]:
            stdout = json.dumps({"ok": True, "channels": self.channels})
        elif args[:2] == ["buzz-fleet-signer", "create-channel"]:
            stdout = json.dumps({"ok": True, "channel_id": FLEET})
        elif args[:2] == ["buzz-fleet-signer", "write-channel-about"]:
            stdout = json.dumps({"ok": True})
        elif args[:2] == ["buzz-fleet-signer", "channel-members"]:
            stdout = json.dumps({"ok": True, "members": self.members})
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
    monkeypatch.setattr(buzz_acp, "buzz_acp_dir", lambda: acp_dir)
    monkeypatch.setattr(buzz_acp, "buzz_acp_path", lambda: stub)
    monkeypatch.setattr(buzz_acp, "buzz_cli_path", lambda: acp_dir / "buzz")
    # write_agent_files() now also creates WORK_DIR/<agent-id> as the unit's
    # WorkingDirectory — keep that off the real home directory too.
    monkeypatch.setattr("buzz_fleet.systemd.work_dir", lambda key: tmp_path / "work" / key)


def test_create_agent_mints_key_registers_and_starts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())

    agent = manager.create_agent(
        display_name="Laravel Backend Dev",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="You are the dev."),
    )

    assert agent.id == "laravel-backend-dev"
    assert agent.public_key == "ab" * 32
    # Deliberately NOT a direct relay member (kind:9030) — see the comment in
    # create_agent: a direct member short-circuits the relay's own membership
    # check before it ever looks at the NIP-OA auth tag, which would silently
    # break the owner_only channel-add-policy backfill. Matches Desktop's own
    # agent-creation flow, which never adds agents as direct relay members.
    assert not any("add-member" in c for c in runner.calls)
    assert ["systemctl", "--user", "enable", "--now", "buzz-agent@eltahir:laravel-backend-dev.service"] in runner.calls
    assert manager.list_agents() == [agent]


def test_create_agent_writes_env_and_mcp_server_into_agent_files(tmp_path: Path, monkeypatch) -> None:
    """End-to-end (through the manager, not systemd directly): env/mcp_server
    passed to create_agent must reach the written .env file with real
    secret values, and the MCP wrapper must exist.
    """
    from buzz_fleet.models import McpServer

    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())

    agent = manager.create_agent(
        display_name="Laravel Backend Dev",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="You are the dev."),
        env={"DATABASE_URL": "postgres://x"},
        mcp_server=McpServer(name="boost", command="php", args=["artisan", "boost:mcp"]),
    )

    env_text = agent_env_path(f"eltahir:{agent.id}").read_text()
    assert "DATABASE_URL=postgres://x\n" in env_text
    assert "BUZZ_ACP_MCP_COMMAND=" in env_text
    wrapper = tmp_path / "work" / f"eltahir:{agent.id}" / "mcp-boost.sh"
    assert wrapper.is_file()
    # And it round-tripped through state as a real SecretStr, not the
    # masked literal "**********".
    reloaded = manager.list_agents()[0]
    assert reloaded.env is not None
    assert reloaded.env["DATABASE_URL"].get_secret_value() == "postgres://x"


def test_update_agent_rejects_unsafe_env_key(tmp_path: Path, monkeypatch) -> None:
    # Finding 4 (final review), extended: Agent.env's field_validator
    # (models.validate_env_key) never runs for update_agent, because
    # `current.model_copy(update=changes)` does not run field validation at
    # all -- update_agent already knows this for the *value* type (its own
    # comment: "Coerce explicitly rather than relying on validate-on-copy")
    # but, before this fix, never applied the same reasoning to the *key*.
    # `agent update --env BUZZ_ACP_AGENT_OWNER=attacker` would otherwise
    # silently re-point a live agent's owner with no validation anywhere.
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Update Env Guard",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )

    with pytest.raises(ValueError, match="reserved"):
        manager.update_agent(agent.id, env={"BUZZ_ACP_AGENT_OWNER": "attacker-pubkey"})


@pytest.mark.parametrize("field", ["env", "mcp_server"])
def test_update_agent_env_or_mcp_server_change_does_not_republish_managed_agent(
    field: str, tmp_path: Path, monkeypatch
) -> None:
    """content_fields deliberately excludes env/mcp_server: they hold
    secrets and are never read by visibility.managed_agent_content, so
    republishing the relay-facing managed-agent record for either would be
    a wasted round trip today, and — more importantly — this is a second,
    independent guard against a secret field ever being folded into that
    published record by a future change to content_fields alone.
    """
    from buzz_fleet.models import McpServer

    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Secret Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    assert agent.visibility_state.managed_agent_published is True
    runner.calls.clear()

    value: object = {"TOKEN": "shh"} if field == "env" else McpServer(name="boost", command="php")
    updated = manager.update_agent(agent.id, **{field: value})

    subcommands = [c[1] for c in runner.calls if c[0] == "buzz-fleet-signer"]
    assert "publish-managed-agent" not in subcommands
    # But the change itself still lands on the agent and its env file —
    # content_fields only governs the relay republish, not whether the
    # change actually takes effect.
    assert getattr(updated, field) is not None
    env_text = agent_env_path(f"eltahir:{agent.id}").read_text()
    if field == "env":
        assert "TOKEN=shh\n" in env_text
    else:
        assert "BUZZ_ACP_MCP_COMMAND=" in env_text


def test_create_agent_publishes_profile_and_add_policy_with_connection_auth_tag(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression test: publish-agent-profile and publish-agent-add-policy
    connect to the relay AS THE AGENT (not the owner) to sign their own
    events, and the agent is not a direct relay member — that connection
    itself needs an --auth-tag or the relay rejects it as "not a relay
    member" before the event is ever considered, regardless of what auth
    tag is embedded in the event content.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())

    manager.create_agent(
        display_name="Laravel Backend Dev",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="You are the dev."),
    )

    profile_call = next(c for c in runner.calls if tuple(c[:2]) == ("buzz-fleet-signer", "publish-agent-profile"))
    add_policy_call = next(
        c for c in runner.calls if tuple(c[:2]) == ("buzz-fleet-signer", "publish-agent-add-policy")
    )
    assert "--auth-tag" in profile_call
    assert "--auth-tag" in add_policy_call


def test_delete_agent_removes_member_and_stops_unit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Throwaway",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )

    manager.delete_agent(agent.id)

    assert ["systemctl", "--user", "disable", "--now", "buzz-agent@eltahir:throwaway.service"] in runner.calls
    assert any("remove-member" in c for c in runner.calls)
    assert manager.list_agents() == []


def test_update_agent_restarts_without_re_registering(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Throwaway",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    updated = manager.update_agent(agent.id, system_prompt_source=SystemPromptSource(kind="inline", text="y"))

    assert ["systemctl", "--user", "restart", "buzz-agent@eltahir:throwaway.service"] in runner.calls
    assert updated.system_prompt_source.text == "y"


def test_create_agent_with_missing_persona_file_fails_before_publishing(tmp_path: Path, monkeypatch) -> None:
    """Regression test for Fix 3.

    A missing/invalid persona_file path must fail loudly before any relay-side
    effect happens — otherwise state would be orphaned with no local record.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())

    with pytest.raises(FileNotFoundError):
        manager.create_agent(
            display_name="Broken Persona",
            harness="claude",
            system_prompt_source=SystemPromptSource(kind="persona_file", path=Path("/nonexistent/persona.md")),
        )

    assert manager.list_agents() == []


def test_delete_agent_survives_remove_member_failure_for_never_registered_agent(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression test: an agent created after direct relay membership was
    dropped from create_agent was never added as a member, so the relay
    rejects remove-member as "member not found" on delete. That must not
    crash deletion — it's expected steady state, not an error to surface.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")

    class NeverMemberRunner(FakeRunner):
        def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
            if "remove-member" in args:
                stdout = json.dumps({"ok": False, "error": "invalid: member not found: ab" * 16})
                return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
            return super().run(args)

    runner = NeverMemberRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Throwaway",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )

    manager.delete_agent(agent.id)  # must not raise

    assert manager.list_agents() == []


def test_delete_agent_removes_env_and_prompt_files(tmp_path: Path, monkeypatch) -> None:
    """Regression test for Fix 4: deleting an agent must remove its
    private-key-bearing .env file and its .prompt.md file, not just the state
    JSON — otherwise the secret survives "deletion" on disk.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Throwaway",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    assert agent_env_path(f"eltahir:{agent.id}").exists()
    assert agent_prompt_path(f"eltahir:{agent.id}").exists()

    manager.delete_agent(agent.id)

    assert not agent_env_path(f"eltahir:{agent.id}").exists()
    assert not agent_prompt_path(f"eltahir:{agent.id}").exists()


def test_delete_agent_removes_mcp_wrapper_and_pi_mcp_json(tmp_path: Path, monkeypatch) -> None:
    # Finding 2 (final review): Task 19 added two more secret-bearing
    # artifacts under WORK_DIR/<agent_id>/ that the "Fix 4" cleanup above
    # never covered -- the MCP wrapper script (mode 0700, a real
    # `export TOKEN='<secret>'`) and Pi's private mcp.json (mode 0600, the
    # same secrets as JSON). Both must be gone after delete_agent, the same
    # as the env file and prompt file.
    from pydantic import SecretStr

    from buzz_fleet.models import McpServer

    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.work_dir", lambda key: tmp_path / "work" / key)
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
    monkeypatch.setattr("buzz_fleet.systemd.pi_agent_template_dir", lambda: tmp_path / "pi-template-unused")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Secret Bearer",
        harness="pi",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
        mcp_server=McpServer(name="boost", command="php", args=["artisan", "boost:mcp"], env={"TOKEN": SecretStr("t")}),
    )
    wrapper = tmp_path / "work" / f"eltahir:{agent.id}" / "mcp-boost.sh"
    mcp_json = tmp_path / "work" / f"eltahir:{agent.id}" / ".pi-agent" / "mcp.json"
    assert wrapper.exists() and mcp_json.exists()

    manager.delete_agent(agent.id)

    assert not wrapper.exists()
    assert not mcp_json.exists()


def test_update_agent_preserves_previously_set_api_keys(tmp_path: Path, monkeypatch) -> None:
    """Regression test for Fix 9: update_agent must not wipe a previously-set
    ANTHROPIC_API_KEY/OPENAI_API_KEY when the update doesn't touch keys at all.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Keyed Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
        anthropic_api_key="sk-ant-test",
    )
    assert "ANTHROPIC_API_KEY=sk-ant-test" in agent_env_path(f"eltahir:{agent.id}").read_text()

    manager.update_agent(agent.id, display_name="Keyed Agent Renamed")

    env_content = agent_env_path(f"eltahir:{agent.id}").read_text()
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
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
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
    # Visibility events were published before enable_now raised — the local
    # record above is what makes that published state discoverable/revocable.
    assert any(tuple(c[:2]) == ("buzz-fleet-signer", "publish-agent-profile") for c in runner.calls)


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
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
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
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
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
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
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
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")

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
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
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

    assert ["systemctl", "--user", "restart", f"buzz-agent@eltahir:{first.id}.service"] in runner.calls
    assert ["systemctl", "--user", "restart", f"buzz-agent@eltahir:{second.id}.service"] in runner.calls


def test_ensure_runtime_ready_does_not_restart_agents_when_buzz_acp_already_installed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
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
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")

    from buzz_fleet import harnesses

    monkeypatch.setattr(harnesses.shutil, "which", lambda cmd: None)  # not resolvable at create time
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Codex Bot",
        harness="codex",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    env_path = agent_env_path(f"eltahir:{agent.id}")
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
    assert ["systemctl", "--user", "restart", f"buzz-agent@eltahir:{agent.id}.service"] in runner.calls


def test_ensure_runtime_ready_continues_healing_other_agents_if_one_restart_fails(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")

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

    assert ["systemctl", "--user", "restart", f"buzz-agent@eltahir:{second.id}.service"] in runner.calls


def test_ensure_runtime_ready_never_touches_agent_with_visibility_managed_false(tmp_path: Path, monkeypatch) -> None:
    """Regression test for the old-agent exemption — the single most
    important invariant in this feature. An agent created before this
    feature existed (visibility_managed=False) must never have any
    visibility signer subcommand invoked against it, ever.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
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
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")

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
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
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


@pytest.mark.parametrize(
    ("field", "value"),
    [("role", "reviewer"), ("capabilities", ["laravel"]), ("description", "Reviews PHP.")],
)
def test_update_agent_republishes_managed_agent_on_directory_field_change(
    field: str, value: object, tmp_path: Path, monkeypatch
) -> None:
    """Discriminating test for the content_fields republish trigger: role,
    capabilities, and description are directory fields published in the
    managed-agent record (kind:30177) — a change to any of them that fails
    to republish would leave every other machine reading stale directory
    data indefinitely, since nothing else re-triggers the publish.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Directory Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    assert agent.visibility_state.managed_agent_published is True
    runner.calls.clear()

    updated = manager.update_agent(agent.id, **{field: value})

    assert getattr(updated, field) == value
    subcommands = [c[1] for c in runner.calls if c[0] == "buzz-fleet-signer"]
    assert "publish-managed-agent" in subcommands
    assert updated.visibility_state.managed_agent_published is True


@pytest.mark.parametrize(
    ("field", "value"),
    [("role", "reviewer"), ("capabilities", ["laravel"]), ("description", "Reviews PHP.")],
)
def test_update_agent_with_unchanged_directory_field_does_not_republish(
    field: str, value: object, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Directory Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    assert agent.visibility_state.managed_agent_published is True
    # Set the field's starting value directly on the saved record (rather than
    # through create_agent's keyword-only params) so this test doesn't need a
    # dynamic **{field: value} call into a strictly-typed signature.
    from buzz_fleet import state as state_module

    pre_set = agent.model_copy(update={field: value})
    state_module.save_agent(pre_set)
    runner.calls.clear()

    updated = manager.update_agent(agent.id, **{field: value})

    subcommands = [c[1] for c in runner.calls if c[0] == "buzz-fleet-signer"]
    assert "publish-managed-agent" not in subcommands
    assert updated.visibility_state.managed_agent_published is True


def test_update_agent_joins_new_channel_and_leaves_removed_one(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
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
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
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
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
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
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
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
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
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
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")

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
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
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
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
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


def _expected_fake_auth_tag() -> str:
    return json.dumps(["auth", "d" * 64, "", "e" * 128])


def test_create_agent_writes_auth_tag_to_env_file(tmp_path: Path, monkeypatch) -> None:
    """Regression test for a real production incident: without BUZZ_AUTH_TAG
    in the agent's env file, buzz-acp never attaches a NIP-OA auth tag to its
    own NIP-42 AUTH event, so the relay's agent_owner_pubkey column is never
    populated — a human adding the agent to a channel from Desktop then fails
    with "policy:owner_only — agent has no owner set", even though every
    visibility event (kind:0/30177/10100) published fine.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())

    agent = manager.create_agent(
        display_name="Auth Tag Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )

    env_content = agent_env_path(f"eltahir:{agent.id}").read_text()
    assert f"BUZZ_AUTH_TAG={_expected_fake_auth_tag()}" in env_content


def test_ensure_runtime_ready_retroactively_adds_missing_auth_tag(tmp_path: Path, monkeypatch) -> None:
    """The exact real-world scenario: an agent created by an earlier version
    of buzz-fleet (before BUZZ_AUTH_TAG existed) is visibility_managed=True
    but its env file lacks BUZZ_AUTH_TAG — ensure_runtime_ready must notice
    and fix it on the very next call, without needing the agent recreated.
    """
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Retrofit Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    # Simulate a pre-fix env file: strip the BUZZ_AUTH_TAG line back out.
    env_path = agent_env_path(f"eltahir:{agent.id}")
    stripped = "\n".join(
        line for line in env_path.read_text().splitlines() if not line.startswith("BUZZ_AUTH_TAG=")
    )
    env_path.write_text(stripped + "\n")
    assert "BUZZ_AUTH_TAG" not in env_path.read_text()
    runner.calls.clear()

    manager.ensure_runtime_ready()

    assert f"BUZZ_AUTH_TAG={_expected_fake_auth_tag()}" in env_path.read_text()
    assert ["systemctl", "--user", "restart", f"buzz-agent@eltahir:{agent.id}.service"] in runner.calls


def test_ensure_runtime_ready_does_not_add_auth_tag_for_old_agent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Old Agent Two",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    from buzz_fleet import state as state_module

    old_style = agent.model_copy(update={"visibility_managed": False})
    state_module.save_agent(old_style)
    env_path = agent_env_path(f"eltahir:{agent.id}")
    stripped = "\n".join(
        line for line in env_path.read_text().splitlines() if not line.startswith("BUZZ_AUTH_TAG=")
    )
    env_path.write_text(stripped + "\n")
    runner.calls.clear()

    manager.ensure_runtime_ready()

    assert "BUZZ_AUTH_TAG" not in env_path.read_text()
    assert not any(c[:2] == ["buzz-fleet-signer", "compute-auth-tag"] for c in runner.calls)


def test_update_agent_writes_auth_tag_to_env_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "systemd" / "buzz-agent@.service")
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Update Auth Tag Agent",
        harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="x"),
    )
    env_path = agent_env_path(f"eltahir:{agent.id}")
    stripped = "\n".join(
        line for line in env_path.read_text().splitlines() if not line.startswith("BUZZ_AUTH_TAG=")
    )
    env_path.write_text(stripped + "\n")

    manager.update_agent(agent.id, display_name="Renamed")

    assert f"BUZZ_AUTH_TAG={_expected_fake_auth_tag()}" in env_path.read_text()


def test_template_change_restarts_every_agent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.work_dir", lambda key: tmp_path / "work" / key)
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "unit" / "buzz-agent@.service")
    monkeypatch.setattr("buzz_fleet.systemd.ensure_linger_enabled", lambda runner: None)
    runner = FakeRunner()
    manager = AgentManager(runner, _community())
    agent = manager.create_agent(
        display_name="Restart Me", harness="claude",
        system_prompt_source=SystemPromptSource(kind="inline", text="hi"),
    )
    (tmp_path / "unit" / "buzz-agent@.service").write_text("[Unit]\nDescription=old\n")
    runner.calls.clear()

    manager.ensure_runtime_ready()

    assert any(a[:3] == ["systemctl", "--user", "restart"] and agent.id in a[3] for a in runner.calls)


from buzz_fleet.orchestration.record import ABOUT_HEADER, FleetRecord, encode_about


def _fresh_manager(tmp_path: Path, monkeypatch, runner: FakeRunner) -> AgentManager:
    monkeypatch.setattr("buzz_fleet.state.CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr("buzz_fleet.systemd.units_secrets_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.units_state_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr("buzz_fleet.systemd.work_dir", lambda key: tmp_path / "work" / key)
    monkeypatch.setattr("buzz_fleet.systemd.template_unit_path", lambda: tmp_path / "unit" / "buzz-agent@.service")
    monkeypatch.setattr("buzz_fleet.systemd.ensure_linger_enabled", lambda runner: None)
    from buzz_fleet import state
    community = _community()
    state.save_community(community)
    return AgentManager(runner, community)


def _record_about() -> str:
    return encode_about(FleetRecord(retrieval_key="r" * 64, created_at=1))


def test_init_fleet_channel_creates_writes_record_and_persists(tmp_path: Path, monkeypatch) -> None:
    runner = FakeRunner()
    manager = _fresh_manager(tmp_path, monkeypatch, runner)

    channel_id, rec = manager.init_fleet_channel(existing=None, host="vps")

    assert channel_id == FLEET and len(rec.retrieval_key) == 64
    from buzz_fleet import state
    saved = state.load_community("eltahir")
    assert saved is not None and saved.fleet_channel_id == FLEET
    assert saved.fleet_record is not None and saved.fleet_record.retrieval_key == rec.retrieval_key
    about_call = next(a for a in runner.calls if a[1] == "write-channel-about")
    assert about_call[about_call.index("--about") + 1].startswith(ABOUT_HEADER)


def test_init_fleet_channel_records_pi_mcp_adapter_version(tmp_path: Path, monkeypatch) -> None:
    """The pinned pi-mcp-adapter version travels in the fleet record
    alongside buzz-fleet's own, so every machine (and Desktop/mobile, which
    reads this record) can see what's pinned without SSHing in.
    """
    from buzz_fleet import harnesses

    runner = FakeRunner()
    manager = _fresh_manager(tmp_path, monkeypatch, runner)

    _, rec = manager.init_fleet_channel(existing=None, host="vps")

    assert rec.versions["pi-mcp-adapter"] == harnesses.PI_MCP_ADAPTER_VERSION


def test_init_fleet_channel_refuses_when_record_exists(tmp_path: Path, monkeypatch) -> None:
    runner = FakeRunner()
    runner.channels = [{"channel_id": FLEET, "name": "fleet", "about": _record_about(), "archived": False}]
    manager = _fresh_manager(tmp_path, monkeypatch, runner)
    with pytest.raises(RuntimeError, match="already exists"):
        manager.init_fleet_channel(existing=None, host="vps")


def test_init_fleet_channel_adopts_and_writes_record(tmp_path: Path, monkeypatch) -> None:
    runner = FakeRunner()
    runner.channels = [{"channel_id": FLEET, "name": "ops", "about": "plain", "archived": False}]
    manager = _fresh_manager(tmp_path, monkeypatch, runner)
    channel_id, _ = manager.init_fleet_channel(existing=FLEET, host="vps")
    assert channel_id == FLEET
    assert not any(a[1] == "create-channel" for a in runner.calls)
    assert any(a[1] == "write-channel-about" for a in runner.calls)


def test_ensure_runtime_ready_discovers_record_joins_and_rewrites_env(tmp_path: Path, monkeypatch) -> None:
    runner = FakeRunner()
    manager = _fresh_manager(tmp_path, monkeypatch, runner)
    agent = manager.create_agent(display_name="Reviewer", harness="claude",
                                 system_prompt_source=SystemPromptSource(kind="inline", text="hi"))
    runner.channels = [{"channel_id": FLEET, "name": "fleet", "about": _record_about(), "archived": False}]
    runner.calls.clear()

    manager.ensure_runtime_ready()

    from buzz_fleet import state
    saved = state.load_community("eltahir")
    assert saved is not None and saved.fleet_channel_id == FLEET
    assert len([a for a in runner.calls if a[1] == "join-channel" and FLEET in a]) == 1
    env = agent_env_path(f"eltahir:{agent.id}").read_text()
    assert f"BUZZ_FLEET_CHANNEL={FLEET}\n" in env and f"BUZZ_FLEET_RETRIEVAL_KEY={'r' * 64}\n" in env
    assert state.load_agents("eltahir")[0].visibility_state.channels[FLEET] == "joined"


def test_ensure_runtime_ready_refreshes_stale_block(tmp_path: Path, monkeypatch) -> None:
    runner = FakeRunner()
    manager = _fresh_manager(tmp_path, monkeypatch, runner)
    agent = manager.create_agent(display_name="Blocky", harness="claude",
                                 system_prompt_source=SystemPromptSource(kind="inline", text="hi"))
    env_path = agent_env_path(f"eltahir:{agent.id}")
    env_path.write_text(env_path.read_text().replace("coordination v1", "coordination v0"))
    runner.calls.clear()

    manager.ensure_runtime_ready()

    assert "coordination v1" in env_path.read_text()
    assert any(a[:3] == ["systemctl", "--user", "restart"] and agent.id in a[3] for a in runner.calls)


def test_create_agent_refuses_duplicate_display_name_unless_forced(tmp_path: Path, monkeypatch) -> None:
    runner = FakeRunner()
    runner.channels = [{"channel_id": FLEET, "name": "fleet", "about": _record_about(), "archived": False}]
    runner.members = [{"pubkey": "d" * 64, "display_name": "Reviewer"}]
    manager = _fresh_manager(tmp_path, monkeypatch, runner)
    src = SystemPromptSource(kind="inline", text="hi")
    with pytest.raises(ValueError, match="already used"):
        manager.create_agent(display_name="Reviewer", harness="claude", system_prompt_source=src)
    assert manager.create_agent(display_name="Reviewer", harness="claude", system_prompt_source=src, force=True)


class _CustomReadMetaRunner(FakeRunner):
    """`FakeRunner` whose `read-channel-meta` response is fully overridable
    — used to drive `ensure_fleet_record`'s never-raise contract through
    each of its `except` clauses individually (a signer-reported failure, a
    malformed/non-JSON payload, and a channel entry missing `channel_id`),
    none of which the shared `FakeRunner` (which always returns `ok: true`)
    can exercise on its own.
    """

    def __init__(self, stdout: str, returncode: int = 0) -> None:
        super().__init__()
        self._meta_stdout = stdout
        self._meta_returncode = returncode

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["buzz-fleet-signer", "read-channel-meta"]:
            self.calls.append(args)
            return subprocess.CompletedProcess(args, self._meta_returncode, stdout=self._meta_stdout, stderr="")
        return super().run(args)


def test_ensure_fleet_record_never_raises_on_signer_reported_failure(tmp_path: Path, monkeypatch) -> None:
    runner = _CustomReadMetaRunner(json.dumps({"ok": False, "error": "boom"}), returncode=1)
    manager = _fresh_manager(tmp_path, monkeypatch, runner)

    assert manager.ensure_fleet_record() is None
    assert manager._last_fleet_error is not None and "boom" in manager._last_fleet_error
    manager.ensure_runtime_ready()  # must complete, not raise


def test_ensure_fleet_record_never_raises_on_malformed_json(tmp_path: Path, monkeypatch) -> None:
    runner = _CustomReadMetaRunner("not json at all")
    manager = _fresh_manager(tmp_path, monkeypatch, runner)

    assert manager.ensure_fleet_record() is None
    manager.ensure_runtime_ready()  # must complete, not raise


def test_ensure_fleet_record_never_raises_on_channel_missing_id(tmp_path: Path, monkeypatch) -> None:
    runner = _CustomReadMetaRunner(json.dumps({"ok": True, "channels": [{"about": _record_about(), "archived": False}]}))
    manager = _fresh_manager(tmp_path, monkeypatch, runner)

    assert manager.ensure_fleet_record() is None
    manager.ensure_runtime_ready()  # must complete, not raise


def test_ensure_fleet_record_still_returns_record_when_local_caching_fails(tmp_path: Path, monkeypatch) -> None:
    """Regression: `_save_fleet` reaches `state._write_secure`, whose
    `mkdir`/`os.open(O_CREAT)`/`os.write` all raise `OSError` on a
    read-only home, a full disk, or a root-owned `~/.config/buzz-fleet`.
    The record itself was still found and is perfectly usable — a local
    caching failure must not masquerade as "no fleet record exists", and
    must not propagate out of `ensure_runtime_ready` either.
    """
    from buzz_fleet import state

    runner = FakeRunner()
    runner.channels = [{"channel_id": FLEET, "name": "fleet", "about": _record_about(), "archived": False}]
    manager = _fresh_manager(tmp_path, monkeypatch, runner)
    real_save_community = state.save_community
    call_count = {"n": 0}

    def _boom(community: Community) -> None:
        # Only the FIRST save (the fleet-record caching write triggered by
        # ensure_fleet_record below) fails. A later, unrelated write (e.g.
        # _ensure_owner_pubkey's own backfill save inside
        # ensure_runtime_ready) must keep working — this isolates exactly
        # the scenario finding 1 is about, not every write forever after.
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("disk full")
        real_save_community(community)

    monkeypatch.setattr(state, "save_community", _boom)

    rec = manager.ensure_fleet_record()

    assert rec is not None and rec.retrieval_key == "r" * 64
    manager.ensure_runtime_ready()  # must complete, not raise


def test_ensure_fleet_record_reports_duplicate_records_via_last_fleet_error(tmp_path: Path, monkeypatch) -> None:
    """Two racing `fleet init` runs produce two channels each carrying a
    fleet record. `ensure_fleet_record` must not raise (never-raise
    contract) but must not silently discard the diagnostic either — the
    operator needs it (see `fleet_status`'s use of `_last_fleet_error`).
    """
    other_channel = "77770000-0000-4000-8000-000000000000"
    runner = FakeRunner()
    runner.channels = [
        {"channel_id": FLEET, "name": "fleet", "about": _record_about(), "archived": False},
        {"channel_id": other_channel, "name": "fleet", "about": _record_about(), "archived": False},
    ]
    manager = _fresh_manager(tmp_path, monkeypatch, runner)

    assert manager.ensure_fleet_record() is None
    assert manager._last_fleet_error is not None
    assert "more than one channel carries a fleet record" in manager._last_fleet_error
    assert FLEET in manager._last_fleet_error and other_channel in manager._last_fleet_error
    manager.ensure_runtime_ready()  # must complete, not raise, even for this case


def test_init_fleet_channel_names_orphaned_channel_when_about_write_fails(tmp_path: Path, monkeypatch) -> None:
    """If `write_channel_about` fails after `create_channel` already
    succeeded, the new channel exists on the relay with no fleet record —
    invisible to future discovery — so a blind retry of this create-once
    operation would create a SECOND channel. The error must name the
    orphaned id so the operator can adopt it with `--channel` instead.
    """

    class _FailingAboutRunner(FakeRunner):
        def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:2] == ["buzz-fleet-signer", "write-channel-about"]:
                self.calls.append(args)
                return subprocess.CompletedProcess(args, 1, stdout=json.dumps({"ok": False, "error": "relay timeout"}), stderr="")
            return super().run(args)

    runner = _FailingAboutRunner()
    manager = _fresh_manager(tmp_path, monkeypatch, runner)

    with pytest.raises(RuntimeError, match=f"channel '{FLEET}' was created") as exc_info:
        manager.init_fleet_channel(existing=None, host="vps")
    assert "--channel" in str(exc_info.value) and FLEET in str(exc_info.value)
