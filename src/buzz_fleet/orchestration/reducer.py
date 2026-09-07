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
            cur = self.tasks.get(cur.parent_task) if cur.parent_task else None
        return depth


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
        if cmd := p.get("cmd"):
            state.seen_cmds.add(cmd)
        kind = p.get("type")
        task_id = p.get("task") or ev.task_id
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
            continue
        if kind in ("ack", "report"):
            _apply_assignee_event(task, ev, kind)
        elif kind == "cancel-task":
            if ev.pubkey in {task.requester, owner_pubkey} and task.is_live:
                task.current.status = "cancelled"
            else:
                task.notes.append(f"ignored cancel {ev.id[:8]} from {ev.pubkey[:8]}")
    return state


def _apply_delegate(state: State, ev: FleetEvent, task_id: str, conductors: set[str]) -> None:
    p = ev.payload or {}
    attempt_id = p.get("attempt") or ev.id
    to = p.get("to") or (ev.mentions[0] if ev.mentions else "")
    existing = state.tasks.get(task_id)
    if existing is None:
        state.tasks[task_id] = Task(
            task_id=task_id, requester=ev.pubkey, run_id=p.get("run") or ev.run_id, step=p.get("step"),
            parent_task=p.get("parent_task"), required=bool(p.get("required", True)), rework_target=p.get("rework_target"),
            artifact=p.get("artifact"), acceptance=list(p.get("acceptance") or []), deadline=int(p.get("deadline") or 0),
            created_at=ev.created_at, channel_id=ev.channel_id, root_event_id=ev.root or ev.id, delegate_event_id=ev.id,
            brief=ev.content, attempts=[Attempt(attempt_id, to, ev.created_at)],
        )
        return
    if any(a.attempt_id == attempt_id for a in existing.attempts):
        existing.notes.append(f"duplicate delegate {ev.id[:8]} ignored")
        return
    if ev.pubkey not in conductors and ev.pubkey != existing.requester:
        existing.notes.append(f"ignored new attempt {ev.id[:8]} from {ev.pubkey[:8]}")
        return
    if existing.is_live:
        existing.current.status = "superseded"
    existing.attempts.append(Attempt(attempt_id, to, ev.created_at))
    existing.deadline = int(p.get("deadline") or existing.deadline)
    existing.nudged_at = existing.escalated_at = None
    existing.redelivered = 0


def _apply_assignee_event(task: Task, ev: FleetEvent, kind: str) -> None:
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
        return
    status = p.get("status")
    if status not in ("done", "blocked", "failed"):
        task.notes.append(f"report {ev.id[:8]} with unknown status {status!r} ignored")
        return
    attempt.status = status
    attempt.report = {**p, "content": ev.content}
    attempt.reported_at = ev.created_at
