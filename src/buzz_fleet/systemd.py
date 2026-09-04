"""Systemd template unit + per-agent env/prompt file management."""

from __future__ import annotations

import getpass
import os
from pathlib import Path
from typing import TYPE_CHECKING

from buzz_fleet.models import Agent, Community

if TYPE_CHECKING:
    # Task 7 creates buzz_fleet.proc; guard this import so Task 6 doesn't
    # depend on a module that doesn't exist yet at runtime — only the type
    # checker needs it, `ensure_template_unit_installed` only calls
    # `runner.run(...)` (duck-typed).
    from buzz_fleet.proc import CommandRunner

AGENTS_DIR = Path.home() / ".config" / "buzz-fleet" / "agents"
TEMPLATE_UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / "buzz-agent@.service"

_HARNESS_COMMAND = {
    "claude": "claude-agent-acp",
    "codex": "codex-acp",
    "pi": "pi-acp",
    "goose": "goose",
}

# A --user unit, not a system unit — no root anywhere in buzz-fleet (spec Open
# Question 2, resolved this way): no `User=` line (it always runs as whoever
# owns this systemd --user instance), `WantedBy=default.target` (the --user
# equivalent of multi-user.target), and the env path matches AGENTS_DIR above.
# Requires `loginctl enable-linger <user>` once so the --user instance (and
# this unit) keeps running after the SSH session that created it ends — see
# Task 12 Step 1.
TEMPLATE_UNIT = f"""[Unit]
Description=Buzz headless agent (%i)
After=network-online.target

[Service]
EnvironmentFile={AGENTS_DIR}/%i.env
ExecStart=/usr/local/bin/buzz-acp
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def render_template_unit() -> str:
    return TEMPLATE_UNIT


def ensure_template_unit_installed(runner: CommandRunner) -> None:
    """Write the shared buzz-agent@.service template if missing or stale, then daemon-reload."""
    current = TEMPLATE_UNIT_PATH.read_text() if TEMPLATE_UNIT_PATH.exists() else None
    if current == TEMPLATE_UNIT:
        return
    TEMPLATE_UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    TEMPLATE_UNIT_PATH.write_text(TEMPLATE_UNIT)
    runner.run(["systemctl", "--user", "daemon-reload"])


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
    return source.path.read_text()


def write_agent_files(
    agent: Agent,
    community: Community,
    anthropic_api_key: str | None,
    openai_api_key: str | None,
) -> None:
    prompt_path = agent_prompt_path(agent.id)
    _write_secure(prompt_path, resolve_prompt_text(agent))

    lines = [
        f"BUZZ_PRIVATE_KEY={agent.private_key.get_secret_value()}",
        f"BUZZ_RELAY_URL={community.relay_url}",
        f"BUZZ_ACP_AGENT_COMMAND={_HARNESS_COMMAND[agent.harness]}",
        f"BUZZ_ACP_SYSTEM_PROMPT_FILE={prompt_path}",
    ]
    if agent.team_instructions:
        lines.append(f"BUZZ_ACP_TEAM_INSTRUCTIONS={agent.team_instructions}")
    if anthropic_api_key:
        lines.append(f"ANTHROPIC_API_KEY={anthropic_api_key}")
    if openai_api_key:
        lines.append(f"OPENAI_API_KEY={openai_api_key}")

    _write_secure(agent_env_path(agent.id), "\n".join(lines) + "\n")
