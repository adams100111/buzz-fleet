"""Orchestrates state, systemd files, and the signer/systemctl clients for agent CRUD."""

from __future__ import annotations

from datetime import UTC, datetime

from buzz_fleet import signer_client, state, systemctl_client, systemd
from buzz_fleet.models import Agent, Community, SystemPromptSource
from buzz_fleet.proc import CommandRunner
from buzz_fleet.slug import agent_slug

_ENV_KEY_NAMES = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")


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
        systemd.ensure_linger_enabled(self._runner)
        systemd.ensure_template_unit_installed(self._runner)
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
