import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from buzz_fleet.cli import fleet_commands as fc
from buzz_fleet.cli.app import app
from buzz_fleet.orchestration.identity import Identity
from buzz_fleet.orchestration.protocol import Artifact

CH, RK, OWNER = "6f1c0000-0000-4000-8000-000000000000", "r" * 64, "0" * 64
A, B = "a" * 64, "b" * 64
AGENT = Identity(nsec="nsec1a", pubkey=A, relay_url="wss://r", auth_tag=None, fleet_channel=CH, retrieval_key=RK,
                 is_owner=False, owner_pubkey=OWNER, record=None)
REVIEWER = Identity(**{**AGENT.__dict__, "nsec": "nsec1b", "pubkey": B})
cli = CliRunner()
T1, AT1 = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


class FakeRunner:
    def __init__(self, events: list[dict] | None = None, *, post_error: str | None = None) -> None:
        self.events, self.post_error = events or [], post_error
        self.calls: list[list[str]] = []

    def run(self, args):
        self.calls.append(args)
        sub = args[1]
        if sub == "channel-members":
            out = json.dumps({"ok": True, "members": [{"pubkey": B, "display_name": "Reviewer"},
                                                       {"pubkey": A, "display_name": "Implementer"}]})
        elif sub == "post-message":
            if self.post_error and len([c for c in self.calls if c[1] == "post-message"]) == 1:
                return subprocess.CompletedProcess(args, 1, stdout=json.dumps({"ok": False, "error": self.post_error}), stderr="")
            out = json.dumps({"ok": True, "event_id": "e" * 64})
        elif sub == "query":
            f = json.loads(args[args.index("--filter") + 1])
            evs = self.events if "authors" not in f else []
            out = "".join(json.dumps(e) + "\n" for e in evs)
        elif sub == "pubkey-from-nsec":
            out = json.dumps({"ok": True, "public_key": A})
        else:
            raise AssertionError(sub)
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")


_DEFAULT_ARTIFACT = {"repo": "git@x:o/r.git", "commit": "c" * 40, "branch": None, "base": None}


def _delegate_event(task=T1, attempt=AT1, requester=A, assignee=B, run=None, parent=None, event_id=None,
                    artifact=_DEFAULT_ARTIFACT) -> dict:
    payload = {"v": 1, "type": "delegate", "task": task, "attempt": attempt, "run": run, "step": None, "parent_task": parent,
               "required": True, "from": requester, "to": assignee, "deadline": 1000, "rework_target": None,
               "artifact": artifact, "acceptance": []}
    # event_id defaults to task[:8] * 8 (the brief's own fixture derivation) -- but two
    # tasks sharing an 8-char task-id prefix would then collide on event id too, and the
    # reducer's own event-level dedup (reduce()'s `seen` set) would silently drop the
    # second one. The explicit override lets a test construct a genuine prefix ambiguity
    # (same task-id prefix, distinct underlying relay events) without tripping that dedup.
    return {"id": event_id or task[:8] * 8, "pubkey": requester, "created_at": 100, "kind": 9, "content": "brief",
            "tags": [["h", CH], ["p", RK], ["t", "fleet"], ["t", f"fleet:task:{task}"], ["p", assignee], ["fleet", json.dumps(payload)]]}


def _post(runner: FakeRunner) -> list[str]:
    return next(c for c in runner.calls if c[1] == "post-message")


def test_delegate_posts_and_returns_ids(monkeypatch) -> None:
    monkeypatch.setattr(fc.ids, "new_id", lambda: T1)
    runner = FakeRunner()
    out = fc.delegate_task(runner, AGENT, to="Reviewer", brief="Review it", wait_seconds=1800, acceptance=["tests pass"],
                           artifact=Artifact(repo="git@x:o/r.git", commit="c" * 40), run_id=None, thread_root="d" * 64,
                           parent_task=None, required=True, channel=None, cwd=None, git_run=None, now=1000)
    assert out["task"] == T1 and out["deadline"] == 2800 and out["channel"] == CH
    post = _post(runner)
    assert post[post.index("--mention") + 1] == B and RK in post and post[post.index("--root") + 1] == "d" * 64
    assert "c" * 40 in post[post.index("--content") + 1]


