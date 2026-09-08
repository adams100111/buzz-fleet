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

# What `model_dump(mode="json")` turns every SecretStr into. It must never
# round-trip back to disk as if it were a real value -- see _extract_secrets
# (refuses to persist it) and _read_merged (refuses to load it).
_MASK = "**********"


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

    Raises if any real SecretStr's value is itself the mask string. That can
    only happen if a model was built from already-masked data (e.g. a caller
    re-saving an object it loaded without going through `_read_merged`, or a
    load that raced a temporary I/O failure) -- writing it through here would
    permanently overwrite a real key with ten literal asterisks.
    """
    out: dict = {}
    for name in type(original).model_fields:
        value = getattr(original, name, None)
        if isinstance(value, SecretStr):
            secret = value.get_secret_value()
            if secret == _MASK:
                raise ValueError(
                    f"refusing to persist a masked secret for {type(original).__name__}.{name} — "
                    "this SecretStr holds the literal mask, not a real value"
                )
            out[name] = secret
        elif isinstance(value, BaseModel):
            nested = _extract_secrets(value)
            if nested:
                out[name] = nested
        elif isinstance(value, dict):
            inner: dict = {}
            for key, item in value.items():
                if not isinstance(item, SecretStr):
                    continue
                secret = item.get_secret_value()
                if secret == _MASK:
                    raise ValueError(
                        f"refusing to persist a masked secret for "
                        f"{type(original).__name__}.{name}[{key!r}] — "
                        "this SecretStr holds the literal mask, not a real value"
                    )
                inner[key] = secret
            if inner:
                out[name] = inner
    return out


def _contains_mask(data: object) -> bool:
    """True if `data` (a `_read_merged`-shaped dict) still holds the mask
    string anywhere — meaning the secrets file didn't actually account for
    every secret the state file declares."""
    if isinstance(data, str):
        return data == _MASK
    if isinstance(data, dict):
        return any(_contains_mask(v) for v in data.values())
    return False


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

    The secrets file is written *first*. Each `atomic.write_secure` call is
    individually atomic, but the pair is not — if the second write fails
    (ENOSPC, a permission error on `secrets/`, a signal), whichever file
    landed first is left orphaned relative to the other. `load_community`/
    `load_agents` key existence off the *state* file, so an orphaned secrets
    file with no matching state file is simply invisible and harmless, while
    the reverse (a state file with no matching secrets file) is the
    corrupting case `_read_merged` below would otherwise have to detect after
    the fact. Writing secrets first also means an unlocked concurrent reader
    can only ever see the previous consistent generation, never a new state
    file whose secret hasn't landed yet.
    """
    secrets_json = json.dumps(_extract_secrets(obj), indent=2)
    atomic.write_secure(secret_path, secrets_json, mode=0o600)
    atomic.write_secure(state_path, json.dumps(obj.model_dump(mode="json"), indent=2), mode=0o600)


def _read_merged(state_path: Path, secret_path: Path) -> dict:
    """Merge a state file with its secrets file, refusing to hand back a mask.

    Every `Community` has a required `relay_admin_nsec` and every `Agent` a
    required `private_key`, so a state file existing with no secrets file (or
    with a secrets file that doesn't actually cover every secret the state
    file declares) is always corruption, never a legitimate state — silently
    returning the mask would otherwise let `"**********"` round-trip into the
    loaded model, and from there into whatever a caller does next (e.g. a
    systemd env file, or a re-save that would persist the mask permanently).
    """
    data = json.loads(state_path.read_text())
    if not secret_path.exists():
        raise ValueError(
            f"state file {state_path} exists but its secrets file {secret_path} is missing"
        )
    _merge_secrets(data, json.loads(secret_path.read_text()))
    if _contains_mask(data):
        raise ValueError(
            f"secrets file {secret_path} does not account for every secret in {state_path}"
        )
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
