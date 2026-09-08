"""Local state for buzz-fleet: one file per community plus per-agent files,
with every secret held in a parallel tree under `paths.secrets_dir()`.

Splitting them means a state dump is safe to read, diff and paste into a bug
report without any redaction step. The secrets tree mirrors the state tree's
shape exactly — same relative paths, same filenames — so the two are trivially
correlated by eye, and a merge is a recursive dict update with no key escaping
to get wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, SecretStr

from buzz_fleet import atomic, paths
from buzz_fleet.models import Agent, Community


def _communities_dir() -> Path:
    return paths.state_dir() / "communities"


def _secret_communities_dir() -> Path:
    return paths.secrets_dir() / "communities"


def _agents_dir(community_id: str) -> Path:
    return _communities_dir() / community_id / "agents"


def _secret_agents_dir(community_id: str) -> Path:
    return _secret_communities_dir() / community_id / "agents"


def _extract_secrets(original: BaseModel) -> dict:
    """A dict mirroring the model's shape, holding only its SecretStr leaves.

    Mirrors the three shapes secrets actually take in these models: a plain
    field (`relay_admin_nsec`, `private_key`), a nested model (`mcp_server`),
    and a dict of secrets (`Agent.env`, `McpServer.env`).
    """
    out: dict = {}
    for name in type(original).model_fields:
        value = getattr(original, name, None)
        if isinstance(value, SecretStr):
            out[name] = value.get_secret_value()
        elif isinstance(value, BaseModel):
            nested = _extract_secrets(value)
            if nested:
                out[name] = nested
        elif isinstance(value, dict):
            inner = {k: v.get_secret_value() for k, v in value.items() if isinstance(v, SecretStr)}
            if inner:
                out[name] = inner
    return out


def _merge_secrets(data: dict, secrets: dict) -> dict:
    for name, value in secrets.items():
        if isinstance(value, dict) and isinstance(data.get(name), dict):
            _merge_secrets(data[name], value)
        else:
            data[name] = value
    return data


def _write_split(state_path: Path, secret_path: Path, obj: Community | Agent) -> None:
    """`model_dump(mode="json")` already masks every SecretStr as
    "**********", so the public dump needs no scrubbing of its own — the mask
    is what lands in state, and the real values go to the secrets tree.
    `atomic.write_secure`'s `dir_mode` default of 0700 creates every missing
    ancestor directory itself, so there is no directory bookkeeping to do here.
    """
    atomic.write_secure(state_path, json.dumps(obj.model_dump(mode="json"), indent=2), mode=0o600)
    atomic.write_secure(secret_path, json.dumps(_extract_secrets(obj), indent=2), mode=0o600)


def _read_merged(state_path: Path, secret_path: Path) -> dict:
    data = json.loads(state_path.read_text())
    if secret_path.exists():
        _merge_secrets(data, json.loads(secret_path.read_text()))
    return data


def save_community(community: Community) -> None:
    with atomic.locked(paths.state_dir() / f".{community.id}.lock"):
        _write_split(
            _communities_dir() / f"{community.id}.json",
            _secret_communities_dir() / f"{community.id}.json",
            community,
        )


def load_community(community_id: str) -> Community | None:
    path = _communities_dir() / f"{community_id}.json"
    if not path.exists():
        return None
    secret = _secret_communities_dir() / f"{community_id}.json"
    return Community.model_validate(_read_merged(path, secret))


def list_community_ids() -> list[str]:
    directory = _communities_dir()
    return sorted(p.stem for p in directory.glob("*.json")) if directory.exists() else []


def save_agent(agent: Agent) -> None:
    with atomic.locked(paths.state_dir() / f".{agent.community_id}.lock"):
        _write_split(
            _agents_dir(agent.community_id) / f"{agent.id}.json",
            _secret_agents_dir(agent.community_id) / f"{agent.id}.json",
            agent,
        )


def load_agents(community_id: str) -> list[Agent]:
    directory = _agents_dir(community_id)
    if not directory.exists():
        return []
    secrets = _secret_agents_dir(community_id)
    return [
        Agent.model_validate(_read_merged(p, secrets / p.name))
        for p in sorted(directory.glob("*.json"))
    ]


def delete_agent(community_id: str, agent_id: str) -> None:
    with atomic.locked(paths.state_dir() / f".{community_id}.lock"):
        (_agents_dir(community_id) / f"{agent_id}.json").unlink(missing_ok=True)
        (_secret_agents_dir(community_id) / f"{agent_id}.json").unlink(missing_ok=True)
