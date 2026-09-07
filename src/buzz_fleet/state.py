"""Local JSON state for buzz-fleet, one file per community plus per-agent files."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, SecretStr

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
    """Serialize model to JSON, including actual SecretStr values for secure storage.

    Uses mode="json" to convert datetime/Path to JSON-serializable types,
    then patches back actual SecretStr values (since mode="json" masks them as "**********").
    """
    data = obj.model_dump(mode="json")

    # Recursively find and patch masked SecretStr placeholders with actual
    # values, walking the *original* (unmasked) model alongside the dumped
    # JSON dict so every field is patched using its own real type rather than
    # guessing from the serialized shape (mode="json" turns every SecretStr —
    # top-level or nested any number of levels deep, plain or inside a dict —
    # into the literal string "**********").
    def patch_secrets(obj_data: dict, original_obj: BaseModel) -> dict:
        for field_name, field_value in obj_data.items():
            original_field = getattr(original_obj, field_name, None)
            if isinstance(original_field, SecretStr):
                if field_value == "**********":
                    obj_data[field_name] = original_field.get_secret_value()
            elif isinstance(original_field, BaseModel):
                # A nested model (e.g. system_prompt_source, mcp_server) —
                # recurse using the real sub-model as the new "original".
                if isinstance(field_value, dict):
                    patch_secrets(field_value, original_field)
            elif isinstance(original_field, dict) and isinstance(field_value, dict):
                # A dict field whose values may themselves be SecretStr (e.g.
                # Agent.env, McpServer.env) — patch each masked entry using
                # the real dict's values, not another recursive dict walk
                # (its values are secrets, not nested models).
                for key, original_value in original_field.items():
                    if isinstance(original_value, SecretStr) and field_value.get(key) == "**********":
                        field_value[key] = original_value.get_secret_value()
        return obj_data

    patch_secrets(data, obj)
    return json.dumps(data)


def save_community(community: Community) -> None:
    path = CONFIG_DIR / "communities" / f"{community.id}.json"
    _write_secure(path, _serialize_with_secrets(community))


def load_community(community_id: str) -> Community | None:
    path = CONFIG_DIR / "communities" / f"{community_id}.json"
    if not path.exists():
        return None
    return Community.model_validate_json(path.read_text())


def list_community_ids() -> list[str]:
    directory = CONFIG_DIR / "communities"
    return sorted(p.stem for p in directory.glob("*.json")) if directory.exists() else []


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
