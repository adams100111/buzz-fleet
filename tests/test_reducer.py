import json
import time

import pytest

from buzz_fleet.cli import fleet_commands as fc
from buzz_fleet.orchestration import protocol
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


# --- Review round 2: mutation-demonstrated gaps (see task-12 fix report). ---


def test_stranger_cannot_add_a_new_attempt_to_someone_elses_task() -> None:
    # Finding 1: deleting the `_apply_delegate` authorization block left all tests green.
    # A stranger (neither conductor nor requester) publishing a delegate with a *new* attempt
    # id for an existing live task must not reassign it -- the original attempt must stay open.
    hijack = _delegate("2", created_at=150, attempt=AT2, frm="e" * 64, to="e" * 64)
    t = reduce([_delegate(), hijack], REC).tasks[T1]
    assert len(t.attempts) == 1
    assert t.attempts[0].attempt_id == AT1 and t.attempts[0].status == "open" and t.assignee == B
    assert any("ignored new attempt" in n for n in t.notes)


def test_requester_can_add_a_new_attempt_without_being_the_conductor() -> None:
    # Finding 1: the accept arm of that same boundary (requester, not just conductor, may add a
    # new attempt) was equally untested -- narrowing the rule to conductor-only would also have
    # gone unnoticed.
    fallback = _delegate("2", created_at=150, attempt=AT2, frm=A, to="d" * 64)
    t = reduce([_delegate(), fallback], REC).tasks[T1]
    assert t.attempts[0].status == "superseded"
    assert t.current.attempt_id == AT2 and t.current.assignee == "d" * 64 and t.status == "open"


def test_cancel_by_requester_on_a_live_task_is_applied() -> None:
    # Finding 2: narrowing `{task.requester, owner_pubkey}` to `{owner_pubkey}` at reducer.py:136
    # passed all 16 original tests because the only requester-issued cancel in the suite
    # (test_cancel_authorization) lands on an already-terminal task. This exercises a
    # requester's cancel on a still-live task.
    by_requester = _ev("2", A, 150, {"type": "cancel-task", "task": T1, "reason": "changed my mind"})
    assert reduce([_delegate(), by_requester], REC).tasks[T1].status == "cancelled"


def test_redelivered_counter_is_idempotent_under_event_redelivery() -> None:
    # Finding 2: deleting the top-level `ev.id in seen` guard at reducer.py:108 passed all 16
    # original tests. That guard is what keeps counters like `redelivered` correct when the
    # same relay event is read twice -- the brief's own "events arrive again on every re-read"
    # determinism property.
    redeliver = _ev("2", COND, 200, {"type": "redeliver", "task": T1, "attempt": AT1})
    t = reduce([_delegate(), redeliver, redeliver], REC).tasks[T1]
    assert t.redelivered == 1


def test_delegate_without_to_is_refused_not_assigned_to_a_mention_tag() -> None:
    # Promoted minor: a delegate payload with no "to" must never fall back to the first "p"
    # mention tag. In the wire tag layout that tag is the retrieval key (see `_ev`'s fixed
    # `["p", RK]` tag) -- a keypair nobody holds -- which would silently assign the task to
    # nobody and leave it un-ackable forever. It must be refused instead.
    bad = _ev("1", A, 100, {"type": "delegate", "task": T1, "attempt": AT1, "deadline": 1000,
                            "required": True, "acceptance": []})  # no "to", no extra mentions
    assert reduce([bad], REC).tasks == {}


def test_delegate_with_non_numeric_deadline_does_not_crash() -> None:
    # Finding 3: a hand-built delegate with `deadline: "soon"` raised ValueError out of
    # `int(...)`, killing `reduce` for every reader on a single hostile event. Must degrade to
    # "refuse this event", never abort. No task can be created without a valid deadline, so
    # (as with the missing-`to` case above) there is nothing to note it against.
    hostile = _delegate(deadline="soon")
    assert reduce([hostile], REC).tasks == {}


