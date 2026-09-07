"""Pure reducer: fleet events -> task state (spec 5.3). Shared by CLI views, TUI, and the conductor."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from buzz_fleet.orchestration.protocol import FleetEvent
from buzz_fleet.orchestration.record import FleetRecord

TaskStatus = Literal["open", "acked", "done", "blocked", "failed", "cancelled", "superseded"]
_TERMINAL = frozenset({"done", "blocked", "failed", "cancelled", "superseded"})
_CONDUCTOR_TYPES = frozenset({"redeliver", "nudge", "escalate", "cancel-notice", "advance", "fallback",
                              "run-paused", "run-done", "run-failed", "budget-paused", "heartbeat", "takeover", "yield"})


@dataclass
class Attempt:
    attempt_id: str
    assignee: str
    created_at: int
    acked_at: int | None = None
    status: TaskStatus = "open"
    report: dict | None = None
    reported_at: int | None = None


@dataclass
class Task:
    task_id: str
    requester: str
    run_id: str | None
    step: int | None
    parent_task: str | None
    required: bool
    rework_target: str | None
    artifact: dict | None
    acceptance: list[str]
    deadline: int
    created_at: int
    channel_id: str | None
    root_event_id: str
    delegate_event_id: str
    brief: str
    attempts: list[Attempt] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    nudged_at: int | None = None
    escalated_at: int | None = None
    redelivered: int = 0

    @property
    def current(self) -> Attempt:
        return self.attempts[-1]

    @property
    def assignee(self) -> str:
        return self.current.assignee

    @property
    def status(self) -> TaskStatus:
        return self.current.status

    @property
    def is_live(self) -> bool:
        return self.status in ("open", "acked")

    @property
    def unacked(self) -> bool:
        return self.status == "open"

    def late(self, now: int) -> bool:
        return self.is_live and self.deadline < now


@dataclass
class State:
    tasks: dict[str, Task] = field(default_factory=dict)
    seen_cmds: set[str] = field(default_factory=set)

    def open_tasks(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.is_live]

    def stuck_tasks(self, now: int) -> list[Task]:
        return [t for t in self.tasks.values() if t.late(now)]

    def unacked_tasks(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.unacked]

    def open_adhoc_by_requester(self, pubkey: str) -> int:
        return sum(1 for t in self.open_tasks() if t.run_id is None and t.requester == pubkey)

    def chain_depth(self, task_id: str) -> int:
        depth, cur, seen = 0, self.tasks.get(task_id), set()
        while cur is not None and cur.task_id not in seen:
            seen.add(cur.task_id)
            depth += 1
            parent = cur.parent_task
            # `parent_task` is validated to `str | None` at task-creation time (see
            # `_safe_task_id` in `_apply_delegate`), so this dict.get is always given a
            # hashable key -- an unvalidated `dict.get(<list>)` here would be the same
            # "unhashable key" TypeError as the top-level `task` field in `reduce`.
            cur = self.tasks.get(parent) if isinstance(parent, str) and parent else None
        return depth


def _record_cmd(state: State, p: dict) -> None:
    """Record an authorized event's `cmd` for the conductor's own dedup (spec 5.4).

    Called only from branches that actually *apply* an event. An event rejected on
    authorization grounds must not be able to plant a `cmd` in `seen_cmds` anyway --
    otherwise any fleet member could forge a conductor `cmd` id (e.g. from a fake
    `nudge`) that the real conductor would then treat as already-done and skip forever.
    This deviates from the brief's literal "every payload's cmd, when present, goes
    into seen_cmds" -- see the task-12 fix report for why.

    Also guards against a malformed `cmd` (e.g. a list, where the brief's original
    unconditional `state.seen_cmds.add(cmd)` would raise TypeError on the unhashable
    value): a non-empty string is required, anything else is silently dropped rather
    than recorded or raised.
    """
    cmd = p.get("cmd")
    if isinstance(cmd, str) and cmd:
        state.seen_cmds.add(cmd)


def _safe_int(value: object, default: int) -> int | None:
    """Coerce a payload field to int, defensively.

    Returns `default` when the field is absent, the coerced int when the value is a
    genuine number (or numeric string), or None when it is present but not usable
    (e.g. "soon", a list, a bool) -- callers must treat None as "refuse this event",
    never silently fall back to a default, so a malformed field can't be quietly
    smuggled through with fabricated data.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, str, float)):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _safe_acceptance(value: object) -> list[str] | None:
    """Validate the `acceptance` field. None (absent) becomes []; anything that isn't
    a list of strings is refused (e.g. `list(5)` in the original code raised TypeError)."""
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _safe_task_id(value: object) -> str | None:
    """Validate a payload's `task` (or `parent_task`) field.

    Returns the id when it is a non-empty string, else None -- callers must treat
    None as "malformed, refuse" rather than falling back to a default. Unlike
    `_safe_int`, there is no sensible default for a task id: `p.get("task")` being a
    list or dict (e.g. `{"task": []}`) previously reached `state.tasks.get(task_id)`
    unvalidated, raising "unhashable type" and killing `reduce` for every reader on a
    single hostile event -- exactly the bug `_safe_int`/`_safe_acceptance` closed for
    `deadline`/`acceptance`, left open here because `task` is the first field the
    reducer touches, before those checks ever run.
    """
    if isinstance(value, str) and value:
        return value
    return None