def test_delegate_detects_artifact_from_checkout(monkeypatch) -> None:
    monkeypatch.setattr(fc.git_artifact, "detect", lambda cwd, run: Artifact(repo="git@x:o/r.git", commit="d" * 40))
    runner = FakeRunner()
    fc.delegate_task(runner, AGENT, to="Reviewer", brief="x", wait_seconds=60, acceptance=[], artifact=None, run_id=None,
                     thread_root=None, parent_task=None, required=True, channel=None, cwd=Path("/x"), git_run=lambda a: None, now=1)
    assert "d" * 40 in _post(runner)[_post(runner).index("--content") + 1]


def test_delegate_enforces_adhoc_limits() -> None:
    open_tasks = [_delegate_event(task=f"{i:08d}-0000-4000-8000-000000000000", attempt=f"{i:08d}-1111-4111-8111-111111111111") for i in range(5)]
    runner = FakeRunner(events=open_tasks)
    with pytest.raises(RuntimeError, match="5 open"):
        fc.delegate_task(runner, AGENT, to="Reviewer", brief="x", wait_seconds=60, acceptance=[], artifact=None, run_id=None,
                         thread_root=None, parent_task=None, required=True, channel=None, cwd=None, git_run=None, now=1)


def test_delegate_requires_channel() -> None:
    ident = Identity(**{**AGENT.__dict__, "fleet_channel": None, "retrieval_key": None})
    with pytest.raises(RuntimeError, match="fleet init"):
        fc.delegate_task(FakeRunner(), ident, to="Reviewer", brief="x", wait_seconds=60, acceptance=[], artifact=None,
                         run_id=None, thread_root=None, parent_task=None, required=True, channel=None, cwd=None, git_run=None, now=1)


def test_delegate_recovers_from_ambiguous_publish(monkeypatch) -> None:
    monkeypatch.setattr(fc.ids, "new_id", lambda: T1)
    # attempt=T1 (not the module default AT1): this fixture represents the relay
    # already holding *our own* just-minted task+attempt -- i.e. the publish that
    # appeared to time out actually landed. With the default AT1 the fixture would
    # be an unrelated event that merely shares a task id (an artifact of new_id
    # being mocked to a constant), which is not what "recovers from ambiguous
    # publish" is testing.
    runner = FakeRunner(events=[_delegate_event(attempt=T1)], post_error="publish timeout waiting for OK")
    out = fc.delegate_task(runner, AGENT, to="Reviewer", brief="x", wait_seconds=60, acceptance=[], artifact=None, run_id=None,
                           thread_root=None, parent_task=None, required=True, channel=None, cwd=None, git_run=None, now=1)
    assert out["task"] == T1 and len([c for c in runner.calls if c[1] == "post-message"]) == 1


def test_ack_and_report_thread_and_mention_requester() -> None:
    runner = FakeRunner(events=[_delegate_event()])
    fc.ack_task(runner, REVIEWER, task_ref=T1[:8], channel=None)
    post = _post(runner)
    assert post[post.index("--root") + 1] == T1[:8] * 8 and post[post.index("--mention") + 1] == RK
    runner = FakeRunner(events=[_delegate_event()])
    out = fc.report_task(runner, REVIEWER, task_ref=T1, status="done", summary="Looks good", next_task="default",
                         input_commit="c" * 40, output_commit=None, evidence=[], channel=None)
    assert out["task"] == T1
    post = _post(runner)
    assert post[post.index("--mention") + 1] == A and post[post.index("--content") + 1].startswith("@Implementer ✅")


def test_report_refusals() -> None:
    runner = FakeRunner(events=[_delegate_event()])
    with pytest.raises(RuntimeError, match="assigned to"):
        fc.report_task(runner, AGENT, task_ref=T1, status="done", summary="x", next_task="default", input_commit=None,
                       output_commit=None, evidence=[], channel=None)
    with pytest.raises(RuntimeError, match="input-commit"):
        fc.report_task(runner, REVIEWER, task_ref=T1, status="done", summary="x", next_task="default", input_commit="9" * 40,
                       output_commit=None, evidence=[], channel=None)
    with pytest.raises(RuntimeError, match="unknown id"):
        fc.report_task(runner, REVIEWER, task_ref="zzzzzzzz", status="done", summary="x", next_task="default",
                       input_commit=None, output_commit=None, evidence=[], channel=None)


