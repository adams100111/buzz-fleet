"""Typer groups for orchestration: `buzz-fleet fleet ...`, `buzz-fleet task ...`, `buzz-fleet tasks`."""

from __future__ import annotations

import socket
from typing import Annotated

import typer

from buzz_fleet import state
from buzz_fleet.manager import AgentManager
from buzz_fleet.proc import RealCommandRunner

fleet_app = typer.Typer(help="Fleet channel, fleet record, and status")


def _load_manager(community_id: str) -> AgentManager:
    community = state.load_community(community_id)
    if community is None:
        typer.echo(f"No community '{community_id}'. Run `buzz-fleet connect` first.", err=True)
        raise typer.Exit(code=1)
    return AgentManager(RealCommandRunner(), community)


@fleet_app.command("init")
def fleet_init(
    community: Annotated[str, typer.Option()],
    channel: Annotated[str | None, typer.Option(help="Adopt an existing channel UUID instead of creating one")] = None,
) -> None:
    manager = _load_manager(community)
    try:
        channel_id, rec = manager.init_fleet_channel(existing=channel, host=socket.gethostname())
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"Fleet channel: {channel_id}")
    typer.echo(f"Retrieval key: {rec.retrieval_key}")
    typer.echo("Retrieval secret (archive it; nothing signs with it and buzz-fleet does not store it):")
    typer.echo(manager._last_retrieval_secret)
    typer.echo("Agents on every machine join the channel on their next buzz-fleet command.")
    typer.echo("Prerequisite per machine: SSH access to every repository your pipelines name.")


@fleet_app.command("status")
def fleet_status(community: Annotated[str, typer.Option()]) -> None:
    manager = _load_manager(community)
    rec = manager.ensure_fleet_record()
    if rec is None:
        if manager._last_fleet_error:
            # e.g. more than one channel carries a fleet record (two racing
            # `fleet init` runs) — telling the operator to run `fleet init`
            # again here would create a THIRD one. Show the real reason.
            typer.echo(f"Could not determine the fleet record: {manager._last_fleet_error}", err=True)
        else:
            typer.echo("No fleet record found. Run `buzz-fleet fleet init` once on the conductor host.")
        raise typer.Exit(code=1)
    typer.echo(f"Channel: {manager._community.fleet_channel_id}")
    typer.echo(f"Retrieval key: {rec.retrieval_key}")
    typer.echo(f"Conductors: {', '.join(f'{k}={v.host} ({v.pubkey[:8]})' for k, v in rec.conductors.items()) or 'none yet'}")
    typer.echo(f"Versions recorded: {rec.versions}")