def _safe_run_id(value: object) -> str | None:
    """Validate a payload's `run` field.

    Returns the id when it is a non-empty string, else None -- same shape as
    `_safe_task_id`, since a stored `run_id` reaches the same kind of unvalidated
    downstream use: `fleet_commands.py`'s `ids.short(t.run_id)` does `full[:8]`, so a
    dict/list/int `run` (e.g. `{"run": {}}`) stored as-is previously raised
    KeyError/TypeError there, and a list additionally raised `rich.errors.
    NotRenderableError` when handed to a Rich table cell -- permanently breaking
    `buzz-fleet tasks`/`task show` for every reader from one hostile event. Unlike
    `task`/`parent_task`, a malformed `run` must not refuse the whole delegate: `run`
    is cosmetic (grouping only), so the caller falls back to `ev.run_id` instead.
    """
    if isinstance(value, str) and value:
        return value
    return None


def reduce(events: Iterable[FleetEvent], record: FleetRecord | None, *, owner_pubkey: str | None = None,
           deleted_ids: set[str] | frozenset[str] = frozenset()) -> State:
    state = State()
    conductors = {c.pubkey for c in record.conductors.values()} if record else set()
    seen: set[str] = set()
    for ev in sorted(events, key=lambda e: (e.created_at, e.id)):
        if ev.id in seen or ev.id in deleted_ids or ev.payload is None:
            continue
        seen.add(ev.id)
        p = ev.payload
        kind = p.get("type")
        raw_task = p.get("task")
        # A malformed `task` (e.g. `[]` or `{}` -- present but not a string) must be
        # refused outright, not fall back to `ev.task_id`: falling back would mask the
        # hostile payload rather than reject it, and using it unvalidated as a dict key
        # below is what broke every reader on one bad event (see `_safe_task_id`).
        if raw_task is not None and _safe_task_id(raw_task) is None:
            continue
        task_id = raw_task or ev.task_id
        if kind == "delegate" and task_id:
            _apply_delegate(state, ev, task_id, conductors)
            continue
        task = state.tasks.get(task_id or "")
        if task is None:
            continue
        if kind in _CONDUCTOR_TYPES:
            if ev.pubkey not in conductors:
                task.notes.append(f"ignored {kind} {ev.id[:8]} from non-conductor {ev.pubkey[:8]}")
                continue
            if kind == "redeliver":
                task.redelivered += 1
            elif kind == "nudge":
                task.nudged_at = ev.created_at
            elif kind == "escalate":
                task.escalated_at = ev.created_at
            _record_cmd(state, p)
            continue
        if kind in ("ack", "report"):
            _apply_assignee_event(state, task, ev, kind)
        elif kind == "cancel-task":
            if ev.pubkey in {task.requester, owner_pubkey} and task.is_live:
                task.current.status = "cancelled"
                _record_cmd(state, p)
            else:
                task.notes.append(f"ignored cancel {ev.id[:8]} from {ev.pubkey[:8]}")
    return state


