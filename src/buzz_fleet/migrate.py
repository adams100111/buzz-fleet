"""Move a machine from the pre-XDG layout onto the current one, once.

Every step checks its own postcondition, so resuming after an interruption is
just running it again — which matters, because an interrupted run leaves some
agents on old unit names and some on new. Migration is per-agent for the same
reason: an agent already moved is skipped rather than re-moved.

Hazards this module exists specifically to close, beyond the obvious file
moves:

- An agent's env file (copied verbatim would be a bug, not a fix) holds
  absolute paths into the legacy tree — at minimum
  `BUZZ_ACP_SYSTEM_PROMPT_FILE`, and depending on the agent's harness/MCP
  setup, `BUZZ_ACP_MCP_COMMAND` and/or `PI_CODING_AGENT_DIR` too, all
  ultimately derived from the same two legacy locations. The migration
  relocates the files those variables point at, so a byte-for-byte copy
  would leave the migrated agent pointing at files that no longer exist.
  `_rewrite_env` below rewrites every occurrence of those two legacy paths,
  wherever they appear in the file's text.
- A legacy community id (or agent id) was never validated at creation time
  (Task 7 added that check only going forward, deliberately not
  retrofitting it onto already-saved communities — a stricter model would
  have made existing on-disk data unloadable). `units.unit_name` validates
  unconditionally, so reaching it mid-migration with a bad id would abort
  after some agents had already moved. `plan()` validates every legacy
  community id and every legacy agent id up front and refuses the whole
  migration before writing anything if any is unusable.
- The one-shot "is anything left to do" check must never depend on a
  `CommandRunner`'s own memory: a real `systemctl` genuinely remembers what
  it enabled between separate invocations of this program, but nothing
  guarantees a `CommandRunner` passed to two different `run()` calls (in
  tests, or across two separate `buzz-fleet migrate` invocations) does.
  Every step's postcondition, `unit` included, is therefore something on
  disk — see `_unit_marker_path`.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from buzz_fleet import atomic, connect, paths, state, systemctl_client, systemd, units
from buzz_fleet.models import Agent, Community
from buzz_fleet.proc import CommandRunner

# Bumped whenever the on-disk layout changes in a way this module's `plan()`
# needs to know how to move a machine off of. NOTE for a future bump: a
# `LAYOUT_VERSION = 3` migration must NOT assume `plan()`/`run()` here handle
# it — this module only ever migrates the pre-XDG (implicitly "1") layout to
# "2". A v2-to-v3 migration is a new, separate set of steps; this file's
# per-asset existence checks (not a whole-machine version gate — see the
# module docstring) mean a v2 machine already reports "nothing to do" for
# everything covered here regardless of what a future version adds. It is
# currently a write-only artifact — nothing in this codebase reads it back —
# kept solely because `test_writes_a_layout_version_marker` requires it and
# a future v2->v3 migration will want a place to check "did v2 finish".
LAYOUT_VERSION = 2

# The ONLY raw `systemctl is-active` answers this migration accepts as safe
# to act on. Deliberately an allowlist of literal strings, not a lookup
# through `systemctl_client`'s `AgentStatus`/`_STATE_MAP`: that map is a
# *display*-oriented coarsening that folds "deactivating" into the same
# bucket as "inactive" purely for a status column, and "deactivating" is
# NOT safe here — an auto-restarting (`Restart=on-failure`) unit transits
# `deactivating -> activating -> active` on every single restart, so a
# migration that accepts "deactivating" guards only half of the exact
# crash-loop hazard CLAUDE.md's 775-restart incident describes. An
# allowlist also refuses "" and every unrecognised string BY CONSTRUCTION
# — "unknown is unsafe" no longer depends on a shared enum's default.
_SAFE_TO_MIGRATE_RAW_STATES = frozenset({"inactive", "failed"})


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


def _unit_marker_path(key: str) -> Path:
    """On-disk proof that `key`'s unit step has already run.

    Not derived from `systemctl is-enabled` (a real, persistent fact about
    the machine) precisely because this module's own idempotency contract —
    "running it again is safe" — must hold for two entirely separate
    `CommandRunner` instances, including two fakes in a test that share no
    state with each other. A file next to the prompt/env files this same
    step's agent already owns is exactly as durable as those, and is
    written only after `systemctl_client.enable_now` has actually succeeded.
    """
    return paths.state_dir() / "units" / f"{key}.unit-migrated"


def _legacy_agent_ids(legacy: Path, community_id: str) -> list[str]:
    directory = legacy / "communities" / community_id / "agents"
    return sorted(p.stem for p in directory.glob("*.json")) if directory.exists() else []


def _legacy_community_ids(legacy: Path) -> list[str]:
    directory = legacy / "communities"
    return sorted(p.stem for p in directory.glob("*.json")) if directory.exists() else []


def _active_legacy_units(runner: CommandRunner, legacy: Path) -> list[str]:
    """Legacy units not safe to migrate out from under.

    Uses `systemctl_client.raw_state_of_unit` — the unclassified, literal
    `is-active` answer — rather than `status_of_unit`'s `AgentStatus`. The
    systemctl call itself still lives inside `systemctl_client`, not
    re-implemented here; only the safety judgment (the
    `_SAFE_TO_MIGRATE_RAW_STATES` allowlist above) is migrate's own, kept
    deliberately separate from `_STATE_MAP`'s display-oriented grouping so
    "deactivating" can't quietly ride along with "inactive" the way it does
    for a status column. See that constant's own comment for why.
    """
    unsafe = []
    for community_id in _legacy_community_ids(legacy):
        for agent_id in _legacy_agent_ids(legacy, community_id):
            unit = f"buzz-agent@{agent_id}.service"
            if systemctl_client.raw_state_of_unit(runner, unit) not in _SAFE_TO_MIGRATE_RAW_STATES:
                unsafe.append(unit)
    return unsafe


def _legacy_work_dir(agent_id: str) -> Path:
    # The pre-migration WORK_DIR constant (removed by Task 4) — see that
    # task's diff. Not `paths.legacy_dir()`-relative: it always lived under
    # `~/.local/share`, never under `~/.config`, even before XDG.
    return Path.home() / ".local" / "share" / "buzz-fleet" / "work" / agent_id


def _legacy_prompt_path(legacy: Path, agent_id: str) -> Path:
    return legacy / "agents" / f"{agent_id}.prompt.md"


def _already_migrated_agent_ids() -> set[str]:
    """Bare agent ids (not community-qualified) that already have an env
    file under the new secrets tree.

    Used only to tell a genuinely stranded legacy env file (one that was
    never migrated, with no community context left to place it) apart from
    one that's simply left over after a successful migration plus a
    partial cleanup — the README's own documented order deletes
    `communities/` before `agents/`, so a machine mid-cleanup legitimately
    has agent env files with no `communities/` directory at all.
    """
    units_dir = paths.secrets_dir() / "units"
    if not units_dir.exists():
        return set()
    ids = set()
    for p in units_dir.glob("*.env"):
        try:
            _, agent_id = units.split_key(p.stem)
        except ValueError:
            continue
        ids.add(agent_id)
    return ids


def _unique_backup_path(legacy: Path) -> Path:
    """A `.bak-<timestamp>` sibling of `legacy` guaranteed not to already
    exist. Microsecond, not second, resolution: two `migrate` invocations
    within the same second (a scripted retry, or — the case this was
    actually caught by — two calls in one test) would otherwise collide on
    the exact same name and `shutil.copytree` would raise `FileExistsError`
    on the SECOND, real migration's backup step, before it had done
    anything else. The counter suffix is additional insurance for a
    filesystem/clock combination coarser than microseconds; it should never
    actually trigger in practice.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    candidate = legacy.parent / f"{legacy.name}.bak-{stamp}"
    suffix = 2
    while candidate.exists():
        candidate = legacy.parent / f"{legacy.name}.bak-{stamp}-{suffix}"
        suffix += 1
    return candidate


