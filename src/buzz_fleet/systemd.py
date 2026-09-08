"""Systemd template unit + per-agent env/prompt file management."""

from __future__ import annotations

import getpass
import json
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import SecretStr

from buzz_fleet.buzz_acp import BUZZ_ACP_DIR, BUZZ_ACP_PATH
from buzz_fleet.harnesses import (
    PI_AGENT_TEMPLATE_DIR,
    PI_MCP_ADAPTER_VERSION,
    resolve_adapter_command,
)
from buzz_fleet.models import MCP_SERVER_NAME_RE, Agent, Community
from buzz_fleet.orchestration.instructions import apply_coordination_block

if TYPE_CHECKING:
    # Task 7 creates buzz_fleet.proc; guard this import so Task 6 doesn't
    # depend on a module that doesn't exist yet at runtime — only the type
    # checker needs it, `ensure_template_unit_installed` only calls
    # `runner.run(...)` (duck-typed).
    from buzz_fleet.proc import CommandRunner

AGENTS_DIR = Path.home() / ".config" / "buzz-fleet" / "agents"
TEMPLATE_UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / "buzz-agent@.service"
WORK_DIR = Path.home() / ".local" / "share" / "buzz-fleet" / "work"

# A --user unit, not a system unit — no root anywhere in buzz-fleet (spec Open
# Question 2, resolved this way): no `User=` line (it always runs as whoever
# owns this systemd --user instance), `WantedBy=default.target` (the --user
# equivalent of multi-user.target), and the env path matches AGENTS_DIR above.
# Requires `loginctl enable-linger <user>` once so the --user instance (and
# this unit) keeps running after the SSH session that created it ends — see
# Task 12 Step 1.
#
# ExecStart points at BUZZ_ACP_PATH (a per-user path, not /usr/local/bin) so
# `ensure_buzz_acp_installed()` can install it automatically with no sudo
# prompt — a unit pointed at a root-owned path could never self-heal without
# asking the user to run something manually. Real incident: a machine that
# never separately installed buzz-acp had this crash-loop status=203/EXEC
# (exec target doesn't exist) hundreds of times before this was caught.
TEMPLATE_UNIT = f"""[Unit]
Description=Buzz headless agent (%i)
After=network-online.target

[Service]
EnvironmentFile={AGENTS_DIR}/%i.env
Environment=PATH={BUZZ_ACP_DIR}:/usr/local/bin:/usr/bin:/bin
WorkingDirectory={WORK_DIR}/%i
ExecStart={BUZZ_ACP_PATH}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def render_template_unit() -> str:
    return TEMPLATE_UNIT


def ensure_template_unit_installed(runner: CommandRunner) -> bool:
    """Write the shared buzz-agent@.service template if missing or stale, then daemon-reload.

    Returns True when the unit file was actually written — a `--user` unit
    only picks up a new `Environment=`/`WorkingDirectory=` line on restart,
    so callers use this to decide whether to restart already-running agents.
    """
    current = TEMPLATE_UNIT_PATH.read_text() if TEMPLATE_UNIT_PATH.exists() else None
    if current == TEMPLATE_UNIT:
        return False
    TEMPLATE_UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    TEMPLATE_UNIT_PATH.write_text(TEMPLATE_UNIT)
    runner.run(["systemctl", "--user", "daemon-reload"])
    return True


def ensure_linger_enabled(runner: CommandRunner) -> None:
    """Enable `loginctl` lingering for the current user if it isn't already.

    Without lingering, every `buzz-agent@*` --user unit dies the moment the
    SSH session that created it ends — silently defeating the entire point
    of running agents on a headless box. This is a one-time, per-user,
    per-host setting, not something to repeat on every install/run: this
    function is a no-op the moment it's already enabled.

    Enabling it can require privilege the current session doesn't have
    (some distros' polkit policy only allows an "active" — i.e. console,
    not SSH — session to self-enable). Try it directly first (works out of
    the box on many hosts); only ask for a manual `sudo` step if that
    genuinely fails, rather than requiring `sudo` unconditionally or
    failing later with a confusing `systemctl enable --now` error.
    """
    user = getpass.getuser()
    status = runner.run(["loginctl", "show-user", user, "--property=Linger", "--value"])
    if status.stdout.strip() == "yes":
        return
    enabled = runner.run(["loginctl", "enable-linger", user])
    if enabled.returncode != 0:
        raise RuntimeError(
            f"Could not enable lingering for '{user}' automatically (needed so your "
            f"agents keep running after you log out) — run this once, then try again: "
            f"sudo loginctl enable-linger {user}\n({enabled.stderr.strip()})"
        )


def agent_env_path(agent_id: str) -> Path:
    return AGENTS_DIR / f"{agent_id}.env"


def agent_prompt_path(agent_id: str) -> Path:
    return AGENTS_DIR / f"{agent_id}.prompt.md"


def _write_secure(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)


def resolve_prompt_text(agent: Agent) -> str:
    """Resolve an agent's system prompt text, reading from disk for persona_file sources.

    Exported (not module-private) so callers like AgentManager.create_agent can
    validate a persona_file path *before* triggering any side effects (e.g.
    publishing relay membership) that would be awkward to undo if the file
    turns out to be missing or unreadable.
    """
    source = agent.system_prompt_source
    if source.kind == "inline":
        assert source.text is not None
        return source.text
    assert source.path is not None
    raw = source.path.read_text()
    if raw.startswith("---\n"):
        closing = raw.find("\n---\n", 4)
        if closing != -1:
            return raw[closing + len("\n---\n") :]
    return raw


def _secret_value(value: str | SecretStr) -> str:
    """Unwrap a `dict[str, SecretStr]` entry.

    `Agent.model_copy(update=...)` — used throughout this codebase's own
    tests, and by TUI/manager code paths that build a changed copy without
    going through full model construction — does not re-run field
    validation, so a caller can (and in tests routinely does) hand back a
    plain `str` in a field typed `SecretStr`. Guard here rather than assume
    every value has already been coerced.
    """
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def _mcp_wrapper_path(agent_id: str, name: str) -> Path:
    # Defense in depth: `McpServer.name` is already validated at the model
    # level (see `models.MCP_SERVER_NAME_RE`'s docstring for why), but this
    # builds a filesystem path from it directly — assert again here so a
    # future code path that ever bypasses model validation (e.g.
    # `model_construct`, or a direct attribute assignment after
    # construction) cannot turn an unsafe name into a write outside this
    # agent's own directory.
    if not MCP_SERVER_NAME_RE.match(name):
        raise ValueError(f"unsafe MCP server name {name!r} — refusing to build a wrapper path from it")
    return WORK_DIR / agent_id / f"mcp-{name}.sh"


def _quote(value: str) -> str:
    """Always single-quote a shell word, unlike stdlib `shlex.quote` (which
    only quotes when a character actually requires it, e.g. leaves `boost:mcp`
    bare). The wrapper holds secrets end to end — every value is quoted
    unconditionally so its shape never silently depends on what a given
    secret happens to contain, and a value containing a `'` is escaped the
    same way `shlex.quote` itself does internally.
    """
    return "'" + value.replace("'", "'\"'\"'") + "'"


def write_mcp_wrapper(agent: Agent) -> Path | None:
    """Generate the wrapper script buzz-acp actually execs for `agent`'s MCP
    server, or None if the agent has none, or if it needs no wrapper at
    all. buzz-acp itself only ever passes a bare command with no args and
    no env — the wrapper exists *because* of that limitation, purely to
    supply args/env buzz-acp itself cannot pass (exported, then exec'd into
    the real command). A bare command with empty `args` and `env` needs
    none of that: `BUZZ_ACP_MCP_COMMAND` can point straight at `m.command`,
    so no wrapper is written for it (see `write_agent_files`'s fallback).

    0700, not 0600: it must remain executable, and it holds the same class
    of secret (an MCP server's own env vars, e.g. an API token) as the
    agent's private key — see `_write_secure`'s 0600 rationale.
    """
    if agent.mcp_server is None:
        return None
    m = agent.mcp_server
    if not (m.args or m.env):
        return None
    lines = ["#!/bin/sh"] + [f"export {k}={_quote(_secret_value(v))}" for k, v in m.env.items()]
    lines.append("exec " + " ".join(_quote(x) for x in [m.command, *m.args]))
    path = _mcp_wrapper_path(agent.id, m.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    try:
        os.write(fd, ("\n".join(lines) + "\n").encode())
    finally:
        os.close(fd)
    return path


def _cleanup_stale_mcp_wrappers(agent_id: str, *, keep: Path | None) -> None:
    """Remove any `mcp-*.sh` wrapper under this agent's WORK_DIR other than
    `keep` (the path `write_mcp_wrapper` just (re)wrote this call, or None if
    it wrote nothing this call).

    Without this, three paths leave a secret-bearing wrapper (mode 0700,
    `export TOKEN='<real secret>'`) behind forever: clearing the MCP server
    entirely (`agent.mcp_server` goes to None — `write_mcp_wrapper` is never
    even called), renaming it (the old `mcp-<oldname>.sh` is orphaned; the new
    name gets its own file), and editing it down to a bare command with no
    args/env (`write_mcp_wrapper` correctly returns None for that case, but a
    wrapper from before the edit may still be on disk). Glob-based rather than
    remembering the previous Agent's server name so it is correct even for a
    fresh env-file rewrite with no "previous" state to diff against — it just
    deletes whatever doesn't match what was (or wasn't) written this call.
    """
    agent_dir = WORK_DIR / agent_id
    if not agent_dir.is_dir():
        return
    for path in agent_dir.glob("mcp-*.sh"):
        if path != keep:
            path.unlink(missing_ok=True)


def _pi_agent_dir(agent_id: str) -> Path:
    return WORK_DIR / agent_id / ".pi-agent"


def _write_pi_agent_dir(agent: Agent) -> Path:
    """Pi has no MCP support of its own — it gets it through the
    `pi-mcp-adapter` extension instead, which reads its own private
    `PI_CODING_AGENT_DIR` (settings.json + mcp.json + skills/), isolating
    each Pi agent from the owner's own Pi setup entirely (spec fact 17, 5.13).

    `npm/` is copied in from the shared template dir (populated once by
    `harnesses.install_adapter("pi")`) so a brand-new agent's first turn
    doesn't need network access to fetch pi-mcp-adapter itself.
    """
    pi_dir = _pi_agent_dir(agent.id)
    pi_dir.mkdir(parents=True, exist_ok=True)
    (pi_dir / "skills").mkdir(parents=True, exist_ok=True)

    settings = {
        "defaultProjectTrust": "always",
        "packages": [f"npm:pi-mcp-adapter@{PI_MCP_ADAPTER_VERSION}"],
    }
    _write_secure(pi_dir / "settings.json", json.dumps(settings))

    mcp_json_path = pi_dir / "mcp.json"
    if agent.mcp_server is not None:
        m = agent.mcp_server
        mcp_json = {
            "mcpServers": {
                m.name: {
                    "command": m.command,
                    "args": list(m.args),
                    "env": {k: _secret_value(v) for k, v in m.env.items()},
                }
            }
        }
        _write_secure(mcp_json_path, json.dumps(mcp_json))
    else:
        mcp_json_path.unlink(missing_ok=True)

    template_npm = PI_AGENT_TEMPLATE_DIR / "npm"
    if template_npm.is_dir():
        shutil.copytree(template_npm, pi_dir / "npm", dirs_exist_ok=True)

    return pi_dir


def env_line(key: str, value: str) -> str:
    """One KEY=value line for a systemd EnvironmentFile.

    systemd stops an unquoted value at the first newline (real incident: a
    multi-paragraph BUZZ_ACP_TEAM_INSTRUCTIONS reached the agent as its first
    line only, 47 of 3,625 bytes). A double-quoted value may span lines; inside
    it `\\` escapes `\\` and `"`. Single-line values stay unquoted so existing
    env files are byte-identical.
    """
    if "\n" not in value:
        return f"{key}={value}"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"'


def write_agent_files(
    agent: Agent,
    community: Community,
    anthropic_api_key: str | None,
    openai_api_key: str | None,
    auth_tag: str | None = None,
) -> None:
    (WORK_DIR / agent.id).mkdir(parents=True, exist_ok=True)

    prompt_path = agent_prompt_path(agent.id)
    _write_secure(prompt_path, resolve_prompt_text(agent))

    lines = [
        env_line("BUZZ_PRIVATE_KEY", agent.private_key.get_secret_value()),
        env_line("BUZZ_RELAY_URL", community.relay_url),
        env_line("BUZZ_ACP_AGENT_COMMAND", resolve_adapter_command(agent.harness)),
        env_line("BUZZ_ACP_SYSTEM_PROMPT_FILE", str(prompt_path)),
    ]
    if auth_tag:
        # buzz-acp reads this and attaches it to its own NIP-42 AUTH event on
        # every relay connect — the ONLY way the relay's own agent_owner_pubkey
        # column (which backs third-party kind:9000 channel-add policy checks)
        # ever gets populated. Publishing this same tag on the agent's kind:0
        # profile (see visibility.py) is a *separate*, client-side-only
        # verification path some clients use for their own UI — it never
        # reaches the relay's own ownership record on its own.
        lines.append(env_line("BUZZ_AUTH_TAG", auth_tag))
    if community.owner_pubkey:
        # buzz-acp's own default is respond_to=owner-only — with no owner
        # configured at all, every event is silently dropped forever (a
        # real incident: every agent buzz-fleet ever created was running
        # but functionally inert until this was wired). AgentManager.
        # ensure_runtime_ready() backfills owner_pubkey on communities
        # saved before this field existed, so this is only ever unset for
        # a Community not yet round-tripped through that once.
        lines.append(env_line("BUZZ_ACP_AGENT_OWNER", community.owner_pubkey))
    if community.fleet_channel_id:
        lines.append(env_line("BUZZ_FLEET_CHANNEL", community.fleet_channel_id))
    if community.fleet_record:
        lines.append(env_line("BUZZ_FLEET_RETRIEVAL_KEY", community.fleet_record.retrieval_key))
    lines.append(env_line("BUZZ_ACP_TEAM_INSTRUCTIONS", apply_coordination_block(agent.team_instructions)))
    if agent.model:
        lines.append(env_line("BUZZ_ACP_MODEL", agent.model))
    if agent.parallelism is not None:
        lines.append(env_line("BUZZ_ACP_AGENTS", str(agent.parallelism)))
    if agent.idle_timeout_seconds is not None:
        lines.append(env_line("BUZZ_ACP_IDLE_TIMEOUT", str(agent.idle_timeout_seconds)))
    if agent.max_turn_duration_seconds is not None:
        lines.append(env_line("BUZZ_ACP_MAX_TURN_DURATION", str(agent.max_turn_duration_seconds)))
    lines.append(env_line("BUZZ_ACP_SESSION_POLICY", agent.session_policy or "thread"))
    lines.append(env_line("BUZZ_ACP_MAX_TURNS_PER_SESSION",
                          str(40 if agent.max_turns_per_session is None else agent.max_turns_per_session)))
    lines.append(env_line("BUZZ_ACP_HEARTBEAT_INTERVAL",
                          str(900 if agent.heartbeat_interval_seconds is None else agent.heartbeat_interval_seconds)))
    if agent.respond_to_allowlist:
        # buzz-acp only consults the allowlist when respond_to == "allowlist"
        # (BUZZ_ACP_RESPOND_TO, default "owner-only") — set both together so
        # the allowlist is never silently inert.
        lines.append(env_line("BUZZ_ACP_RESPOND_TO", "allowlist"))
        lines.append(env_line("BUZZ_ACP_RESPOND_TO_ALLOWLIST", ",".join(agent.respond_to_allowlist)))
    if anthropic_api_key:
        lines.append(env_line("ANTHROPIC_API_KEY", anthropic_api_key))
    if openai_api_key:
        lines.append(env_line("OPENAI_API_KEY", openai_api_key))

    # Generic per-agent env-var passthrough (spec 5.13) — written after the
    # API keys, same as they were, so a persona- or operator-supplied value
    # can override a preceding one if the keys collide.
    for key, value in (agent.env or {}).items():
        lines.append(env_line(key, _secret_value(value)))

    wrapper_path = write_mcp_wrapper(agent) if agent.mcp_server is not None else None
    _cleanup_stale_mcp_wrappers(agent.id, keep=wrapper_path)
    if agent.mcp_server is not None:
        if wrapper_path is not None:
            lines.append(env_line("BUZZ_ACP_MCP_COMMAND", str(wrapper_path)))
        else:
            # A bare command with no args/env needs no wrapper — buzz-acp
            # can run it directly.
            lines.append(env_line("BUZZ_ACP_MCP_COMMAND", agent.mcp_server.command))

    if agent.harness == "pi":
        pi_dir = _write_pi_agent_dir(agent)
        lines.append(env_line("PI_CODING_AGENT_DIR", str(pi_dir)))

    _write_secure(agent_env_path(agent.id), "\n".join(lines) + "\n")