def _apply_delegate(state: State, ev: FleetEvent, task_id: str, conductors: set[str]) -> None:
    p = ev.payload or {}
    existing = state.tasks.get(task_id)
    attempt_id = p.get("attempt") or ev.id

    if existing is None:
        # A missing/invalid `to` must never fall back to a mention tag -- in the wire
        # layout the first `p` tag is the retrieval key, a keypair nobody holds, which
        # would silently assign the task to nobody and leave it un-ackable forever.
        to = p.get("to")
        if not isinstance(to, str) or not to:
            return  # nothing to note against yet -- no task exists to record it on
        deadline = _safe_int(p.get("deadline"), default=0)
        if deadline is None:
            return
        acceptance = _safe_acceptance(p.get("acceptance"))
        if acceptance is None:
            return
        raw_parent = p.get("parent_task")
        # A malformed `parent_task` (present but not a string, e.g. a list/dict) must
        # be refused rather than stored: `State.chain_depth` does an unvalidated
        # `dict.get(cur.parent_task)` on every task in the chain, so a bad value stored
        # here would raise "unhashable type" for every future chain-depth check, not
        # just this delegate.
        if raw_parent is not None and _safe_task_id(raw_parent) is None:
            return
        raw_run = p.get("run")
        # A malformed `run` (present but not a string, e.g. a dict/list/int) must be
        # refused rather than stored: `fleet_commands.py`'s `ids.short(t.run_id)` does
        # an unvalidated `full[:8]` on it, so a bad value stored here would raise
        # KeyError/TypeError (or NotRenderableError from Rich) for every future
        # `tasks`/`task show` render, not just this delegate. Unlike `task`/
        # `parent_task`, `run` is cosmetic (grouping only) so the delegate itself is
        # still applied -- just falling back to `ev.run_id` -- with a note recording
        # the refusal instead of silently dropping it.
        safe_run = _safe_run_id(raw_run) if raw_run is not None else None
        notes = [f"ignored malformed 'run' on delegate {ev.id[:8]}"] if raw_run is not None and safe_run is None else []
        state.tasks[task_id] = Task(
            task_id=task_id, requester=ev.pubkey, run_id=safe_run or ev.run_id, step=p.get("step"),
            parent_task=raw_parent, required=bool(p.get("required", True)), rework_target=p.get("rework_target"),
            artifact=p.get("artifact"), acceptance=acceptance, deadline=deadline,
            created_at=ev.created_at, channel_id=ev.channel_id, root_event_id=ev.root or ev.id, delegate_event_id=ev.id,
            brief=ev.content, attempts=[Attempt(attempt_id, to, ev.created_at)], notes=notes,
        )
        _record_cmd(state, p)
        return

    if any(a.attempt_id == attempt_id for a in existing.attempts):
        existing.notes.append(f"duplicate delegate {ev.id[:8]} ignored")
        return
    if ev.pubkey not in conductors and ev.pubkey != existing.requester:
        existing.notes.append(f"ignored new attempt {ev.id[:8]} from {ev.pubkey[:8]}")
        return
    to = p.get("to")
    if not isinstance(to, str) or not to:
        existing.notes.append(f"ignored delegate {ev.id[:8]}: missing or invalid 'to'")
        return
    deadline = _safe_int(p.get("deadline"), default=existing.deadline)
    if deadline is None:
        existing.notes.append(f"ignored delegate {ev.id[:8]}: malformed deadline")
        return
    if existing.is_live:
        existing.current.status = "superseded"
    existing.attempts.append(Attempt(attempt_id, to, ev.created_at))
    existing.deadline = deadline
    existing.nudged_at = existing.escalated_at = None
    existing.redelivered = 0
    _record_cmd(state, p)


def _apply_assignee_event(state: State, task: Task, ev: FleetEvent, kind: str) -> None:
    p = ev.payload or {}
    attempt = next((a for a in task.attempts if a.attempt_id == p.get("attempt")), None)
    if attempt is None or ev.pubkey != attempt.assignee:
        task.notes.append(f"ignored {kind} {ev.id[:8]} from {ev.pubkey[:8]} (not the assignee of that attempt)")
        return
    if attempt.status in _TERMINAL:
        task.notes.append(f"{kind} {ev.id[:8]} on {attempt.status} attempt ignored")
        return
    if kind == "ack":
        attempt.acked_at = attempt.acked_at or ev.created_at
        attempt.status = "acked"
        _record_cmd(state, p)
        return
    status = p.get("status")
    if status not in ("done", "blocked", "failed"):
        task.notes.append(f"report {ev.id[:8]} with unknown status {status!r} ignored")
        return
    attempt.status = status
    attempt.report = {**p, "content": ev.content}
    attempt.reported_at = ev.created_at
    _record_cmd(state, p)