def _load_legacy_agent(source: Path) -> Agent:
    """`Agent.model_validate` on a legacy file as-is can fail on a field that
    didn't exist when the file was written — `created_at` is required today
    but wasn't always. Backfilling it from the file's own mtime is the
    closest available approximation of when the agent was actually created,
    and it only ever fills a gap; a file that already has the field is
    untouched. Loud, not silent: nothing in this codebase reads `created_at`
    today, but the recycling work `CLAUDE.md`'s "Known gaps" section
    describes as unbuilt is exactly the kind of consumer a fabricated
    timestamp would later mislead, so the operator gets a chance to correct
    it by hand.

    Any OTHER missing or invalid required field is a real, unrecoverable
    problem with this one legacy file — raised as `RuntimeError` naming the
    file and pydantic's own field-level detail, rather than letting a bare
    `ValidationError` abort `run()` mid-apply with no indication of which
    file, or which field, caused it.
    """
    data = json.loads(source.read_text())
    backfilled = "created_at" not in data
    if backfilled:
        mtime = datetime.fromtimestamp(source.stat().st_mtime, tz=UTC)
        data["created_at"] = mtime.isoformat()
    try:
        agent = Agent.model_validate(data)
    except ValidationError as e:
        raise RuntimeError(
            f"legacy agent file {source} is missing or has invalid required data: {e}"
        ) from e
    if backfilled:
        # stderr, not stdout: the CLI's own step-by-step listing (each
        # Step's `description`) goes to stdout, and interleaving this note
        # into that would make both harder to read or script against.
        print(
            f"note: {source} had no 'created_at' — using its own mtime "
            f"({mtime.isoformat()}) for {agent.community_id}:{agent.id}. Nothing reads "
            "this field today; fix it by hand afterward if the true creation date ever "
            "matters (see CLAUDE.md's unbuilt recycling work).",
            file=sys.stderr,
        )
    return agent


