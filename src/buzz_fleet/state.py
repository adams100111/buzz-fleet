"""Local JSON state for buzz-fleet, one file per community plus per-agent files."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import SecretStr

from buzz_fleet.models import Agent, Community

CONFIG_DIR = Path.home() / ".config" / "buzz-fleet"


def _write_secure(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)


def _serialize_with_secrets(obj: Community | Agent) -> str:
    """Serialize model to JSON, including actual SecretStr values for secure storage."""
    data = obj.model_dump()
    # Recursively convert SecretStr instances to their actual values
    def convert_secrets(d):
        if isinstance(d, dict):
            return {k: convert_secrets(v) for k, v in d.items()}
        elif isinstance(d, list):
            return [convert_secrets(v) for v in d]
        elif isinstance(d, SecretStr):
            return d.get_secret_value()
        return d
    return json.dumps(convert_secrets(data))


def save_community(community: Community) -> None:
    path = CONFIG_DIR / "communities" / f"{community.id}.json"
    _write_secure(path, _serialize_with_secrets(community))


def load_community(community_id: str) -> Community | None:
    path = CONFIG_DIR / "communities" / f"{community_id}.json"
    if not path.exists():
        return None
    return Community.model_validate_json(path.read_text())


def _agents_dir(community_id: str) -> Path:
    return CONFIG_DIR / "communities" / community_id / "agents"


def save_agent(agent: Agent) -> None:
    path = _agents_dir(agent.community_id) / f"{agent.id}.json"
    _write_secure(path, _serialize_with_secrets(agent))


def load_agents(community_id: str) -> list[Agent]:
    directory = _agents_dir(community_id)
    if not directory.exists():
        return []
    return [Agent.model_validate_json(p.read_text()) for p in sorted(directory.glob("*.json"))]


def delete_agent(community_id: str, agent_id: str) -> None:
    path = _agents_dir(community_id) / f"{agent_id}.json"
    path.unlink(missing_ok=True)
