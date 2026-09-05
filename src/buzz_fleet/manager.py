"""Orchestrates state, systemd files, and the signer/systemctl clients for agent CRUD."""

from __future__ import annotations

from datetime import UTC, datetime

from buzz_fleet import buzz_acp, harnesses, signer_client, state, systemctl_client, systemd
from buzz_fleet.models import Agent, Community, SystemPromptSource
from buzz_fleet.proc import CommandRunner
from buzz_fleet.slug import agent_slug

_ENV_KEY_NAMES = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")


def _read_agent_command(agent_id: str) -> str | None:
    """The `BUZZ_ACP_AGENT_COMMAND` value currently written for `agent_id`,

    or None if it has no env file yet (a brand-new agent — `create_agent`'s
    own `write_agent_files` call handles that case directly).
    """
    path = systemd.agent_env_path(agent_id)
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        if line.startswith("BUZZ_ACP_AGENT_COMMAND="):
            return line.partition("=")[2]
    return None


def _read_existing_env_keys(agent_id: str) -> dict[str, str]:
    """Best-effort parse of an existing agent .env file for keys write_agent_files

    would otherwise clobber with None on every update (Fix 9). A simple
    line-parse is sufficient here — this isn't a general env-file parser.
    """
    path = systemd.agent_env_path(agent_id)
    if not path.exists():
        return {}
    found: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key in _ENV_KEY_NAMES:
            found[key] = value
    return found


