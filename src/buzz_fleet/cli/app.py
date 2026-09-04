"""The buzz-fleet Typer CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from buzz_fleet import state
from buzz_fleet.connect import connect_and_save
from buzz_fleet.manager import AgentManager
from buzz_fleet.models import SystemPromptSource
from buzz_fleet.proc import RealCommandRunner

app = typer.Typer(help="buzz-fleet — manage headless Buzz agents", no_args_is_help=True)
agent_app = typer.Typer(help="Manage agent identities")
app.add_typer(agent_app, name="agent")


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
) -> None:
    manager = _load_manager(community)
    agent = manager.create_agent(
        display_name=display_name,
        harness=harness,
        system_prompt_source=SystemPromptSource(kind="persona_file", path=prompt_file),
    )
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
) -> None:
    manager = _load_manager(community)
    changes: dict[str, object] = {}
    if display_name is not None:
        changes["display_name"] = display_name
    if prompt_file is not None:
        changes["system_prompt_source"] = SystemPromptSource(kind="persona_file", path=prompt_file)
    if not changes:
        typer.echo("Nothing to update — pass --display-name and/or --prompt-file.", err=True)
        raise typer.Exit(code=1)
    updated = manager.update_agent(agent_id, **changes)
    typer.echo(f"Updated agent '{updated.id}'.")


@app.command()
def tui() -> None:
    from buzz_fleet.tui.app import BuzzFleetApp

    BuzzFleetApp().run()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