def test_delegate_with_non_list_acceptance_does_not_crash() -> None:
    # Finding 3: `acceptance: 5` raised TypeError out of `list(5)` ("int object is not
    # iterable"). Same fix and same "nothing to note against" reasoning as above.
    hostile = _delegate(acceptance=5)
    assert reduce([hostile], REC).tasks == {}


def test_delegate_with_non_string_cmd_does_not_crash_and_is_not_recorded() -> None:
    # Finding 3: `cmd: ["a"]` raised TypeError out of `set.add(["a"])` (a list is unhashable).
    # Unlike deadline/acceptance, a malformed `cmd` is unrelated to whether the delegate itself
    # is valid, so the delegate still applies -- only the bookkeeping `cmd` field is dropped.
    hostile = _delegate(cmd=["a"])
    state = reduce([hostile], REC)
    assert state.tasks[T1].status == "open"
    assert state.seen_cmds == set()


def test_unauthorized_conductor_event_cmd_is_not_recorded() -> None:
    # Finding 4 (ruled deviation from the brief's literal text -- see fix report): a `cmd` must
    # be recorded only for events that pass authorization. Otherwise any fleet member can forge
    # `{"type": "nudge", "cmd": "nudge:<task>:<attempt>"}` from a non-conductor key -- the nudge
    # itself is correctly ignored, but without this fix the cmd would still land in seen_cmds,
    # making the real conductor skip that nudge forever.
    forged_cmd = f"nudge:{T1}:{AT1}"
    forged = _ev("2", "e" * 64, 1100, {"type": "nudge", "task": T1, "attempt": AT1, "cmd": forged_cmd})
    state = reduce([_delegate(), forged], REC)
    assert forged_cmd not in state.seen_cmds
    assert len(state.tasks[T1].notes) == 1


@pytest.mark.parametrize("field,value", [
    ("task", ["boom"]),
    ("task", {"a": 1}),
    ("parent_task", ["boom"]),
    ("parent_task", {"a": 1}),
], ids=["task-list", "task-dict", "parent_task-list", "parent_task-dict"])
def test_hostile_task_and_parent_task_fields_do_not_crash_reduce_render_or_json(field, value) -> None:
    # Finding 1 (final review): an earlier ruling hardened `deadline`, `acceptance`,
    # `to`, and `cmd` against malformed-but-parseable payloads -- it left `task` (the
    # *first* payload field the reducer touches, before any of those checks run) and
    # `parent_task` (read unvalidated by `State.chain_depth`) open. A single kind-9
    # event with `"task": []` from any fleet-channel member broke
    # `state.tasks.get(task_id)` with "unhashable type: 'list'", killing `reduce` --
    # and therefore `tasks`, `task show`, `task delegate`, `task ack`, `task report`,
    # and `fleet agents`, on every machine, for every reader -- with no recovery
    # surface in this plan (`deleted_ids` has no CLI, `run purge` is a later plan).
    hostile = _delegate(**{field: value})
    state = reduce([hostile], REC)
    # The point: this must not raise. A malformed task/parent_task can't be attached
    # to a task record it might not even identify, so the event is refused outright.
    assert state.tasks == {}
    # And the downstream views every reader actually calls must survive too.
    now = int(time.time())
    assert fc.render_tasks(list(state.tasks.values()), now) is not None
    assert [fc.task_to_json(t) for t in state.tasks.values()] == []