class AgentManager:
    def __init__(self, runner: CommandRunner, community: Community) -> None:
        self._runner = runner
        self._community = community

    def list_agents(self) -> list[Agent]:
        return state.load_agents(self._community.id)

    def _ensure_owner_pubkey(self) -> None:
        """Backfill `owner_pubkey` on a `Community` saved before that field

        existed, deriving it once from the already-known admin nsec and
        persisting it — not a migration script to run by hand, just another
        thing `ensure_runtime_ready()` notices and fixes.
        """
        if self._community.owner_pubkey:
            return
        owner_pubkey = signer_client.pubkey_from_nsec(
            self._runner, self._community.relay_admin_nsec.get_secret_value()
        )
        self._community = self._community.model_copy(update={"owner_pubkey": owner_pubkey})
        state.save_community(self._community)

    def ensure_runtime_ready(self) -> None:
        """Make sure everything a `buzz-agent@*` unit needs actually exists,
        automatically healing agents that were already broken by it rather
        than requiring a manual restart. Cheap to call on every dashboard
        load or CLI command — each check is a no-op once already satisfied,
        and an agent is only rewritten/restarted when something it actually
        needs changed, never on an already-healthy call.

        Heals three distinct, real incidents this way:

        1. buzz-fleet never installed buzz-acp (the binary every unit
           execs) at all — a machine that never separately installed it
           crash-looped hundreds of times with status=203/EXEC.
        2. Even after installing a harness's adapter (`claude-agent-acp`,
           `codex-acp`, ...), an *already-existing* agent's env file still
           has the stale command that was written before the adapter
           existed — systemd's own PATH is fixed and won't pick it up
           afterward on its own (see `harnesses.resolve_adapter_command`).
        3. `BUZZ_ACP_AGENT_OWNER` was never set at all — every agent ran
           "successfully" while silently dropping 100% of events forever,
           since buzz-acp's own default (`respond_to=owner-only`) has
           nothing to match against with no owner configured.

        No step here should ever need a human to run something by hand.
        """
        owner_pubkey_before = self._community.owner_pubkey
        self._ensure_owner_pubkey()
        owner_pubkey_just_backfilled = owner_pubkey_before != self._community.owner_pubkey

        systemd.ensure_linger_enabled(self._runner)
        systemd.ensure_template_unit_installed(self._runner)
        buzz_acp_just_installed = buzz_acp.ensure_buzz_acp_installed()
        needs_full_refresh = buzz_acp_just_installed or owner_pubkey_just_backfilled

        for agent in self.list_agents():
            resolved_command = harnesses.resolve_adapter_command(agent.harness)
            if not needs_full_refresh and _read_agent_command(agent.id) == resolved_command:
                continue
            existing_keys = _read_existing_env_keys(agent.id)
            systemd.write_agent_files(
                agent,
                self._community,
                anthropic_api_key=existing_keys.get("ANTHROPIC_API_KEY"),
                openai_api_key=existing_keys.get("OPENAI_API_KEY"),
            )
            try:
                systemctl_client.restart(self._runner, agent.id)
            except RuntimeError:
                # Best-effort: one agent's restart failing (e.g. its own
                # unrelated config problem) must not block healing the
                # rest, or the create/list call this came from. The next
                # dashboard refresh's status column shows the truth.
                pass

    def create_agent(
        self,
        *,
        display_name: str,
        harness: str,
        system_prompt_source: SystemPromptSource,
        team_instructions: str | None = None,
        model: str | None = None,
        parallelism: int | None = None,
        idle_timeout_seconds: int | None = None,
        max_turn_duration_seconds: int | None = None,
        respond_to_allowlist: list[str] | None = None,
        role: str | None = None,
        anthropic_api_key: str | None = None,
        openai_api_key: str | None = None,
    ) -> Agent:
        self.ensure_runtime_ready()
        existing_ids = {a.id for a in self.list_agents()}
        agent_id = agent_slug(display_name, existing_ids)
        public_key, secret_key = signer_client.generate_key(self._runner)

        agent = Agent(
            id=agent_id,
            community_id=self._community.id,
            display_name=display_name,
            harness=harness,  # type: ignore[arg-type]
            private_key=secret_key,
            public_key=public_key,
            system_prompt_source=system_prompt_source,
            team_instructions=team_instructions,
            model=model,
            parallelism=parallelism,
            idle_timeout_seconds=idle_timeout_seconds,
            max_turn_duration_seconds=max_turn_duration_seconds,
            respond_to_allowlist=respond_to_allowlist,
            created_at=datetime.now(UTC),
        )

        # Resolve (and thereby validate) the prompt source BEFORE publishing
        # relay membership. A missing/invalid persona_file path must fail loudly
        # here, before any relay-side effect happens — otherwise add_member below
        # publishes a real kind:9030 event for a member that never gets recorded
        # locally (write_agent_files failing after add_member would orphan it,
        # with no local record of the secret key to ever revoke it). See Fix 3.
        systemd.resolve_prompt_text(agent)

        signer_client.add_member(
            self._runner,
            self._community.relay_url,
            self._community.relay_admin_nsec.get_secret_value(),
            public_key,
            role=role,
        )
        systemd.write_agent_files(agent, self._community, anthropic_api_key, openai_api_key)
        # Persist the local record BEFORE enabling the unit. enable_now can
        # raise (e.g. no `loginctl enable-linger` yet on a fresh host — the
        # literal first-run condition), and if it did before this save, the
        # relay membership + private-key env file above would exist with no
        # local record to ever see or revoke them. Saving first means a failed
        # enable_now is retryable (`agent list` still shows the agent; the
        # unit can be enabled by hand or via a future retry) instead of orphaning.
        state.save_agent(agent)
        systemctl_client.enable_now(self._runner, agent.id)
        return agent

    def update_agent(self, agent_id: str, **changes: object) -> Agent:
        self._ensure_owner_pubkey()
        agents = {a.id: a for a in self.list_agents()}
        current = agents[agent_id]
        updated = current.model_copy(update=changes)
        # Preserve any previously-set API keys instead of wiping them on every
        # update — write_agent_files takes explicit key args rather than
        # merging, so we read the existing .env file forward here (Fix 9).
        existing_keys = _read_existing_env_keys(agent_id)
        systemd.write_agent_files(
            updated,
            self._community,
            anthropic_api_key=existing_keys.get("ANTHROPIC_API_KEY"),
            openai_api_key=existing_keys.get("OPENAI_API_KEY"),
        )
        systemctl_client.restart(self._runner, agent_id)
        state.save_agent(updated)
        return updated

    def delete_agent(self, agent_id: str) -> None:
        agents = {a.id: a for a in self.list_agents()}
        agent = agents[agent_id]
        systemctl_client.disable_now(self._runner, agent_id)
        signer_client.remove_member(
            self._runner,
            self._community.relay_url,
            self._community.relay_admin_nsec.get_secret_value(),
            agent.public_key,
        )
        state.delete_agent(self._community.id, agent_id)
        # Remove the private-key-bearing env file and the persona prompt file —
        # without this the secret key survives "deletion" on disk, and a stale
        # env file could be silently reused by a future agent with the same
        # slug (Fix 4).
        systemd.agent_env_path(agent_id).unlink(missing_ok=True)
        systemd.agent_prompt_path(agent_id).unlink(missing_ok=True)