def test_report_with_non_dict_artifact_does_not_crash() -> None:
    # Finding 1 (final review): `task.artifact` comes straight from a peer's delegate
    # payload via the reducer, stored as-is with no type check. `(task.artifact or
    # {}).get("commit")` raised AttributeError ('str' object has no attribute 'get')
    # on `artifact: "not-a-dict"`, refusing `report` outright for every reader of that
    # task -- not just whoever sent the hostile delegate.
    runner = FakeRunner(events=[_delegate_event(artifact="not-a-dict")])
    out = fc.report_task(runner, REVIEWER, task_ref=T1, status="done", summary="ok", next_task="default",
                         input_commit=None, output_commit=None, evidence=[], channel=None)
    assert out["task"] == T1


def test_ambiguous_task_prefix_raises_and_lists_candidates() -> None:
    # Global constraint: "a task reference accepts a unique prefix; ambiguity is an
    # error listing candidates, never a silent pick." The behaviour is already
    # correct (ids.match_prefix), but nothing exercised it through the task_cli
    # surface -- silently acting on the wrong task is exactly what this must prevent.
    prefix = "abcdefgh"
    task_x, attempt_x = f"{prefix}-1111-4111-8111-111111111111", f"{prefix}-aaaa-4aaa-8aaa-111111111111"
    task_y, attempt_y = f"{prefix}-2222-4222-8222-222222222222", f"{prefix}-bbbb-4bbb-8bbb-222222222222"
    runner = FakeRunner(events=[_delegate_event(task=task_x, attempt=attempt_x, event_id="e" * 63 + "1"),
                                _delegate_event(task=task_y, attempt=attempt_y, event_id="e" * 63 + "2")])
    with pytest.raises(RuntimeError, match="ambiguous id") as exc_info:
        fc.ack_task(runner, REVIEWER, task_ref=prefix, channel=None)
    message = str(exc_info.value)
    assert prefix in message
    assert message.count(",") == 1, f"expected exactly two candidates listed, got: {message!r}"


def test_cancel_by_requester_only() -> None:
    runner = FakeRunner(events=[_delegate_event()])
    assert fc.cancel_task(runner, AGENT, task_ref=T1, reason="obsolete", channel=None)["task"] == T1
    with pytest.raises(RuntimeError, match="requester or the owner"):
        fc.cancel_task(FakeRunner(events=[_delegate_event()]), REVIEWER, task_ref=T1, reason="x", channel=None)


def test_cli_delegate_reads_stdin_and_prints_json(monkeypatch) -> None:
    monkeypatch.setattr(fc, "RealCommandRunner", lambda: FakeRunner())
    monkeypatch.setattr(fc, "resolve_identity", lambda env, runner, community_id: AGENT)
    result = cli.invoke(app, ["task", "delegate", "--to", "Reviewer", "--brief", "-", "--wait", "5m",
                              "--repo", "git@x:o/r.git", "--commit", "c" * 40], input="Review please\n")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["channel"] == CH


def test_cli_report_rejects_bad_status_and_next() -> None:
    assert cli.invoke(app, ["task", "report", "--task", T1, "--status", "maybe", "--summary", "x"]).exit_code == 1
    assert cli.invoke(app, ["task", "report", "--task", T1, "--status", "done", "--summary", "x", "--next", "bogus"]).exit_code == 1


# --- Tests added beyond the brief -----------------------------------------------
#
# The brief's own tests leave several specified behaviours with no discriminating
# test: the chain-depth limit (only the open-ad-hoc limit is exercised), the
# "genuinely absent -> repost the *same* message" half of the ambiguous-publish
# recovery (the brief's own recovery test only exercises the "found a match, don't
# repost" half), that recovery reading via fetch_thread on the task's own root
# rather than a channel-wide scan, cancellation by the owner (only requester-allowed
# and stranger-blocked are covered), and that a non-timeout publish failure is
# propagated rather than silently retried or swallowed into a fake success.


