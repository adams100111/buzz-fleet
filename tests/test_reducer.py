import json

from buzz_fleet.orchestration.protocol import parse_event
from buzz_fleet.orchestration.record import ConductorEntry, FleetRecord
from buzz_fleet.orchestration.reducer import reduce

A, B, OWNER, COND, RK = "a" * 64, "b" * 64, "0" * 64, "c" * 64, "f" * 64
T1, AT1, AT2 = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222", "33333333-3333-4333-8333-333333333333"
CH = "6f1c0000-0000-4000-8000-000000000000"
REC = FleetRecord(retrieval_key=RK, conductors={"primary": ConductorEntry(pubkey=COND, host="vps")}, created_at=1)


def _ev(id_: str, pubkey: str, created_at: int, payload: dict | None, *, mentions=(), root=None, parent=None, content=""):
    tags = [["h", CH], ["p", RK]]
    if payload is not None:
        payload = {"v": 1, "from": pubkey, **payload}
        tags += [["t", "fleet"], ["t", f"fleet:task:{payload.get('task', T1)}"], ["fleet", json.dumps(payload)]]
    tags += [["p", m] for m in mentions]
    if root and parent and root != parent:
        tags += [["e", root, "", "root"], ["e", parent, "", "reply"]]
    elif root:
        tags += [["e", root, "", "reply"]]
    return parse_event({"id": id_ * 64, "pubkey": pubkey, "created_at": created_at, "kind": 9, "content": content, "tags": tags})


def _delegate(id_="1", created_at=100, deadline=1000, attempt=AT1, frm=A, to=B, **extra):
    return _ev(id_, frm, created_at, {"type": "delegate", "task": T1, "attempt": attempt, "to": to, "deadline": deadline,
                                       "required": True, "acceptance": [], **extra}, mentions=[to], content="brief")


def test_delegate_opens_task_with_first_attempt() -> None:
    t = reduce([_delegate()], REC).tasks[T1]
    assert t.status == "open" and t.assignee == B and t.requester == A and t.deadline == 1000
    assert t.root_event_id == "1" * 64 and t.current.attempt_id == AT1 and t.unacked


def test_ack_and_report_by_assignee() -> None:
    ack = _ev("2", B, 150, {"type": "ack", "task": T1, "attempt": AT1}, root="1" * 64)
    rep = _ev("3", B, 200, {"type": "report", "task": T1, "attempt": AT1, "status": "done", "next": "default",
                            "input_commit": None, "output": None, "evidence": []}, mentions=[A], root="1" * 64, content="done!")
    t = reduce([_delegate(), ack, rep], REC).tasks[T1]
    report = t.current.report
    assert t.current.acked_at == 150 and t.status == "done" and report is not None and report["next"] == "default"


def test_report_by_stranger_or_wrong_attempt_is_noted() -> None:
    stranger = _ev("2", "e" * 64, 200, {"type": "report", "task": T1, "attempt": AT1, "status": "done", "next": "default"})
    wrong = _ev("3", B, 201, {"type": "report", "task": T1, "attempt": AT2, "status": "done", "next": "default"})
    t = reduce([_delegate(), stranger, wrong], REC).tasks[T1]
    assert t.status == "open" and len(t.notes) == 2


def test_new_attempt_supersedes_and_late_report_is_noted() -> None:
    fallback = _delegate("2", created_at=300, attempt=AT2, frm=COND, to="d" * 64)
    late = _ev("3", B, 400, {"type": "report", "task": T1, "attempt": AT1, "status": "done", "next": "default"})
    t = reduce([_delegate(), fallback, late], REC).tasks[T1]
    assert t.attempts[0].status == "superseded" and t.current.assignee == "d" * 64 and t.status == "open"
    assert any("superseded" in n for n in t.notes)


def test_duplicate_and_out_of_order_events() -> None:
    rep = _ev("2", B, 200, {"type": "report", "task": T1, "attempt": AT1, "status": "failed", "next": "default"})
    t = reduce([rep, _delegate(), rep, _delegate()], REC).tasks[T1]
    assert t.status == "failed" and len(t.attempts) == 1


def test_cancel_authorization() -> None:
    by_owner = _ev("2", OWNER, 300, {"type": "cancel-task", "task": T1, "reason": "x"})
    by_stranger = _ev("3", "e" * 64, 300, {"type": "cancel-task", "task": T1, "reason": "x"})
    assert reduce([_delegate(), by_stranger], REC, owner_pubkey=OWNER).tasks[T1].status == "open"
    assert reduce([_delegate(), by_owner], REC, owner_pubkey=OWNER).tasks[T1].status == "cancelled"


def test_conductor_only_types_need_conductor_key() -> None:
    nudge_ok = _ev("2", COND, 1100, {"type": "nudge", "task": T1, "attempt": AT1, "cmd": f"nudge:{T1}:{AT1}"})
    nudge_bad = _ev("3", "e" * 64, 1100, {"type": "nudge", "task": T1, "attempt": AT1})
    state = reduce([_delegate(), nudge_ok, nudge_bad], REC)
    assert state.tasks[T1].nudged_at == 1100 and f"nudge:{T1}:{AT1}" in state.seen_cmds
    assert len(state.tasks[T1].notes) == 1


def test_deleted_events_are_dropped_and_plain_chat_ignored() -> None:
    chat = _ev("2", B, 150, None, mentions=[A], root="1" * 64, content="on it")
    assert reduce([_delegate(), chat], REC).tasks[T1].status == "open"
    assert reduce([_delegate()], REC, deleted_ids={"1" * 64}).tasks == {}


