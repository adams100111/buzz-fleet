"""Move a machine from the pre-XDG layout onto the current one, once.

Every step checks its own postcondition, so resuming after an interruption is
just running it again — which matters, because an interrupted run leaves some
agents on old unit names and some on new. Migration is per-agent for the same
reason: an agent already moved is skipped rather than re-moved.

Two hazards this module exists specifically to close, beyond the obvious file
moves:

- An agent's env file (copied verbatim would be a bug, not a fix) holds
  absolute paths into the legacy tree — at minimum
  `BUZZ_ACP_SYSTEM_PROMPT_FILE`, and for an agent with an MCP server that
  needed a wrapper script, `BUZZ_ACP_MCP_COMMAND` too. The migration deletes
  neither path by moving the *state*, but it does relocate the files those
  variables point at, so a byte-for-byte copy would leave the migrated agent
  pointing at files that no longer exist where they used to. `_rewrite_env`
  below rewrites exactly those two paths per agent, nothing else.
- A legacy community id was never validated at creation time (Task 7 added
  that check only going forward, deliberately not retrofitting it onto
  already-saved communities — a stricter model would have made existing
  on-disk data unloadable). `units.unit_name` validates unconditionally, so
  reaching it mid-migration with a bad id would abort after some agents had
  already moved. `plan()` validates every legacy community id up front and
  refuses the whole migration before writing anything if any is unusable.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from buzz_fleet import atomic, connect, paths, state, systemd, units
from buzz_fleet.models import Agent, Community
from buzz_fleet.proc import CommandRunner

LAYOUT_VERSION = 2


@dataclass(frozen=True)
class Step:
    # Unit names are carried as fields rather than re-parsed out of
    # `description`: the description is for humans and will get reworded, and a
    # migration that renames the wrong unit is not a bug you notice quickly.
    kind: str
    description: str
    source: Path | None = None
    destination: Path | None = None
    old_unit: str | None = None
    new_unit: str | None = None


def _version_path() -> Path:
    return paths.state_dir() / "layout-version"


def _legacy_agent_ids(legacy: Path, community_id: str) -> list[str]:
    directory = legacy / "communities" / community_id / "agents"
    return sorted(p.stem for p in directory.glob("*.json")) if directory.exists() else []


def _legacy_community_ids(legacy: Path) -> list[str]:
    directory = legacy / "communities"
    return sorted(p.stem for p in directory.glob("*.json")) if directory.exists() else []


def _active_legacy_units(runner: CommandRunner, legacy: Path) -> list[str]:
    active = []
    for community_id in _legacy_community_ids(legacy):
        for agent_id in _legacy_agent_ids(legacy, community_id):
            unit = f"buzz-agent@{agent_id}.service"
            if runner.run(["systemctl", "--user", "is-active", unit]).stdout.strip() == "active":
                active.append(unit)
    return active


def _legacy_work_dir(agent_id: str) -> Path:
    # The pre-migration WORK_DIR constant (removed by Task 4) — see that
    # task's diff. Not `paths.legacy_dir()`-relative: it always lived under
    # `~/.local/share`, never under `~/.config`, even before XDG.
    return Path.home() / ".local" / "share" / "buzz-fleet" / "work" / agent_id


def _legacy_prompt_path(legacy: Path, agent_id: str) -> Path:
    return legacy / "agents" / f"{agent_id}.prompt.md"


def _load_legacy_agent(source: Path) -> Agent:
    """`Agent.model_validate` on a legacy file as-is can fail on a field that
    didn't exist when the file was written — `created_at` is required today
    but wasn't always. Backfilling it from the file's own mtime is the closest
    available approximation of when the agent was actually created, and it
    only ever fills a gap; a file that already has the field is untouched.
    """
    data = json.loads(source.read_text())
    data.setdefault(
        "created_at", datetime.fromtimestamp(source.stat().st_mtime, tz=UTC).isoformat()
    )
    return Agent.model_validate(data)


def _rewrite_env(content: str, *, legacy: Path, agent_id: str, key: str) -> str:
    """Rewrite absolute paths inside an env file's text that point into the
    legacy tree, mapping each to where the migration actually puts it.

    Only two paths ever appear in a legacy env file and point at something
    the migration moves: the system prompt file (`BUZZ_ACP_SYSTEM_PROMPT_FILE`,
    every agent) and, for an agent whose MCP server needed a wrapper script,
    that script's own directory (`BUZZ_ACP_MCP_COMMAND`, under the agent's
    work dir). A plain substring replacement of each old path for its new one
    covers both — the prompt path appears whole, and the work-dir path appears
    as a prefix of the wrapper's full path (e.g. `.../work/reviewer/mcp-x.sh`)
    with the script's own filename left as-is. Nothing else in a legacy env
    file is a path the migration relocates, so nothing else is touched.
    """
    old_prompt = _legacy_prompt_path(legacy, agent_id)
    new_prompt = systemd.agent_prompt_path(key)
    old_work = _legacy_work_dir(agent_id)
    new_work = systemd.work_dir(key)
    content = content.replace(str(old_prompt), str(new_prompt))
    content = content.replace(str(old_work), str(new_work))
    return content


def plan(runner: CommandRunner) -> list[Step]:
    """Steps still outstanding. An already-migrated machine plans nothing.

    Validates every legacy community id up front against today's rules
    (`connect.validate_community_id`) before computing a single step — a bad
    id reaching `units.unit_name` mid-migration would abort after some agents
    had already moved, which is worse than refusing the whole run before
    touching anything.
    """
    legacy = paths.legacy_dir()
    if not (legacy / "communities").exists():
        return []
    # The per-asset checks below are each individually idempotent (a file
    # already at its destination is skipped), but the unit-rename step has no
    # destination to check against — `disable --now`/`enable --now` on an
    # already-renamed unit is harmless to *run*, yet planning it forever would
    # mean a fully migrated machine never reports "nothing to do". The layout
    # marker is the one signal that means the whole machine, not any single
    # asset, is done.
    version = _version_path()
    if version.exists() and version.read_text().strip() == str(LAYOUT_VERSION):
        return []

    community_ids = _legacy_community_ids(legacy)
    for community_id in community_ids:
        try:
            connect.validate_community_id(community_id)
        except ValueError as e:
            raise RuntimeError(
                f"legacy community id {community_id!r} is not valid under current rules "
                f"({e}) — refusing the whole migration rather than moving some agents and "
                "stopping partway. Fix or remove this community's files and try again."
            ) from e

    steps: list[Step] = []
    for community_id in community_ids:
        target = paths.state_dir() / "communities" / f"{community_id}.json"
        if not target.exists():
            steps.append(
                Step(
                    "community",
                    f"split {community_id} into state and secrets",
                    legacy / "communities" / f"{community_id}.json",
                    target,
                )
            )
        for agent_id in _legacy_agent_ids(legacy, community_id):
            key = units.instance_key(community_id, agent_id)
            agent_target = (
                paths.state_dir() / "communities" / community_id / "agents" / f"{agent_id}.json"
            )
            if not agent_target.exists():
                steps.append(
                    Step(
                        "agent",
                        f"split {key} into state and secrets",
                        legacy / "communities" / community_id / "agents" / f"{agent_id}.json",
                        agent_target,
                    )
                )
            env_source = legacy / "agents" / f"{agent_id}.env"
            env_target = paths.secrets_dir() / "units" / f"{key}.env"
            if env_source.exists() and not env_target.exists():
                steps.append(Step("env", f"move env file for {key}", env_source, env_target))
            prompt_source = _legacy_prompt_path(legacy, agent_id)
            prompt_target = paths.state_dir() / "units" / f"{key}.prompt.md"
            if prompt_source.exists() and not prompt_target.exists():
                steps.append(Step("prompt", f"move prompt for {key}", prompt_source, prompt_target))
            work_source = _legacy_work_dir(agent_id)
            work_target = systemd.work_dir(key)
            if work_source.exists() and not work_target.exists():
                steps.append(Step("work", f"move workdir for {key}", work_source, work_target))
            # The legacy unit name is deliberately unqualified: that is what it
            # is actually called on a pre-migration machine.
            steps.append(
                Step(
                    "unit",
                    f"rename buzz-agent@{agent_id} to buzz-agent@{key}",
                    old_unit=f"buzz-agent@{agent_id}.service",
                    new_unit=units.unit_name(key),
                )
            )
    return steps


def run(runner: CommandRunner, *, dry_run: bool = False) -> list[Step]:
    """Apply the outstanding steps. Returns what was applied — empty when the
    machine is already on the current layout."""
    legacy = paths.legacy_dir()
    steps = plan(runner)
    if not steps or dry_run:
        return steps

    still_active = _active_legacy_units(runner, legacy)
    if still_active:
        raise RuntimeError(
            f"these units are still active: {', '.join(still_active)}. "
            "Stop them before migrating — moving an env file out from under a "
            "running unit leaves it holding a key it cannot re-read."
        )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = legacy.parent / f"{legacy.name}.bak-{stamp}"
    shutil.copytree(legacy, backup)

    for step in steps:
        if step.kind == "community":
            assert step.source is not None and step.destination is not None
            # Re-saving through state.save_community is what performs the
            # secrets split: the legacy file has them inline.
            state.save_community(Community.model_validate(json.loads(step.source.read_text())))
        elif step.kind == "agent":
            assert step.source is not None and step.destination is not None
            state.save_agent(_load_legacy_agent(step.source))
        elif step.kind == "env":
            assert step.source is not None and step.destination is not None
            # destination is `.../units/<community>:<agent>.env` — the key is
            # its stem, recovered rather than threaded through a new Step
            # field so the frozen dataclass stays exactly the shape the task
            # interface specifies.
            key = step.destination.stem
            _, agent_id = units.split_key(key)
            content = _rewrite_env(
                step.source.read_text(), legacy=legacy, agent_id=agent_id, key=key
            )
            atomic.write_secure(step.destination, content, mode=0o600)
        elif step.kind == "prompt":
            assert step.source is not None and step.destination is not None
            atomic.write_secure(step.destination, step.source.read_text(), mode=0o600)
        elif step.kind == "work":
            assert step.source is not None and step.destination is not None
            step.destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(step.source, step.destination)
        elif step.kind == "unit":
            assert step.old_unit is not None and step.new_unit is not None
            runner.run(["systemctl", "--user", "disable", "--now", step.old_unit])
            runner.run(["systemctl", "--user", "enable", "--now", step.new_unit])

    atomic.write_secure(_version_path(), f"{LAYOUT_VERSION}\n", mode=0o600)
    return steps