@pytest.mark.parametrize("value", [{"a": 1}, ["boom"], 5], ids=["run-dict", "run-list", "run-int"])
def test_hostile_run_field_does_not_crash_ids_short_or_render(value) -> None:
    # Residual (final fix pass): the same class of bug closed for `task`/`parent_task`
    # above was left open for `run`. `reducer.py` stored `p.get("run")` as-is, and
    # `fleet_commands.py`'s `ids.short(t.run_id)` (`full[:8]`) then broke on it:
    # `{"a": 1}` raised KeyError (`dict[slice]`), `["boom"]` raised
    # `rich.errors.NotRenderableError` once handed to a Rich table cell, and `5`
    # raised TypeError (`int` isn't subscriptable). Unlike `task`/`parent_task`,
    # `reduce()` itself survived (so `--json`/action verbs/`fleet agents` kept
    # working) -- but `buzz-fleet tasks` and `task show` were permanently broken for
    # every reader on one hostile event.
    hostile = _delegate(run=value)
    state = reduce([hostile], REC)
    task = state.tasks[T1]
    # A malformed `run` must not refuse the whole delegate (unlike `task`/
    # `parent_task`, it's cosmetic grouping only) -- the task is still created, just
    # without the bad value, and with a note recording the refusal.
    assert task.run_id is None
    assert any("run" in note for note in task.notes)
    now = int(time.time())
    assert fc.render_tasks([task], now) is not None
    assert fc.task_to_json(task)["run_id"] is None


def test_hostile_empty_content_does_not_crash_render_tasks() -> None:
    # Finding 1 (final review): unlike deadline/acceptance/to/cmd, empty `content`
    # doesn't stop a task from being created -- so it has to be handled by every
    # downstream reader instead. `"".splitlines()` is `[]`, not `[""]`, so
    # `render_tasks`'s `summary.splitlines()[0]` raised IndexError on a delegate
    # posted with `content=""`, refusing the whole `tasks`/`task show` table for
    # every reader, not just whoever sent the empty-content delegate.
    hostile = _ev("1", A, 100, {"type": "delegate", "task": T1, "attempt": AT1, "to": B, "deadline": 1000,
                                "required": True, "acceptance": []}, mentions=[B], content="")
    state = reduce([hostile], REC)
    assert state.tasks[T1].brief == ""
    now = int(time.time())
    table = fc.render_tasks(list(state.tasks.values()), now)
    assert table is not None
    assert fc.task_to_json(state.tasks[T1])["brief"] == ""


def test_wire_round_trip_from_build_delegate_through_parse_event_and_reduce() -> None:
    # Addition B (final review): the highest-value test named in the review.
    # Every reducer and CLI test hand-builds the raw event dict rather than
    # deriving it from build_delegate's own output, so payload key names are
    # asserted independently on each side and nothing checks they are the
    # *same* names. The reviewer's mutation proved it: renaming
    # `parent_task` to `parent_taskXX` in build_delegate left all 411
    # existing tests green while silently disabling the chain-depth limit --
    # one of the plan's four global constraints. This test takes
    # build_delegate's real output, shapes it exactly as the relay returns
    # it (h/p/e tags added the way relay.post/signer_client.post_message
    # add them from OutgoingMessage's mentions/root/parent, not hand-typed
    # payload keys), and runs it through parse_event -> reduce, checking the
    # resulting Task's fields came from the real wire payload.
    parent_task_id = "44444444-4444-4444-8444-444444444444"
    msg = protocol.build_delegate(
        task_id=T1, attempt_id=AT1, from_pubkey=A, to_pubkey=B, to_name="Reviewer", retrieval_key=RK,
        brief="Review the CSV export.", deadline=1_800_000_000, acceptance=["tests pass"],
        artifact=None, run_id="run-1", step=None, parent_task=parent_task_id, required=True,
        rework_target=None, default_next=None, thread_root=None, thread_parent=None,
    )
    tags = [["h", CH], *(list(t) for t in msg.tags)] + [["p", m] for m in msg.mentions]
    if msg.root:
        tags.append(["e", msg.root, "", "root"])
    if msg.parent and msg.parent != msg.root:
        tags.append(["e", msg.parent, "", "reply"])
    raw = {"id": "9" * 64, "pubkey": A, "created_at": 1_700_000_000, "kind": 9, "content": msg.content, "tags": tags}

    ev = parse_event(raw)
    task = reduce([ev], REC).tasks[T1]

    assert task.parent_task == parent_task_id
    assert task.run_id == "run-1"
    assert task.assignee == B
    assert task.deadline == 1_800_000_000
    assert task.acceptance == ["tests pass"]