def test_limits_helpers() -> None:
    child = _ev("2", B, 120, {"type": "delegate", "task": "44444444-4444-4444-8444-444444444444", "attempt": AT2,
                             "to": A, "deadline": 900, "required": True, "acceptance": [], "parent_task": T1}, mentions=[A])
    state = reduce([_delegate(), child], REC)
    assert state.open_adhoc_by_requester(A) == 1 and state.open_adhoc_by_requester(B) == 1
    assert state.chain_depth("44444444-4444-4444-8444-444444444444") == 2
    assert [t.task_id for t in state.stuck_tasks(now=950)] == ["44444444-4444-4444-8444-444444444444"]


# --- Additional tests: specified rules the brief's own test file above leaves unexercised. ---


def test_chain_depth_of_root_task_is_one() -> None:
    # test_limits_helpers only exercises chain_depth for a 2-deep chain; the root case (no
    # parent_task) is the base of that recursion and deserves its own check.
    assert reduce([_delegate()], REC).chain_depth(T1) == 1


def test_view_helpers_open_and_unacked() -> None:
    # open_tasks() and unacked_tasks() are part of the specified State interface but no brief
    # test asserts their return values directly (open_adhoc_by_requester only calls open_tasks()
    # internally).
    before = reduce([_delegate()], REC)
    assert [t.task_id for t in before.open_tasks()] == [T1]
    assert [t.task_id for t in before.unacked_tasks()] == [T1]
    ack = _ev("2", B, 150, {"type": "ack", "task": T1, "attempt": AT1}, root="1" * 64)
    after = reduce([_delegate(), ack], REC)
    assert [t.task_id for t in after.open_tasks()] == [T1]  # acked is still live/open
    assert after.unacked_tasks() == []  # but no longer unacked


def test_duplicate_delegate_same_attempt_different_event_id_is_ignored() -> None:
    # The brief's own "duplicate and out of order" test only re-delivers the *identical* event
    # id, which is already caught by the top-level `ev.id in seen` guard. The Rules paragraph
    # separately specifies "a duplicate delegate (same task and attempt) is ignored" -- this
    # exercises that as a distinct rule, via a different event id for the same (task, attempt).
    dup = _delegate("9", created_at=150, attempt=AT1, frm=A, to=B)
    t = reduce([_delegate(), dup], REC).tasks[T1]
    assert len(t.attempts) == 1
    assert any("duplicate delegate" in n for n in t.notes)


def test_conductor_only_types_rejected_from_everyone_when_record_is_none() -> None:
    # Rules paragraph: conductor-only types are accepted only from a pubkey in
    # record.conductors, "or, when record is None, from nobody". No brief test calls reduce
    # with record=None at all.
    nudge = _ev("2", COND, 1100, {"type": "nudge", "task": T1, "attempt": AT1})
    t = reduce([_delegate(), nudge], None).tasks[T1]
    assert t.nudged_at is None
    assert len(t.notes) == 1


def test_terminal_status_is_final_for_contradicting_events() -> None:
    # "Terminal states are final. A later contradicting event becomes a note, not a state
    # change." The brief's superseded-attempt test covers one terminal status; this covers a
    # live attempt reaching a terminal report status directly, plus cancel-task on an
    # already-terminal task.
    rep_done = _ev("2", B, 200, {"type": "report", "task": T1, "attempt": AT1, "status": "done", "next": "default"})
    rep_failed_later = _ev("3", B, 250, {"type": "report", "task": T1, "attempt": AT1, "status": "failed", "next": "default"})
    cancel_after = _ev("4", A, 300, {"type": "cancel-task", "task": T1, "reason": "too late"})
    t = reduce([_delegate(), rep_done, rep_failed_later, cancel_after], REC).tasks[T1]
    assert t.status == "done"
    assert any("on done attempt ignored" in n for n in t.notes)
    assert any("ignored cancel" in n for n in t.notes)


def test_sort_by_created_at_then_id_is_deterministic() -> None:
    # "Sorted by (created_at, id) ... The id tiebreak is what makes ties deterministic rather
    # than delivery-dependent." No brief test has two competing events sharing a created_at
    # where the outcome depends on order. Event id "5"*64 sorts before "6"*64, so `first` must
    # open the task and `second` must be treated as a (conductor-authorized) new attempt --
    # regardless of the order the events are handed to reduce().
    t2 = "99999999-9999-4999-8999-999999999999"
    first = _ev("5", A, 500, {"type": "delegate", "task": t2, "attempt": AT1, "to": B, "deadline": 1000,
                              "required": True, "acceptance": []}, mentions=[B])
    second = _ev("6", COND, 500, {"type": "delegate", "task": t2, "attempt": AT2, "to": "d" * 64, "deadline": 2000,
                                  "required": True, "acceptance": []}, mentions=["d" * 64])
    for events in ([first, second], [second, first]):
        t = reduce(events, REC).tasks[t2]
        assert t.attempts[0].attempt_id == AT1 and t.attempts[0].assignee == B and t.attempts[0].status == "superseded"
        assert t.attempts[1].attempt_id == AT2 and t.attempts[1].status == "open"


def test_reduce_is_idempotent() -> None:
    # "reduce(events, record) -> State is a pure function ... reducing the same events twice
    # yields the same state." No brief test calls reduce() twice on the same input and compares.
    events = [_delegate(), _ev("2", B, 150, {"type": "ack", "task": T1, "attempt": AT1}, root="1" * 64)]
    state1 = reduce(events, REC)
    state2 = reduce(events, REC)
    assert state1.tasks == state2.tasks
    assert state1.seen_cmds == state2.seen_cmds
