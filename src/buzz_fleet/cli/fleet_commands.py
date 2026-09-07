"""Typer groups for orchestration: `buzz-fleet fleet ...`, `buzz-fleet task ...`, `buzz-fleet tasks`."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.console import Console
from rich.table import Table

from buzz_fleet import state
from buzz_fleet.manager import AgentManager
from buzz_fleet.orchestration import git_artifact, ids, protocol, relay
from buzz_fleet.orchestration.durations import parse_duration
from buzz_fleet.orchestration.identity import Identity, resolve_identity
from buzz_fleet.orchestration.protocol import Artifact
from buzz_fleet.orchestration.record import Limits
from buzz_fleet.orchestration.reducer import State, Task
from buzz_fleet.proc import CommandRunner, RealCommandRunner

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


def render_agents(entries: list[relay.DirectoryEntry]) -> Table:
    table = Table(title="Fleet agents")
    for col in ("Name", "Role", "Capabilities", "Harness", "Host", "Online", "Live tasks", "Version"):
        table.add_column(col)
    for e in entries:
        online = "yes" if e.online is True else "no" if e.online is False else "?"
        table.add_row(e.display_name or e.pubkey[:8], e.role or "-", ", ".join(e.capabilities) or "-",
                      e.harness or "-", e.host or "-", online, str(e.live_tasks), e.version or "-")
    return table


@fleet_app.command("agents")
def fleet_agents(
    community: Annotated[str | None, typer.Option()] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    runner = RealCommandRunner()
    try:
        ident = resolve_identity(os.environ, runner, community)
        entries = relay.directory(runner, ident, channel_id=None)
    except _ERRORS as e:
        _fail(e)
        return
    if as_json:
        typer.echo(json.dumps([asdict(e) for e in entries]))
        return
    Console().print(render_agents(entries))


task_app = typer.Typer(help="Delegate work to fleet agents, ack it, report it, cancel it")


def _read_text_arg(value: str) -> str:
    return sys.stdin.read() if value == "-" else value


def _channel(ident: Identity, explicit: str | None) -> str:
    channel = explicit or ident.fleet_channel
    if not channel or not ident.retrieval_key:
        raise RuntimeError("no fleet record known here; run `buzz-fleet fleet init` once, then any buzz-fleet command")
    return channel


def _limits(ident: Identity) -> Limits:
    return ident.record.limits if ident.record else Limits()


def _match_task_id(state: State, task_ref: str) -> str:
    try:
        return ids.match_prefix(task_ref, state.tasks)
    except ValueError as e:
        # ids.match_prefix raises ValueError (Task 8's contract, shared with other
        # callers that still expect ValueError) -- but every other error path in
        # this module raises RuntimeError, and every caller of this helper relies
        # on that uniformly. Re-wrap here rather than changing match_prefix.
        raise RuntimeError(str(e)) from e


def _find_task(state: State, task_ref: str) -> Task:
    return state.tasks[_match_task_id(state, task_ref)]


def _post_idempotent(runner: CommandRunner, ident: Identity, channel: str, msg: protocol.OutgoingMessage,
                     task_id: str, attempt_id: str, root: str | None) -> str:
    """Publish once; on an ambiguous result, look for the same task+attempt before retrying the same message.

    Re-reads by task id: `fetch_thread` on the intended root when one is already known
    (every ack/report/cancel has one -- the task's own root_event_id -- and a delegate
    replying into an existing thread or run does too), or a channel-wide fleet-events
    scan when there is none yet (a brand-new ad-hoc delegate with no thread to fetch).
    """
    try:
        return relay.post(runner, ident, channel, msg)
    except RuntimeError as e:
        if "timeout" not in str(e).lower():
            raise
        events = relay.fetch_thread(runner, ident, channel_id=channel, root=root) if root \
            else relay.fetch_fleet_events(runner, ident, channel_id=channel)
        for ev in events:
            if ev.payload and ev.payload.get("task") == task_id and ev.payload.get("attempt") == attempt_id \
                    and ev.type == msg_type(msg):
                return ev.id
        return relay.post(runner, ident, channel, msg)


def msg_type(msg: protocol.OutgoingMessage) -> str:
    return json.loads(dict(msg.tags)["fleet"])["type"]


def delegate_task(runner: CommandRunner, ident: Identity, *, to: str, brief: str, wait_seconds: int, acceptance: list[str],
                  artifact: Artifact | None, run_id: str | None, thread_root: str | None, parent_task: str | None,
                  required: bool, channel: str | None, cwd: Path | None, git_run, now: int) -> dict:
    channel_id = _channel(ident, channel)
    assert ident.retrieval_key
    state = relay.load_state(runner, ident, channel_id=channel_id)
    limits = _limits(ident)
    if run_id is None and state.open_adhoc_by_requester(ident.pubkey) >= limits.open_adhoc_per_requester:
        raise RuntimeError(f"you already have {limits.open_adhoc_per_requester} open ad-hoc tasks; report or cancel one first")
    if parent_task:
        parent_task = _match_task_id(state, parent_task)
        if state.chain_depth(parent_task) + 1 > limits.chain_depth:
            raise RuntimeError(f"delegation chain would exceed depth {limits.chain_depth}")
    if artifact is None and cwd is not None and git_run is not None:
        artifact = git_artifact.detect(cwd, git_run)
    to_pubkey, to_name = relay.resolve_member(runner, ident, channel_id, to)
    root = thread_root
    if run_id and not root:
        run_events = [e for e in relay.fetch_fleet_events(runner, ident, channel_id=channel_id) if e.run_id == run_id]
        if run_events:
            first = min(run_events, key=lambda e: (e.created_at, e.id))
            root = first.root or first.id
    task_id, attempt_id = ids.new_id(), ids.new_id()
    deadline = now + wait_seconds
    msg = protocol.build_delegate(task_id=task_id, attempt_id=attempt_id, from_pubkey=ident.pubkey, to_pubkey=to_pubkey,
                                  to_name=to_name or to, retrieval_key=ident.retrieval_key, brief=brief, deadline=deadline,
                                  acceptance=acceptance, artifact=artifact, run_id=run_id, step=None, parent_task=parent_task,
                                  required=required, rework_target=None, default_next=None, thread_root=root, thread_parent=root)
    event_id = _post_idempotent(runner, ident, channel_id, msg, task_id, attempt_id, root)
    return {"task": task_id, "attempt": attempt_id, "event_id": event_id, "deadline": deadline, "channel": channel_id}


def _load_task(runner: CommandRunner, ident: Identity, task_ref: str, channel: str | None) -> tuple[Task, str]:
    channel_id = _channel(ident, channel)
    task = _find_task(relay.load_state(runner, ident, channel_id=channel_id), task_ref)
    return task, task.channel_id or channel_id


def ack_task(runner: CommandRunner, ident: Identity, *, task_ref: str, channel: str | None) -> dict:
    task, channel_id = _load_task(runner, ident, task_ref, channel)
    if ident.pubkey != task.assignee:
        raise RuntimeError(f"task {ids.short(task.task_id)} is assigned to {task.assignee[:12]}…, not to you")
    assert ident.retrieval_key
    msg = protocol.build_ack(task_id=task.task_id, attempt_id=task.current.attempt_id, from_pubkey=ident.pubkey,
                             retrieval_key=ident.retrieval_key, root=task.root_event_id, parent=task.delegate_event_id)
    return {"task": task.task_id, "attempt": task.current.attempt_id,
            "event_id": _post_idempotent(runner, ident, channel_id, msg, task.task_id, task.current.attempt_id,
                                         task.root_event_id)}


def report_task(runner: CommandRunner, ident: Identity, *, task_ref: str, status: Literal["done", "blocked", "failed"],
                summary: str, next_task: str, input_commit: str | None, output_commit: str | None, evidence: list[str],
                channel: str | None) -> dict:
    task, channel_id = _load_task(runner, ident, task_ref, channel)
    if ident.pubkey != task.assignee:
        raise RuntimeError(f"task {ids.short(task.task_id)} is assigned to {task.assignee[:12]}…, not to you ({ident.pubkey[:12]}…)")
    if not task.is_live:
        raise RuntimeError(f"task {ids.short(task.task_id)} is {task.status}; nothing to report")
    expected = (task.artifact or {}).get("commit")
    if expected and input_commit != expected:
        raise RuntimeError(f"--input-commit must be {expected} (the commit you were given); got {input_commit!r}")
    assert ident.retrieval_key
    recipient = task.rework_target if status == "failed" and task.rework_target else task.requester
    _, recipient_name = relay.resolve_member(runner, ident, channel_id, recipient)
    msg = protocol.build_report(task_id=task.task_id, attempt_id=task.current.attempt_id, status=status, summary=summary,
                                from_pubkey=ident.pubkey, recipient_pubkey=recipient, recipient_name=recipient_name,
                                retrieval_key=ident.retrieval_key, next_task=next_task, input_commit=input_commit,
                                output_commit=output_commit, evidence=evidence, run_id=task.run_id,
                                root=task.root_event_id, parent=task.delegate_event_id)
    return {"task": task.task_id, "attempt": task.current.attempt_id,
            "event_id": _post_idempotent(runner, ident, channel_id, msg, task.task_id, task.current.attempt_id,
                                         task.root_event_id)}


def cancel_task(runner: CommandRunner, ident: Identity, *, task_ref: str, reason: str, channel: str | None) -> dict:
    task, channel_id = _load_task(runner, ident, task_ref, channel)
    if ident.pubkey not in (task.requester, ident.owner_pubkey if ident.is_owner else None):
        raise RuntimeError("only the requester or the owner may cancel a task")
    assert ident.retrieval_key
    msg = protocol.build_cancel_task(task_id=task.task_id, reason=reason, from_pubkey=ident.pubkey,
                                     assignee_pubkey=task.assignee, retrieval_key=ident.retrieval_key,
                                     root=task.root_event_id, parent=task.delegate_event_id)
    return {"task": task.task_id, "attempt": task.current.attempt_id,
            "event_id": _post_idempotent(runner, ident, channel_id, msg, task.task_id, task.current.attempt_id,
                                         task.root_event_id)}


def _fail(e: Exception) -> None:
    typer.echo(json.dumps({"error": str(e)}), err=True)
    raise typer.Exit(code=1)


_ERRORS = (RuntimeError, ValueError, json.JSONDecodeError, KeyError)


@task_app.command("delegate")
def task_delegate(
    to: Annotated[str, typer.Option(help="Agent display name or hex pubkey")],
    brief: Annotated[str, typer.Option(help="Task text; '-' reads stdin")],
    wait: Annotated[str, typer.Option(help="Deadline from now, e.g. 45m, 2h")] = "60m",
    repo: Annotated[str | None, typer.Option()] = None,
    commit: Annotated[str | None, typer.Option()] = None,
    branch: Annotated[str | None, typer.Option()] = None,
    base: Annotated[str | None, typer.Option()] = None,
    accept: Annotated[list[str] | None, typer.Option(help="Acceptance criterion (repeatable)")] = None,
    run: Annotated[str | None, typer.Option()] = None,
    thread: Annotated[str | None, typer.Option(help="Root event id of the thread to reply in")] = None,
    parent: Annotated[str | None, typer.Option(help="Parent task id or prefix")] = None,
    optional: Annotated[bool, typer.Option("--optional")] = False,
    channel: Annotated[str | None, typer.Option()] = None,
    community: Annotated[str | None, typer.Option()] = None,
) -> None:
    if (repo is None) != (commit is None):
        _fail(RuntimeError("--repo and --commit go together"))
    runner = RealCommandRunner()
    try:
        ident = resolve_identity(os.environ, runner, community)
        artifact = Artifact(repo=repo, commit=commit, branch=branch, base=base) if repo and commit else None
        out = delegate_task(runner, ident, to=to, brief=_read_text_arg(brief), wait_seconds=parse_duration(wait),
                            acceptance=accept or [], artifact=artifact, run_id=run, thread_root=thread, parent_task=parent,
                            required=not optional, channel=channel,
                            cwd=None if artifact else Path.cwd(), git_run=None if artifact else runner.run, now=int(time.time()))
    except _ERRORS as e:
        _fail(e)
        return
    typer.echo(json.dumps(out))


@task_app.command("ack")
def task_ack(task: Annotated[str, typer.Option()], channel: Annotated[str | None, typer.Option()] = None,
             community: Annotated[str | None, typer.Option()] = None) -> None:
    runner = RealCommandRunner()
    try:
        out = ack_task(runner, resolve_identity(os.environ, runner, community), task_ref=task, channel=channel)
    except _ERRORS as e:
        _fail(e)
        return
    typer.echo(json.dumps(out))


@task_app.command("report")
def task_report(
    task: Annotated[str, typer.Option()],
    status: Annotated[str, typer.Option(help="done, blocked, or failed")],
    summary: Annotated[str, typer.Option(help="Outcome text; '-' reads stdin")],
    next: Annotated[str, typer.Option(help="default, none, or a task id you delegated onward")] = "default",
    input_commit: Annotated[str | None, typer.Option()] = None,
    output_commit: Annotated[str | None, typer.Option()] = None,
    evidence: Annotated[list[str] | None, typer.Option(help="Evidence line (repeatable)")] = None,
    channel: Annotated[str | None, typer.Option()] = None,
    community: Annotated[str | None, typer.Option()] = None,
) -> None:
    if status not in ("done", "blocked", "failed"):
        _fail(RuntimeError("--status must be done, blocked, or failed"))
    if next not in ("default", "none") and len(next) < 8:
        _fail(RuntimeError("--next must be default, none, or a task id"))
    runner = RealCommandRunner()
    try:
        out = report_task(runner, resolve_identity(os.environ, runner, community), task_ref=task, status=status,  # type: ignore[arg-type]
                          summary=_read_text_arg(summary), next_task=next, input_commit=input_commit,
                          output_commit=output_commit, evidence=evidence or [], channel=channel)
    except _ERRORS as e:
        _fail(e)
        return
    typer.echo(json.dumps(out))


@task_app.command("cancel")
def task_cancel(task_id: Annotated[str, typer.Argument()], reason: Annotated[str, typer.Option()],
                channel: Annotated[str | None, typer.Option()] = None,
                community: Annotated[str | None, typer.Option()] = None) -> None:
    runner = RealCommandRunner()
    try:
        out = cancel_task(runner, resolve_identity(os.environ, runner, community), task_ref=task_id, reason=reason, channel=channel)
    except _ERRORS as e:
        _fail(e)
        return
    typer.echo(json.dumps(out))


def task_rows(state: State, *, only_open: bool, only_stuck: bool, only_unacked: bool, mine: str | None, now: int) -> list[Task]:
    rows = list(state.tasks.values())
    if only_open:
        rows = [t for t in rows if t.is_live]
    if only_stuck:
        rows = [t for t in rows if t.late(now)]
    if only_unacked:
        rows = [t for t in rows if t.unacked]
    if mine:
        rows = [t for t in rows if t.assignee == mine]
    return sorted(rows, key=lambda t: t.created_at, reverse=True)


def _age(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d"


def render_tasks(tasks: list[Task], now: int) -> Table:
    table = Table(title="Fleet tasks")
    # Task/Run hold `ids.short()` 8-char ids -- these must never be squeezed below
    # that by the table's column-width negotiation (which otherwise ellipsis-
    # truncates them under a narrow console, e.g. the 80-column fallback Rich uses
    # when stdout isn't a real terminal), or the "views show the first 8
    # characters" contract silently breaks. min_width pins the floor; the other
    # columns still shrink to fit a narrow terminal exactly as before.
    table.add_column("Task", min_width=8, no_wrap=True)
    table.add_column("Run", min_width=8, no_wrap=True)
    for col in ("Status", "Assignee", "Requester", "Age", "Deadline", "Attempt", "Summary"):
        table.add_column(col)
    for t in tasks:
        remaining = t.deadline - now
        deadline = (f"in {_age(remaining)}" if remaining > 0 else f"{_age(-remaining)} overdue") if t.is_live else "-"
        summary = (t.current.report or {}).get("content") or t.brief
        table.add_row(ids.short(t.task_id), ids.short(t.run_id) if t.run_id else "-", t.status, t.assignee[:8],
                      t.requester[:8], _age(now - t.created_at), deadline, str(len(t.attempts)), summary.splitlines()[0][:60])
    return table


def task_to_json(task: Task) -> dict:
    # `status`/`assignee`/`is_live`/`unacked` are @property on Task (derived from
    # `attempts[-1]`), not dataclass fields -- `asdict()` walks fields only, so on
    # its own it would silently drop exactly the columns a fleet-status consumer
    # needs most, forcing every agent parsing this JSON to know Attempt's shape and
    # assume `attempts` is never empty. `is_live`/`unacked` are included alongside
    # `status`/`assignee` for the same reason: they're precisely what `--open` and
    # `--unacked` already select on, so a JSON consumer gets the same status-board
    # semantics without re-deriving the open/acked/terminal grouping from `status`
    # itself (a literal it would otherwise have to hardcode and keep in sync with
    # `reducer.TaskStatus`).
    return {**asdict(task), "status": task.status, "assignee": task.assignee, "is_live": task.is_live, "unacked": task.unacked}


def tasks_command(
    open_only: Annotated[bool, typer.Option("--open")] = False,
    stuck: Annotated[bool, typer.Option("--stuck")] = False,
    unacked: Annotated[bool, typer.Option("--unacked")] = False,
    mine: Annotated[bool, typer.Option("--mine")] = False,
    channel: Annotated[str | None, typer.Option()] = None,
    community: Annotated[str | None, typer.Option()] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    runner, now = RealCommandRunner(), int(time.time())
    try:
        ident = resolve_identity(os.environ, runner, community)
        state = relay.load_state(runner, ident, channel_id=channel)
    except _ERRORS as e:
        _fail(e)
        return
    rows = task_rows(state, only_open=open_only, only_stuck=stuck, only_unacked=unacked, mine=ident.pubkey if mine else None, now=now)
    if as_json:
        typer.echo(json.dumps([task_to_json(t) for t in rows]))
        return
    Console().print(render_tasks(rows, now))


@task_app.command("show")
def task_show(task_ref: Annotated[str, typer.Argument()], channel: Annotated[str | None, typer.Option()] = None,
              community: Annotated[str | None, typer.Option()] = None,
              as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    runner, now = RealCommandRunner(), int(time.time())
    try:
        ident = resolve_identity(os.environ, runner, community)
        task, _ = _load_task(runner, ident, task_ref, channel)
    except _ERRORS as e:
        _fail(e)
        return
    if as_json:
        typer.echo(json.dumps(task_to_json(task)))
        return
    console = Console()
    console.print(render_tasks([task], now))
    console.print(f"[bold]Brief[/bold]\n{task.brief}")
    for i, a in enumerate(task.attempts, 1):
        console.print(f"[bold]Attempt {i}[/bold] {a.assignee[:8]} {a.status}"
                      + (f" acked {_age(now - a.acked_at)} ago" if a.acked_at else " (not acked)"))
        if a.report:
            console.print(a.report.get("content", ""))
    for note in task.notes:
        console.print(f"[dim]note: {note}[/dim]")