def _rewrite_env(content: str, *, legacy: Path, agent_id: str, key: str) -> str:
    """Rewrite absolute paths inside an env file's text that point into the
    legacy tree, mapping each to where the migration actually puts it.

    Two legacy locations are ever referenced from an agent's env file, and
    the migration relocates both: the system prompt file, and (for an agent
    whose MCP server needed a wrapper script, or a Pi-harness agent's own
    private `.pi-agent` directory) the agent's work dir. This is a plain
    substring replacement of each old path for its new one across the WHOLE
    file text — not scoped to a specific variable name — so it correctly
    rewrites every variable actually derived from either path:
    `BUZZ_ACP_SYSTEM_PROMPT_FILE` (the prompt path, in full) and, as a
    prefix of a longer value, `BUZZ_ACP_MCP_COMMAND` (`<work dir>/mcp-
    <name>.sh`) and `PI_CODING_AGENT_DIR` (`<work dir>/.pi-agent`) alike.
    Nothing else in a legacy env file is a path this migration relocates, so
    nothing else is touched.
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

    Validates every legacy community id AND every legacy agent id up front
    (`connect.validate_community_id`, `units.validate_instance_key`) before
    computing a single step — either reaching `units.unit_name` mid-migration
    with a bad one would abort after some agents had already moved, which is
    worse than refusing the whole run before touching anything.

    Always scans the full legacy tree rather than short-circuiting off a
    whole-machine "already done" flag: every step (`unit` included, via
    `_unit_marker_path`) has its own on-disk postcondition, so a community or
    agent added to the legacy tree by an older binary *after* a previous
    migration completed is still found and planned here, not silently
    ignored forever.

    An agent whose `_unit_marker_path` already exists is skipped in full —
    not just its `unit` step. `manager.delete_agent` only ever touches the
    NEW layout (state JSON, env file, prompt); it knows nothing about the
    still-present legacy files or this marker, and the legacy tree is
    deliberately kept around until the operator cleans it up by hand. Without
    this, `plan()` re-deriving every step straight from the legacy tree on
    every call would let "migrate, then delete an agent, then migrate again"
    silently RECREATE that agent — private key included — directly undoing
    the deletion.

    `runner` is used only to query state (`systemctl is-active`, inside
    `run()`'s active-unit refusal, not here) — `plan()` itself never enables,
    disables, or otherwise changes anything a real `systemctl` would
    remember, which is what lets `--dry-run` call this with no side effects.
    """
    legacy = paths.legacy_dir()
    if not (legacy / "communities").exists():
        stray_agents = legacy / "agents"
        if stray_agents.exists():
            already_migrated = _already_migrated_agent_ids()
            genuinely_stranded = [
                p.stem for p in stray_agents.glob("*.env") if p.stem not in already_migrated
            ]
            if genuinely_stranded:
                raise RuntimeError(
                    f"{stray_agents} holds agent env file(s) for {genuinely_stranded} but "
                    f"{legacy / 'communities'} does not exist — cannot determine which "
                    "community they belong to, so refusing to report 'nothing to do' while "
                    "they're stranded. Move or remove them, or restore the missing "
                    "communities directory, before migrating."
                )
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
        for agent_id in _legacy_agent_ids(legacy, community_id):
            key = units.instance_key(community_id, agent_id)
            try:
                units.validate_instance_key(key)
            except ValueError as e:
                raise RuntimeError(
                    f"legacy agent id {agent_id!r} in community {community_id!r} produces an "
                    f"invalid unit instance key ({e}) — refusing the whole migration rather "
                    "than moving some agents and stopping partway. Fix or remove this "
                    "agent's files and try again."
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
            if _unit_marker_path(key).exists():
                # Fully migrated already (see the "MUST-FIX 2" paragraph in
                # this function's own docstring) -- skip this agent
                # entirely, not just its unit step, so a legacy tree left in
                # place (as documented) can never resurrect a since-deleted
                # agent from it.
                continue
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
            # Same helper `_rewrite_env` uses for the new location, not a
            # second, independently-spelled path -- they used to agree only
            # by coincidence.
            prompt_target = systemd.agent_prompt_path(key)
            if prompt_source.exists() and not prompt_target.exists():
                steps.append(Step("prompt", f"move prompt for {key}", prompt_source, prompt_target))
            work_source = _legacy_work_dir(agent_id)
            work_target = systemd.work_dir(key)
            if work_source.exists() and not work_target.exists():
                steps.append(Step("work", f"move workdir for {key}", work_source, work_target))
            # The legacy unit name is deliberately unqualified: that is what
            # it is actually called on a pre-migration machine. Unconditional
            # here (unlike the four steps above): reaching this line already
            # means `_unit_marker_path(key)` doesn't exist yet (the `continue`
            # above would have skipped this agent otherwise), so the unit
            # step is always still outstanding.
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

    still_unsafe = _active_legacy_units(runner, legacy)
    if still_unsafe:
        raise RuntimeError(
            f"these units are still active (or in an unrecognised/transitional state): "
            f"{', '.join(still_unsafe)}. Stop them before migrating — moving an env file "
            "out from under a running (or starting, reloading, or unresponsive-systemctl) "
            "unit leaves it holding a key it cannot re-read."
        )

    backup = _unique_backup_path(legacy)
    shutil.copytree(legacy, backup, symlinks=True)
    # The legacy tree is commonly group/other-readable (a real machine's
    # `~/.config/buzz-fleet` was found at 0755, its `communities/`/`agents/`
    # subdirectories too) — `copytree` preserves that. The backup holds every
    # secret in plaintext (see README's "Upgrading from 0.8.x"), so its root
    # is tightened regardless of what the source tree's own modes were.
    backup.chmod(0o700)

    # Install/refresh the shared template unit BEFORE enabling anything below
    # points at it — every `unit` step's `enable --now` resolves against
    # whatever's on disk at that moment, and the legacy template's
    # `EnvironmentFile=` still points at the pre-migration `agents/%i.env`
    # path, which none of these agents' env files live at anymore.
    systemd.ensure_template_unit_installed(runner)

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
            content = step.source.read_text()
            atomic.write_secure(step.destination, content, mode=0o600)
            # Verified before deleting the only other copy: `atomic.
            # write_secure` already fsyncs durably, but re-reading it back
            # turns "silently wrote something else" into a loud failure
            # instead of a quietly lost prompt. Deleting the legacy source
            # afterward (unlike env/community/agent files, which are left in
            # place — see README's "Upgrading from 0.8.x") is safe here:
            # prompt text isn't a secret, its old path is genuinely dead
            # once copied, and it is what makes a test asserting the
            # migrated env file's BUZZ_ACP_SYSTEM_PROMPT_FILE actually
            # resolves load-bearing rather than trivially true (a verbatim,
            # un-rewritten copy would point at a path that no longer exists).
            if step.destination.read_text() != content:
                raise RuntimeError(
                    f"verification failed writing {step.destination} from {step.source} — "
                    "leaving the legacy source in place"
                )
            step.source.unlink()
        elif step.kind == "work":
            assert step.source is not None and step.destination is not None
            step.destination.parent.mkdir(parents=True, exist_ok=True)
            # Copy-then-rename, not a direct `copytree` into the final path:
            # `copytree` creates its destination up front and fills it
            # incrementally, so an interruption mid-copy leaves a real,
            # existing (but truncated) directory at `step.destination` —
            # `plan()`'s postcondition is exactly "does this path exist",
            # so a resumed run would then silently skip finishing it. A
            # sibling temp directory plus a same-filesystem `os.replace`
            # makes the whole thing atomic: either the final path doesn't
            # exist yet (redo it) or it's the complete copy (skip it),
            # never something in between. Any stale temp dir from a prior
            # interrupted attempt is cleared first so the retry starts
            # clean rather than erroring on an already-existing directory.
            tmp_dest = step.destination.parent / f".{step.destination.name}.migrating"
            # `.exists()` alone would miss a dangling symlink AND would raise
            # `NotADirectoryError` from `rmtree` if the leftover is a plain
            # file or a symlink to one (nothing prevents a killed run's
            # leftover from being either) -- check what it actually is.
            if tmp_dest.is_symlink() or tmp_dest.is_file():
                tmp_dest.unlink()
            elif tmp_dest.is_dir():
                shutil.rmtree(tmp_dest)
            shutil.copytree(step.source, tmp_dest, symlinks=True)
            os.replace(tmp_dest, step.destination)
        elif step.kind == "unit":
            assert step.old_unit is not None and step.new_unit is not None
            prefix, _, suffix = units.TEMPLATE.partition("@")
            key = step.new_unit.removeprefix(f"{prefix}@").removesuffix(suffix)
            result = runner.run(["systemctl", "--user", "disable", "--now", step.old_unit])
            if result.returncode != 0:
                # Tolerated, not raised: on a resumed migration the legacy
                # unit may already be disabled (or gone entirely, if the
                # operator removed it by hand) — `systemctl disable` on a
                # unit with nothing to do can return nonzero for that. The
                # goal state ("the old unit isn't running") already holds
                # either way, and treating this as fatal would block a
                # legitimate resume for no benefit. `enable_now` just below,
                # by contrast, is NOT tolerated — a real failure to start the
                # new unit must abort the migration, not be swallowed.
                pass
            systemctl_client.enable_now(runner, key)
            atomic.write_secure(_unit_marker_path(key), "migrated\n", mode=0o600)

    atomic.write_secure(_version_path(), f"{LAYOUT_VERSION}\n", mode=0o600)
    return steps