def test_delegate_enforces_chain_depth_limit() -> None:
    task_ids = [f"c{i}00000-0000-4000-8000-000000000000" for i in range(4)]
    attempt_ids = [f"c{i}00000-1111-4111-8111-000000000000" for i in range(4)]
    chain, parent = [], None
    for task_id, attempt_id in zip(task_ids, attempt_ids, strict=True):
        chain.append(_delegate_event(task=task_id, attempt=attempt_id, parent=parent))
        parent = task_id
    runner = FakeRunner(events=chain)
    with pytest.raises(RuntimeError, match="exceed depth"):
        fc.delegate_task(runner, AGENT, to="Reviewer", brief="x", wait_seconds=60, acceptance=[], artifact=None, run_id=None,
                         thread_root=None, parent_task=task_ids[-1], required=True, channel=None, cwd=None, git_run=None, now=1)


def test_ack_recovers_via_thread_fetch_and_reposts_same_message_when_genuinely_absent() -> None:
    # The pre-existing event is a *different* type (delegate, not ack) for the same
    # task, so no matching ack is ever found on the relay: this is the "genuinely
    # never landed" half of ambiguous-publish recovery, distinct from the brief's own
    # test which only exercises "found a match, don't repost".
    runner = FakeRunner(events=[_delegate_event()], post_error="publish timeout waiting for OK")
    out = fc.ack_task(runner, REVIEWER, task_ref=T1, channel=None)
    post_calls = [c for c in runner.calls if c[1] == "post-message"]
    assert len(post_calls) == 2, "genuine absence must trigger exactly one retry of the same message, not more"
    assert post_calls[0] == post_calls[1], "the retry must be the identical message, not a freshly built one"
    assert out["event_id"] == "e" * 64

    first_post_idx = next(i for i, c in enumerate(runner.calls) if c[1] == "post-message")
    recovery_queries = [c for c in runner.calls[first_post_idx + 1:] if c[1] == "query"]
    assert recovery_queries, "expected a re-read after the timeout"
    filters = [json.loads(c[c.index("--filter") + 1]) for c in recovery_queries]
    assert any(f.get("#e") == [T1[:8] * 8] for f in filters), \
        "recovery must re-read via fetch_thread on the task's own root, not a channel-wide fleet-events scan"


def test_cancel_allowed_for_owner_even_when_not_requester() -> None:
    owner_ident = Identity(**{**AGENT.__dict__, "nsec": "nsec1o", "pubkey": OWNER, "is_owner": True, "owner_pubkey": OWNER})
    runner = FakeRunner(events=[_delegate_event()])
    assert fc.cancel_task(runner, owner_ident, task_ref=T1, reason="obsolete", channel=None)["task"] == T1


def test_delegate_does_not_swallow_a_non_timeout_publish_failure() -> None:
    runner = FakeRunner(post_error="not authorized to post here")
    with pytest.raises(RuntimeError, match="not authorized"):
        fc.delegate_task(runner, AGENT, to="Reviewer", brief="x", wait_seconds=60, acceptance=[], artifact=None, run_id=None,
                         thread_root=None, parent_task=None, required=True, channel=None, cwd=None, git_run=None, now=1)
    assert len([c for c in runner.calls if c[1] == "post-message"]) == 1, "a non-timeout failure must never be retried"


# --- Task 15: views (task show, tasks) --------------------------------------------

from buzz_fleet.orchestration.protocol import parse_event
from buzz_fleet.orchestration.reducer import reduce

T2, AT2 = "44444444-4444-4444-8444-444444444444", "55555555-5555-4555-8555-555555555555"


def _state():
    return reduce([parse_event(_delegate_event()), parse_event(_delegate_event(task=T2, attempt=AT2, requester=B, assignee=A))], None)


