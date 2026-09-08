"""Fleet wire protocol: kind 9 channel messages carrying fleet tags (spec 5.1)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from buzz_fleet.orchestration.ids import short

TAG_FLEET = "fleet"
PAYLOAD_VERSION = 1
MAX_PAYLOAD_BYTES = 8192
ReportStatus = Literal["done", "blocked", "failed"]
_ICON = {"done": "✅", "blocked": "⛔", "failed": "❌"}


def task_tag(task_id: str) -> str:
    return f"fleet:task:{task_id}"


def run_tag(run_id: str) -> str:
    return f"fleet:run:{run_id}"


class Artifact(BaseModel):
    repo: str
    commit: str
    branch: str | None = None
    base: str | None = None


@dataclass(frozen=True)
class OutgoingMessage:
    content: str
    mentions: list[str]
    tags: list[tuple[str, str]]
    root: str | None
    parent: str | None


def _fmt_deadline(deadline: int) -> str:
    return datetime.fromtimestamp(deadline, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def _tags(task_id: str, run_id: str | None, payload: dict) -> list[tuple[str, str]]:
    payload = {"v": PAYLOAD_VERSION, **payload}
    encoded = json.dumps(payload, separators=(",", ":"))
    if len(encoded.encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"fleet payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    tags = [("t", TAG_FLEET), ("t", task_tag(task_id))]
    if run_id:
        tags.append(("t", run_tag(run_id)))
    tags.append((TAG_FLEET, encoded))
    return tags


def build_delegate(*, task_id: str, attempt_id: str, from_pubkey: str, to_pubkey: str, to_name: str,
                   retrieval_key: str, brief: str, deadline: int, acceptance: list[str], artifact: Artifact | None,
                   run_id: str | None, step: int | None, parent_task: str | None, required: bool,
                   rework_target: str | None, default_next: str | None, thread_root: str | None,
                   thread_parent: str | None) -> OutgoingMessage:
    header = f"@{to_name} ▶ task {short(task_id)}"
    if run_id:
        header += f" (run {short(run_id)}" + (f", step {step})" if step is not None else ")")
    lines = [header, brief.strip(), ""]
    if artifact:
        lines.append(
            f"Artifact: {artifact.repo} @ {artifact.commit}"
            + (f" (branch {artifact.branch})" if artifact.branch else "")
        )
    if acceptance:
        lines.append("Acceptance:")
        lines.extend(f"- {a}" for a in acceptance)
    lines += [
        f"Deadline: {_fmt_deadline(deadline)}",
        f"First: buzz-fleet task ack --task {task_id}",
        f"When done: buzz-fleet task report --task {task_id} --status done|blocked|failed --summary -"
        + (f" --input-commit {artifact.commit}" if artifact else ""),
    ]
    if default_next:
        lines.append(f"Default when done: delegate to @{default_next} (buzz-fleet task delegate --run {run_id} ...)")
    payload = {"type": "delegate", "task": task_id, "attempt": attempt_id, "run": run_id, "step": step,
               "parent_task": parent_task, "required": required, "from": from_pubkey, "to": to_pubkey,
               "deadline": deadline, "rework_target": rework_target,
               "artifact": artifact.model_dump() if artifact else None, "acceptance": acceptance}
    return OutgoingMessage("\n".join(lines), [to_pubkey, retrieval_key], _tags(task_id, run_id, payload),
                           thread_root, thread_parent or thread_root)


def build_ack(*, task_id: str, attempt_id: str, from_pubkey: str, retrieval_key: str, root: str | None,
              parent: str | None) -> OutgoingMessage:
    payload = {"type": "ack", "task": task_id, "attempt": attempt_id, "from": from_pubkey}
    return OutgoingMessage(f"▶ task {short(task_id)} received, starting.", [retrieval_key],
                           _tags(task_id, None, payload), root, parent)


def build_report(*, task_id: str, attempt_id: str, status: ReportStatus, summary: str, from_pubkey: str,
                 recipient_pubkey: str, recipient_name: str | None, retrieval_key: str, next_task: str,
                 input_commit: str | None, output_commit: str | None, evidence: list[str], run_id: str | None,
                 root: str | None, parent: str | None) -> OutgoingMessage:
    who = f"@{recipient_name}" if recipient_name else "@requester"
    lines = [f"{who} {_ICON[status]} task {short(task_id)} {status}: {summary.strip()}"]
    if output_commit:
        lines.append(f"Output commit: {output_commit}")
    lines.extend(f"- {e}" for e in evidence)
    if next_task not in ("default", "none"):
        lines.append(f"Handed onward as task {short(next_task)}.")
    payload = {"type": "report", "task": task_id, "attempt": attempt_id, "from": from_pubkey, "status": status,
               "next": next_task, "input_commit": input_commit,
               "output": {"commit": output_commit} if output_commit else None, "evidence": evidence, "run": run_id}
    return OutgoingMessage("\n".join(lines), [recipient_pubkey, retrieval_key], _tags(task_id, run_id, payload),
                           root, parent)


def build_cancel_task(*, task_id: str, reason: str, from_pubkey: str, assignee_pubkey: str, retrieval_key: str,
                      root: str | None, parent: str | None) -> OutgoingMessage:
    payload = {"type": "cancel-task", "task": task_id, "from": from_pubkey, "reason": reason}
    return OutgoingMessage(f"⏹ task {short(task_id)} cancelled: {reason}", [assignee_pubkey, retrieval_key],
                           _tags(task_id, None, payload), root, parent)


@dataclass(frozen=True)
class FleetEvent:
    id: str
    pubkey: str
    created_at: int
    channel_id: str | None
    content: str
    mentions: list[str] = field(default_factory=list)
    root: str | None = None
    parent: str | None = None
    payload: dict | None = None
    task_id: str | None = None
    run_id: str | None = None

    @property
    def type(self) -> str | None:
        return self.payload.get("type") if self.payload else None


def _decode_payload(value: str, author: str) -> dict | None:
    if len(value.encode()) > MAX_PAYLOAD_BYTES:
        return None
    try:
        obj = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or obj.get("v") != PAYLOAD_VERSION or "type" not in obj:
        return None
    if obj.get("from") != author:
        return None  # payload identity must match the verified event author (spec 5.1)
    return obj


def parse_event(raw: dict) -> FleetEvent:
    channel = root = parent = None
    mentions: list[str] = []
    payload: dict | None = None
    task_id = run_id = None
    for tag in raw.get("tags") or []:
        if not tag:
            continue
        name, value = tag[0], (tag[1] if len(tag) > 1 else "")
        if name == "h":
            channel = value
        elif name == "p":
            mentions.append(value)
        elif name == "e":
            marker = tag[3] if len(tag) > 3 else ""
            if marker == "root":
                root = value
            elif marker == "reply":
                parent = value
        elif name == "t" and value.startswith("fleet:task:"):
            task_id = value.removeprefix("fleet:task:")
        elif name == "t" and value.startswith("fleet:run:"):
            run_id = value.removeprefix("fleet:run:")
        elif name == TAG_FLEET:
            payload = _decode_payload(value, raw["pubkey"])
    if parent and not root:
        root = parent
    return FleetEvent(id=raw["id"], pubkey=raw["pubkey"], created_at=int(raw["created_at"]), channel_id=channel,
                      content=raw.get("content", ""), mentions=mentions, root=root, parent=parent,
                      payload=payload, task_id=task_id, run_id=run_id)
