"""Orchestrates state, systemd files, and the signer/systemctl clients for agent CRUD."""

from __future__ import annotations

from datetime import datetime, timezone

from buzz_fleet import signer_client, state, systemctl_client, systemd
from buzz_fleet.models import Agent, Community, SystemPromptSource
from buzz_fleet.proc import CommandRunner
from buzz_fleet.slug import agent_slug


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
        role: str | None = None,
        anthropic_api_key: str | None = None,
        openai_api_key: str | None = None,
    ) -> Agent:
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
            created_at=datetime.now(timezone.utc),
        )

        signer_client.add_member(
            self._runner,
            self._community.relay_url,
            self._community.relay_admin_nsec.get_secret_value(),
            public_key,
            role=role,
        )
        systemd.write_agent_files(agent, self._community, anthropic_api_key, openai_api_key)
        systemctl_client.enable_now(self._runner, agent.id)
        state.save_agent(agent)
        return agent

    def update_agent(self, agent_id: str, **changes: object) -> Agent:
        agents = {a.id: a for a in self.list_agents()}
        current = agents[agent_id]
        updated = current.model_copy(update=changes)
        systemd.write_agent_files(updated, self._community, anthropic_api_key=None, openai_api_key=None)
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
