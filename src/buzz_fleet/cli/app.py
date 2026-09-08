"""The buzz-fleet Typer CLI."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

import typer

from buzz_fleet import __version__, harnesses, paths, state
from buzz_fleet.cli.fleet_commands import fleet_app, task_app, tasks_command
from buzz_fleet.connect import connect_and_save
from buzz_fleet.manager import AgentManager
from buzz_fleet.models import McpServer, SystemPromptSource
from buzz_fleet.proc import RealCommandRunner


def _parse_channel_ids(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    ids = [entry.strip() for entry in raw.split(",") if entry.strip()]
    for entry in ids:
        try:
            uuid.UUID(entry)
        except ValueError as e:
            typer.echo(f"Invalid channel id {entry!r} — must be a UUID.", err=True)
            raise typer.Exit(code=1) from e
    return ids or None


def _parse_env_pairs(pairs: list[str] | None, *, flag: str) -> dict[str, str]:
    """Parse repeated `KEY=VALUE` option values. A value is free to contain
    its own `=` (split("=", 1)) — only a missing `=` entirely is rejected.
    """
    result: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            typer.echo(f"Invalid {flag} value {pair!r} — expected KEY=VALUE.", err=True)
            raise typer.Exit(code=1)
        key, value = pair.split("=", 1)
        result[key] = value
    return result


def _parse_env_file(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    result: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "=" not in line:
            typer.echo(f"Invalid line in {path} — expected KEY=VALUE: {line!r}", err=True)
            raise typer.Exit(code=1)
        key, value = line.split("=", 1)
        result[key] = value
    return result


def _resolve_env(env: list[str] | None, env_file: Path | None) -> dict[str, str] | None:
    """`--env-file` first, then `--env` (repeatable) on top so an explicit
    per-invocation override wins over whatever the file says.
    """
    merged = {**_parse_env_file(env_file), **_parse_env_pairs(env, flag="--env")}
    return merged or None


def _resolve_mcp_server(
    mcp_name: str | None, mcp_command: str | None, mcp_arg: list[str] | None, mcp_env: list[str] | None
) -> McpServer | None:
    if mcp_name is None and mcp_command is None and not mcp_arg and not mcp_env:
        return None
    if mcp_name is None or mcp_command is None:
        typer.echo("--mcp-name and --mcp-command must be given together to attach an MCP server.", err=True)
        raise typer.Exit(code=1)
    return McpServer(
        name=mcp_name, command=mcp_command, args=mcp_arg or [],
        env=_parse_env_pairs(mcp_env, flag="--mcp-env"),  # type: ignore[arg-type]
    )


app = typer.Typer(help="buzz-fleet — manage headless Buzz agents", no_args_is_help=True)
agent_app = typer.Typer(help="Manage agent identities")
app.add_typer(agent_app, name="agent")
harness_app = typer.Typer(help="Detect and install harness adapters")
app.add_typer(harness_app, name="harness")
app.add_typer(fleet_app, name="fleet")
app.add_typer(task_app, name="task")
app.command("tasks")(tasks_command)
config_app = typer.Typer(help="Inspect configuration")
app.add_typer(config_app, name="config")


def _version_callback(show_version: bool) -> None:
    if show_version:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main_callback(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show the version and exit."),
    ] = False,
) -> None:
    pass


@app.command()
def connect(
    id: Annotated[str, typer.Option(help="Local id for this community, e.g. 'eltahir'")],
    relay: Annotated[str, typer.Option(help="Relay URL, e.g. wss://buzz.eltahir.me")],
    admin_nsec: Annotated[
        str,
        typer.Option(
            prompt=True,
            hide_input=True,
            help="Your own owner/admin nsec (prompted with masked input if omitted)",
        ),
    ],
) -> None:
    runner = RealCommandRunner()
    if not connect_and_save(runner, id, relay, admin_nsec):
        typer.echo("Could not authenticate against that relay with that key.", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Connected and saved community '{id}'.")


def _load_manager(community_id: str) -> AgentManager:
    community = state.load_community(community_id)
    if community is None:
        typer.echo(f"Unknown community '{community_id}' — run `buzz-fleet connect` first.", err=True)
        raise typer.Exit(code=1)
    return AgentManager(RealCommandRunner(), community)


@agent_app.command("create")
def agent_create(
    community: Annotated[str, typer.Option()],
    display_name: Annotated[str, typer.Option()],
    harness: Annotated[str, typer.Option()],
    prompt_file: Annotated[Path, typer.Option(help="Path to a persona .persona.md or plain prompt text file")],
    team_instructions: Annotated[str | None, typer.Option()] = None,
    model: Annotated[str | None, typer.Option()] = None,
    parallelism: Annotated[int | None, typer.Option()] = None,
    idle_timeout_seconds: Annotated[int | None, typer.Option()] = None,
    max_turn_duration_seconds: Annotated[int | None, typer.Option()] = None,
    respond_to_allowlist: Annotated[
        str | None, typer.Option(help="Comma-separated pubkeys")
    ] = None,
    session_policy: Annotated[
        str | None, typer.Option(help="buzz-acp session scoping: thread (default) or channel")
    ] = None,
    max_turns_per_session: Annotated[
        int | None, typer.Option(help="Rotate an active session after N turns (default 40)")
    ] = None,
    heartbeat_interval_seconds: Annotated[
        int | None, typer.Option(help="Heartbeat prompt interval (default 900, 0 disables)")
    ] = None,
    role: Annotated[str | None, typer.Option(help="This agent's role in the fleet, e.g. 'reviewer'")] = None,
    capability: Annotated[
        list[str] | None, typer.Option("--capability", help="A capability this agent has (repeatable)")
    ] = None,
    description: Annotated[str | None, typer.Option(help="Free-text description of this agent")] = None,
    channel_ids: Annotated[
        str | None, typer.Option(help="Comma-separated NIP-29 channel UUIDs to join")
    ] = None,
    channel_add_policy: Annotated[
        str | None, typer.Option(help="Who may add this agent to a new channel: anyone, owner_only, nobody")
    ] = None,
    env: Annotated[
        list[str] | None, typer.Option("--env", help="KEY=VALUE env var for this agent (repeatable)")
    ] = None,
    env_file: Annotated[
        Path | None, typer.Option(help="Path to a file of KEY=VALUE lines to load as env vars")
    ] = None,
    mcp_name: Annotated[str | None, typer.Option(help="This agent's MCP server's name")] = None,
    mcp_command: Annotated[str | None, typer.Option(help="This agent's MCP server's command")] = None,
    mcp_arg: Annotated[
        list[str] | None, typer.Option("--mcp-arg", help="An argument to the MCP server command (repeatable)")
    ] = None,
    mcp_env: Annotated[
        list[str] | None, typer.Option("--mcp-env", help="KEY=VALUE env var for the MCP server (repeatable)")
    ] = None,
    force: Annotated[
        bool, typer.Option(help="Create even if the display name is already used in the fleet channel")
    ] = False,
) -> None:
    manager = _load_manager(community)
    parsed_channel_ids = _parse_channel_ids(channel_ids)
    if channel_add_policy is not None and channel_add_policy not in ("anyone", "owner_only", "nobody"):
        typer.echo("--channel-add-policy must be one of: anyone, owner_only, nobody", err=True)
        raise typer.Exit(code=1)
    if session_policy is not None and session_policy not in ("thread", "channel"):
        typer.echo("--session-policy must be one of: thread, channel", err=True)
        raise typer.Exit(code=1)
    parsed_env = _resolve_env(env, env_file)
    try:
        # `_resolve_mcp_server` is inside the try, not just `create_agent`:
        # an invalid `--mcp-name '../x'` raises a raw pydantic ValidationError
        # out of McpServer's own constructor, before create_agent is ever
        # called. Outside this try, that printed a traceback instead of the
        # message-plus-exit-1 every other validation on this command produces.
        mcp_server = _resolve_mcp_server(mcp_name, mcp_command, mcp_arg, mcp_env)
        agent = manager.create_agent(
            display_name=display_name,
            harness=harness,
            system_prompt_source=SystemPromptSource(kind="persona_file", path=prompt_file),
            team_instructions=team_instructions,
            model=model,
            parallelism=parallelism,
            idle_timeout_seconds=idle_timeout_seconds,
            max_turn_duration_seconds=max_turn_duration_seconds,
            respond_to_allowlist=(
                [key.strip() for key in respond_to_allowlist.split(",") if key.strip()] or None
                if respond_to_allowlist
                else None
            ),
            session_policy=session_policy,
            max_turns_per_session=max_turns_per_session,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            role=role,
            capabilities=capability or None,
            description=description,
            channel_ids=parsed_channel_ids,
            channel_add_policy=channel_add_policy,
            env=parsed_env,
            mcp_server=mcp_server,
            force=force,
        )
    except ValueError as e:
        # e.g. a blank/punctuation-only --display-name (agent_slug raises)
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"Created agent '{agent.id}' ({agent.public_key}).")


@agent_app.command("list")
def agent_list(community: Annotated[str, typer.Option()]) -> None:
    from buzz_fleet import visibility

    manager = _load_manager(community)
    manager.ensure_runtime_ready()
    for agent in manager.list_agents():
        status = visibility.visibility_status_text(agent)
        typer.echo(f"{agent.id}\t{agent.display_name}\t{agent.harness}\t{status}")


@agent_app.command("delete")
def agent_delete(community: Annotated[str, typer.Option()], agent_id: Annotated[str, typer.Argument()]) -> None:
    manager = _load_manager(community)
    manager.delete_agent(agent_id)
    typer.echo(f"Deleted agent '{agent_id}'.")


@agent_app.command("update")
def agent_update(
    community: Annotated[str, typer.Option()],
    agent_id: Annotated[str, typer.Argument()],
    display_name: Annotated[str | None, typer.Option()] = None,
    prompt_file: Annotated[
        Path | None, typer.Option(help="Replace the system prompt with this persona/prompt file")
    ] = None,
    team_instructions: Annotated[str | None, typer.Option()] = None,
    model: Annotated[str | None, typer.Option()] = None,
    parallelism: Annotated[int | None, typer.Option()] = None,
    idle_timeout_seconds: Annotated[int | None, typer.Option()] = None,
    max_turn_duration_seconds: Annotated[int | None, typer.Option()] = None,
    respond_to_allowlist: Annotated[
        str | None, typer.Option(help="Comma-separated pubkeys")
    ] = None,
    session_policy: Annotated[
        str | None, typer.Option(help="buzz-acp session scoping: thread (default) or channel")
    ] = None,
    max_turns_per_session: Annotated[
        int | None, typer.Option(help="Rotate an active session after N turns (default 40)")
    ] = None,
    heartbeat_interval_seconds: Annotated[
        int | None, typer.Option(help="Heartbeat prompt interval (default 900, 0 disables)")
    ] = None,
    role: Annotated[str | None, typer.Option(help="This agent's role in the fleet, e.g. 'reviewer'")] = None,
    capability: Annotated[
        list[str] | None, typer.Option("--capability", help="A capability this agent has (repeatable)")
    ] = None,
    description: Annotated[str | None, typer.Option(help="Free-text description of this agent")] = None,
    channel_ids: Annotated[
        str | None, typer.Option(help="Comma-separated NIP-29 channel UUIDs to join")
    ] = None,
    channel_add_policy: Annotated[
        str | None, typer.Option(help="Who may add this agent to a new channel: anyone, owner_only, nobody")
    ] = None,
    env: Annotated[
        list[str] | None, typer.Option("--env", help="KEY=VALUE env var for this agent (repeatable)")
    ] = None,
    env_file: Annotated[
        Path | None, typer.Option(help="Path to a file of KEY=VALUE lines to load as env vars")
    ] = None,
    mcp_name: Annotated[str | None, typer.Option(help="This agent's MCP server's name")] = None,
    mcp_command: Annotated[str | None, typer.Option(help="This agent's MCP server's command")] = None,
    mcp_arg: Annotated[
        list[str] | None, typer.Option("--mcp-arg", help="An argument to the MCP server command (repeatable)")
    ] = None,
    mcp_env: Annotated[
        list[str] | None, typer.Option("--mcp-env", help="KEY=VALUE env var for the MCP server (repeatable)")
    ] = None,
) -> None:
    manager = _load_manager(community)
    changes: dict[str, object] = {}
    if display_name is not None:
        changes["display_name"] = display_name
    if prompt_file is not None:
        changes["system_prompt_source"] = SystemPromptSource(kind="persona_file", path=prompt_file)
    if team_instructions is not None:
        changes["team_instructions"] = team_instructions
    if model is not None:
        changes["model"] = model
    if parallelism is not None:
        changes["parallelism"] = parallelism
    if idle_timeout_seconds is not None:
        changes["idle_timeout_seconds"] = idle_timeout_seconds
    if max_turn_duration_seconds is not None:
        changes["max_turn_duration_seconds"] = max_turn_duration_seconds
    if respond_to_allowlist is not None:
        changes["respond_to_allowlist"] = (
            [key.strip() for key in respond_to_allowlist.split(",") if key.strip()] or None
            if respond_to_allowlist
            else None
        )
    if session_policy is not None:
        if session_policy not in ("thread", "channel"):
            typer.echo("--session-policy must be one of: thread, channel", err=True)
            raise typer.Exit(code=1)
        changes["session_policy"] = session_policy
    if max_turns_per_session is not None:
        changes["max_turns_per_session"] = max_turns_per_session
    if heartbeat_interval_seconds is not None:
        changes["heartbeat_interval_seconds"] = heartbeat_interval_seconds
    if role is not None:
        changes["role"] = role
    if capability is not None:
        changes["capabilities"] = capability or None
    if description is not None:
        changes["description"] = description
    if channel_ids is not None:
        changes["channel_ids"] = _parse_channel_ids(channel_ids)
    if channel_add_policy is not None:
        if channel_add_policy not in ("anyone", "owner_only", "nobody"):
            typer.echo("--channel-add-policy must be one of: anyone, owner_only, nobody", err=True)
            raise typer.Exit(code=1)
        changes["channel_add_policy"] = channel_add_policy
    if env is not None or env_file is not None:
        changes["env"] = _resolve_env(env, env_file)
    try:
        # Same reasoning as agent_create: `_resolve_mcp_server` can raise a
        # raw pydantic ValidationError out of McpServer's own constructor
        # (e.g. an unsafe --mcp-name), and update_agent itself can raise
        # ValueError too (e.g. an unsafe --env key — see
        # models.validate_env_key). Both belong inside the same try as every
        # other validation this command produces, not a bare traceback.
        mcp_server = _resolve_mcp_server(mcp_name, mcp_command, mcp_arg, mcp_env)
        if mcp_server is not None:
            changes["mcp_server"] = mcp_server
        if not changes:
            typer.echo("Nothing to update — pass at least one field to change.", err=True)
            raise typer.Exit(code=1)
        updated = manager.update_agent(agent_id, **changes)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"Updated agent '{updated.id}'.")


@harness_app.command("list")
def harness_list() -> None:
    availability = harnesses.detect_harness_availability()
    for harness in harnesses.HARNESSES:
        typer.echo(f"{harness}\t{availability[harness]}")


@harness_app.command("install")
def harness_install(name: Annotated[str, typer.Argument(help="claude, codex, pi, or goose")]) -> None:
    if name not in harnesses.HARNESSES:
        typer.echo(f"Unknown harness '{name}' — one of: {', '.join(harnesses.HARNESSES)}", err=True)
        raise typer.Exit(code=1)
    try:
        harnesses.install_adapter(RealCommandRunner(), name)
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"Installed {name}'s adapter.")


@config_app.command("show")
def config_show() -> None:
    """Print the effective configuration and where it was read from."""
    from dataclasses import asdict

    from buzz_fleet import config as config_module

    path = paths.config_dir() / "config.toml"
    typer.echo(f"# {path}{'' if path.exists() else '  (not present; showing defaults)'}")
    values = asdict(config_module.load())
    values["ntfy_token"] = "<set>" if values["ntfy_token"] else None
    for key, value in values.items():
        typer.echo(f"{key} = {value!r}")


@app.command()
def tui() -> None:
    from buzz_fleet.tui.app import BuzzFleetApp

    BuzzFleetApp().run()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