def test_task_rows_filters() -> None:
    s = _state()
    assert {t.task_id for t in fc.task_rows(s, only_open=True, only_stuck=False, only_unacked=False, mine=None, now=500)} == {T1, T2}
    assert {t.task_id for t in fc.task_rows(s, only_open=False, only_stuck=True, only_unacked=False, mine=None, now=5000)} == {T1, T2}
    assert fc.task_rows(s, only_open=False, only_stuck=True, only_unacked=False, mine=None, now=500) == []
    assert [t.task_id for t in fc.task_rows(s, only_open=False, only_stuck=False, only_unacked=False, mine=A, now=500)] == [T2]


def test_cli_tasks_json_and_show(monkeypatch) -> None:
    monkeypatch.setattr(fc, "RealCommandRunner", lambda: FakeRunner(events=[_delegate_event()]))
    monkeypatch.setattr(fc, "resolve_identity", lambda env, runner, community_id: AGENT)
    result = cli.invoke(app, ["tasks", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["task_id"] == T1
    result = cli.invoke(app, ["task", "show", T1[:8]])
    assert result.exit_code == 0 and "11111111" in result.output and "open" in result.output


# --- Tests added beyond the brief (Task 15) ---------------------------------------
#
# The brief's own test_task_rows_filters exercises each filter in isolation (and one
# combination, only_open=False/only_stuck=False/only_unacked=False + mine), but never
# builds a state with every reachable status side by side and checks each filter --
# and filter *combination* -- returns exactly the right set. A filter that is too
# INCLUSIVE (e.g. --stuck matching a `done` task whose deadline has simply passed,
# because `late()` forgot to gate on `is_live`) would pass every test the brief itself
# gives but silently make a finished task look like it's rotting. The following builds
# one task per reachable Task.status ("superseded" is not reachable via the `status`
# property -- see below) plus distinguishes assignees, and checks every filter and
# every pairwise combination against an exact expected set, never just a length.


def _ack_event(task: str, attempt: str, assignee: str, event_id: str) -> dict:
    payload = {"v": 1, "type": "ack", "task": task, "attempt": attempt, "from": assignee}
    return {"id": event_id, "pubkey": assignee, "created_at": 150, "kind": 9, "content": "ack",
            "tags": [["h", CH], ["p", RK], ["t", "fleet"], ["t", f"fleet:task:{task}"], ["fleet", json.dumps(payload)]]}


def _report_event(task: str, attempt: str, assignee: str, status: str, event_id: str) -> dict:
    payload = {"v": 1, "type": "report", "task": task, "attempt": attempt, "from": assignee, "status": status,
               "next": "default", "input_commit": None, "output": None, "evidence": [], "run": None}
    return {"id": event_id, "pubkey": assignee, "created_at": 200, "kind": 9, "content": f"{status} report",
            "tags": [["h", CH], ["p", RK], ["t", "fleet"], ["t", f"fleet:task:{task}"], ["fleet", json.dumps(payload)]]}


def _cancel_event(task: str, requester: str, event_id: str) -> dict:
    payload = {"v": 1, "type": "cancel-task", "task": task, "from": requester, "reason": "obsolete"}
    return {"id": event_id, "pubkey": requester, "created_at": 200, "kind": 9, "content": "cancelled",
            "tags": [["h", CH], ["p", RK], ["t", "fleet"], ["t", f"fleet:task:{task}"], ["fleet", json.dumps(payload)]]}


def _task_id(n: int) -> str:
    return f"7{n}000000-0000-4000-8000-000000000000"


def _attempt_id(n: int) -> str:
    return f"7{n}000000-1111-4111-8111-000000000000"


# One task per status Task.status can actually report. "superseded" is deliberately
# excluded: _apply_delegate only ever marks the *previous* attempt superseded, and
# Task.status/assignee always read the newest attempt (`current` = attempts[-1]) --
# so a task's overall `.status` can never observably be "superseded", only an
# individual (non-current) Attempt's `.status` field, which task_rows/render_tasks
# never look at directly. Confirmed by reading reducer._apply_delegate.
T_OPEN, T_ACKED, T_DONE, T_BLOCKED, T_FAILED, T_CANCELLED = (_task_id(i) for i in range(6))


def _all_statuses_state():
    events = [
        _delegate_event(task=T_OPEN, attempt=_attempt_id(0), requester=A, assignee=B, event_id="ev0"),
        _delegate_event(task=T_ACKED, attempt=_attempt_id(1), requester=B, assignee=A, event_id="ev1"),
        _ack_event(T_ACKED, _attempt_id(1), A, "ev1a"),
        _delegate_event(task=T_DONE, attempt=_attempt_id(2), requester=A, assignee=B, event_id="ev2"),
        _ack_event(T_DONE, _attempt_id(2), B, "ev2a"),
        _report_event(T_DONE, _attempt_id(2), B, "done", "ev2r"),
        _delegate_event(task=T_BLOCKED, attempt=_attempt_id(3), requester=A, assignee=B, event_id="ev3"),
        _ack_event(T_BLOCKED, _attempt_id(3), B, "ev3a"),
        _report_event(T_BLOCKED, _attempt_id(3), B, "blocked", "ev3r"),
        _delegate_event(task=T_FAILED, attempt=_attempt_id(4), requester=A, assignee=B, event_id="ev4"),
        _ack_event(T_FAILED, _attempt_id(4), B, "ev4a"),
        _report_event(T_FAILED, _attempt_id(4), B, "failed", "ev4r"),
        _delegate_event(task=T_CANCELLED, attempt=_attempt_id(5), requester=A, assignee=B, event_id="ev5"),
        _cancel_event(T_CANCELLED, A, "ev5c"),
    ]
    return reduce([parse_event(e) for e in events], None)


def test_task_rows_exact_sets_per_status_and_combination() -> None:
    s = _all_statuses_state()
    assert {t.task_id: t.status for t in s.tasks.values()} == {
        T_OPEN: "open", T_ACKED: "acked", T_DONE: "done", T_BLOCKED: "blocked",
        T_FAILED: "failed", T_CANCELLED: "cancelled",
    }

    def rows(*, only_open=False, only_stuck=False, only_unacked=False, mine=None):
        return {t.task_id for t in fc.task_rows(s, only_open=only_open, only_stuck=only_stuck,
                                                only_unacked=only_unacked, mine=mine, now=5000)}

    # deadline on every fixture is 1000 (the fixture's hardcoded payload), so
    # now=5000 is "past deadline" for every task -- late() must still gate on
    # is_live, or a finished task would wrongly show up as --stuck.
    cases = {
        # (only_open, only_stuck, only_unacked, mine) -> expected task_id set
        (True, False, False, None): {T_OPEN, T_ACKED},                     # --open
        (False, True, False, None): {T_OPEN, T_ACKED},                     # --stuck (only live tasks, even past deadline)
        (False, False, True, None): {T_OPEN},                              # --unacked (open only, not acked)
        (False, False, False, A): {T_ACKED},                               # --mine (assignee, not requester)
        (False, False, False, B): {T_OPEN, T_DONE, T_BLOCKED, T_FAILED, T_CANCELLED},
        (True, False, True, None): {T_OPEN},                               # --open --unacked
        (True, False, False, A): {T_ACKED},                                # --open --mine
        (False, True, False, A): {T_ACKED},                                # --stuck --mine
        (True, True, False, None): {T_OPEN, T_ACKED},                      # --open --stuck (both live+late here)
        (False, False, True, A): set(),                                    # --unacked --mine=A: A's task is acked, not open
        (True, False, True, A): set(),                                     # --open --unacked --mine=A: same, empty
    }
    for (only_open, only_stuck, only_unacked, mine), expected in cases.items():
        got = {t.task_id for t in fc.task_rows(s, only_open=only_open, only_stuck=only_stuck,
                                               only_unacked=only_unacked, mine=mine, now=5000)}
        assert got == expected, f"open={only_open} stuck={only_stuck} unacked={only_unacked} mine={mine!r}: got {got}, want {expected}"

    # Terminal tasks are never --stuck even before any deadline math: a task that
    # finished ahead of its deadline must not be reported as rotting either.
    assert rows(only_stuck=True) & {T_DONE, T_BLOCKED, T_FAILED, T_CANCELLED} == set()
    # No filters at all: every task, regardless of status.
    assert rows() == {T_OPEN, T_ACKED, T_DONE, T_BLOCKED, T_FAILED, T_CANCELLED}


def test_task_rows_sorts_newest_first() -> None:
    # created_at comes from the delegate event's created_at (100 for every
    # `_delegate_event` fixture) -- give the two tasks distinct created_at via
    # distinct event ids/timestamps by building the events directly.
    older = _delegate_event(task=T1, attempt=AT1, event_id="older")
    newer = dict(_delegate_event(task=T2, attempt=AT2, requester=B, assignee=A, event_id="newer"))
    newer["created_at"] = 500
    s = reduce([parse_event(older), parse_event(newer)], None)
    rows = fc.task_rows(s, only_open=False, only_stuck=False, only_unacked=False, mine=None, now=1000)
    assert [t.task_id for t in rows] == [T2, T1]


def test_task_show_unknown_id_errors_to_stderr_json(monkeypatch) -> None:
    monkeypatch.setattr(fc, "RealCommandRunner", lambda: FakeRunner(events=[_delegate_event()]))
    monkeypatch.setattr(fc, "resolve_identity", lambda env, runner, community_id: AGENT)
    result = cli.invoke(app, ["task", "show", "deadbeef"])
    assert result.exit_code == 1
    assert "unknown id" in json.loads(result.output)["error"]


def test_task_show_ambiguous_prefix_lists_candidates(monkeypatch) -> None:
    prefix = "abcdefgh"
    task_x, attempt_x = f"{prefix}-1111-4111-8111-111111111111", f"{prefix}-aaaa-4aaa-8aaa-111111111111"
    task_y, attempt_y = f"{prefix}-2222-4222-8222-222222222222", f"{prefix}-bbbb-4bbb-8bbb-222222222222"
    monkeypatch.setattr(fc, "RealCommandRunner", lambda: FakeRunner(
        events=[_delegate_event(task=task_x, attempt=attempt_x, event_id="e" * 63 + "1"),
                _delegate_event(task=task_y, attempt=attempt_y, event_id="e" * 63 + "2")]))
    monkeypatch.setattr(fc, "resolve_identity", lambda env, runner, community_id: AGENT)
    result = cli.invoke(app, ["task", "show", prefix])
    assert result.exit_code == 1
    assert "ambiguous id" in json.loads(result.output)["error"]


def test_cli_tasks_empty_state_prints_no_rows(monkeypatch) -> None:
    monkeypatch.setattr(fc, "RealCommandRunner", lambda: FakeRunner(events=[]))
    monkeypatch.setattr(fc, "resolve_identity", lambda env, runner, community_id: AGENT)
    result = cli.invoke(app, ["tasks", "--json"])
    assert result.exit_code == 0 and json.loads(result.output) == []
    result = cli.invoke(app, ["tasks"])
    assert result.exit_code == 0 and "Fleet tasks" in result.output


def test_task_to_json_promotes_status_and_assignee_to_top_level() -> None:
    # dataclasses.asdict(task) alone would drop these -- they're @property on Task
    # (derived from attempts[-1]), not dataclass fields. A JSON consumer must not
    # have to know Attempt's shape, or assume attempts is never empty, just to find
    # out what's happening with a task.
    s = _all_statuses_state()
    acked = s.tasks[T_ACKED]
    out = fc.task_to_json(acked)
    assert out["status"] == "acked"
    assert out["assignee"] == A
    assert out["is_live"] is True
    assert out["unacked"] is False
    # still round-trips through JSON (no non-serializable values snuck in), and the
    # ordinary dataclass fields (asdict's own contribution) are still present.
    reloaded = json.loads(json.dumps(out))
    assert reloaded["task_id"] == T_ACKED
    assert reloaded["status"] == "acked"

    done = fc.task_to_json(s.tasks[T_DONE])
    assert done["status"] == "done" and done["is_live"] is False and done["unacked"] is False

    open_task = fc.task_to_json(s.tasks[T_OPEN])
    assert open_task["status"] == "open" and open_task["is_live"] is True and open_task["unacked"] is True


def test_cli_tasks_json_includes_status_and_assignee(monkeypatch) -> None:
    monkeypatch.setattr(fc, "RealCommandRunner", lambda: FakeRunner(events=[_delegate_event()]))
    monkeypatch.setattr(fc, "resolve_identity", lambda env, runner, community_id: AGENT)
    result = cli.invoke(app, ["tasks", "--json"])
    assert result.exit_code == 0, result.output
    row = json.loads(result.output)[0]
    assert row["status"] == "open" and row["assignee"] == B and row["is_live"] is True and row["unacked"] is True
    result = cli.invoke(app, ["task", "show", T1[:8], "--json"])
    assert result.exit_code == 0, result.output
    shown = json.loads(result.output)
    assert shown["status"] == "open" and shown["assignee"] == B


def test_cli_fleet_agents_json(monkeypatch) -> None:
    from buzz_fleet.orchestration.relay import DirectoryEntry

    monkeypatch.setattr(fc, "_load_manager", lambda community: type("M", (), {"ensure_fleet_record": lambda self: None, "_community": None})())
    monkeypatch.setattr(fc, "RealCommandRunner", lambda: FakeRunner())
    monkeypatch.setattr(fc, "resolve_identity", lambda env, runner, community_id: AGENT)
    monkeypatch.setattr(fc.relay, "directory", lambda runner, ident, channel_id: [DirectoryEntry(
        pubkey=B, display_name="Reviewer", role="reviewer", capabilities=["laravel"], description=None, harness="claude",
        host="vps", online=True, last_seen=1, live_tasks=0, version="0.8.0")])
    result = cli.invoke(app, ["fleet", "agents", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["role"] == "reviewer"


# --- Tests added beyond the brief (Task 18: fleet agents) --------------------
#
# The brief's own test exercises the JSON path with a single fully-populated
# entry. It says nothing about the table-rendering path, the error path when
# `relay.directory` raises, or the empty-fleet case -- all specified behaviour
# ("Errors to stderr as {"error": "..."} with exit 1. Never fake success." and
# the shared `_ERRORS`/`_fail` contract every other fleet/task command follows).


def test_cli_fleet_agents_table_renders_columns(monkeypatch) -> None:
    from buzz_fleet.orchestration.relay import DirectoryEntry

    monkeypatch.setattr(fc, "RealCommandRunner", lambda: FakeRunner())
    monkeypatch.setattr(fc, "resolve_identity", lambda env, runner, community_id: AGENT)
    monkeypatch.setattr(fc.relay, "directory", lambda runner, ident, channel_id: [DirectoryEntry(
        pubkey=B, display_name="Reviewer", role="reviewer", capabilities=["laravel"], description=None, harness="claude",
        host="vps", online=True, last_seen=1, live_tasks=2, version="0.8.0")])
    result = cli.invoke(app, ["fleet", "agents"])
    assert result.exit_code == 0, result.output
    assert "Reviewer" in result.output and "reviewer" in result.output and "laravel" in result.output
    assert "claude" in result.output and "vps" in result.output


def test_cli_fleet_agents_empty_prints_no_rows(monkeypatch) -> None:
    monkeypatch.setattr(fc, "RealCommandRunner", lambda: FakeRunner())
    monkeypatch.setattr(fc, "resolve_identity", lambda env, runner, community_id: AGENT)
    monkeypatch.setattr(fc.relay, "directory", lambda runner, ident, channel_id: [])
    result = cli.invoke(app, ["fleet", "agents", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []
    result = cli.invoke(app, ["fleet", "agents"])
    assert result.exit_code == 0, result.output


def test_cli_fleet_agents_surfaces_errors_as_json_on_stderr(monkeypatch) -> None:
    monkeypatch.setattr(fc, "RealCommandRunner", lambda: FakeRunner())

    def _raise(env, runner, community_id):
        raise RuntimeError("no fleet record known here")

    monkeypatch.setattr(fc, "resolve_identity", _raise)
    result = cli.invoke(app, ["fleet", "agents"])
    assert result.exit_code == 1
    assert json.loads(result.output) == {"error": "no fleet record known here"}
