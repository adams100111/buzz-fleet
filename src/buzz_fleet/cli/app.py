"""The buzz-fleet Typer CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from buzz_fleet import __version__, harnesses, state
from buzz_fleet.connect import connect_and_save
from buzz_fleet.manager import AgentManager
from buzz_fleet.models import SystemPromptSource
from buzz_fleet.proc import RealCommandRunner

app = typer.Typer(help="buzz-fleet — manage headless Buzz agents", no_args_is_help=True)
agent_app = typer.Typer(help="Manage agent identities")
app.add_typer(agent_app, name="agent")
harness_app = typer.Typer(help="Detect and install harness adapters")
app.add_typer(harness_app, name="harness")


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
    model: Annotated[str | None, typer.Option()] = None,
    parallelism: Annotated[int | None, typer.Option()] = None,
    idle_timeout_seconds: Annotated[int | None, typer.Option()] = None,
    max_turn_duration_seconds: Annotated[int | None, typer.Option()] = None,
    respond_to_allowlist: Annotated[
        str | None, typer.Option(help="Comma-separated pubkeys")
    ] = None,
) -> None:
    manager = _load_manager(community)
    try:
        agent = manager.create_agent(
            display_name=display_name,
            harness=harness,
            system_prompt_source=SystemPromptSource(kind="persona_file", path=prompt_file),
            model=model,
            parallelism=parallelism,
            idle_timeout_seconds=idle_timeout_seconds,
            max_turn_duration_seconds=max_turn_duration_seconds,
            respond_to_allowlist=(
                [key.strip() for key in respond_to_allowlist.split(",") if key.strip()] or None
                if respond_to_allowlist
                else None
            ),
        )
    except ValueError as e:
        # e.g. a blank/punctuation-only --display-name (agent_slug raises)
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"Created agent '{agent.id}' ({agent.public_key}).")


@agent_app.command("list")
def agent_list(community: Annotated[str, typer.Option()]) -> None:
    manager = _load_manager(community)
    for agent in manager.list_agents():
        typer.echo(f"{agent.id}\t{agent.display_name}\t{agent.harness}")


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
    model: Annotated[str | None, typer.Option()] = None,
    parallelism: Annotated[int | None, typer.Option()] = None,
    idle_timeout_seconds: Annotated[int | None, typer.Option()] = None,
    max_turn_duration_seconds: Annotated[int | None, typer.Option()] = None,
    respond_to_allowlist: Annotated[
        str | None, typer.Option(help="Comma-separated pubkeys")
    ] = None,
) -> None:
    manager = _load_manager(community)
    changes: dict[str, object] = {}
    if display_name is not None:
        changes["display_name"] = display_name
    if prompt_file is not None:
        changes["system_prompt_source"] = SystemPromptSource(kind="persona_file", path=prompt_file)
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
    if not changes:
        typer.echo("Nothing to update — pass at least one field to change.", err=True)
        raise typer.Exit(code=1)
    updated = manager.update_agent(agent_id, **changes)
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


@app.command()
def tui() -> None:
    from buzz_fleet.tui.app import BuzzFleetApp

    BuzzFleetApp().run()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
