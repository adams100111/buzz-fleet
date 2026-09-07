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


def _delegate_event(task=T1, attempt=AT1, requester=A, assignee=B, run=None, parent=None) -> dict:
    payload = {"v": 1, "type": "delegate", "task": task, "attempt": attempt, "run": run, "step": None, "parent_task": parent,
               "required": True, "from": requester, "to": assignee, "deadline": 1000, "rework_target": None,
               "artifact": {"repo": "git@x:o/r.git", "commit": "c" * 40, "branch": None, "base": None}, "acceptance": []}
    return {"id": task[:8] * 8, "pubkey": requester, "created_at": 100, "kind": 9, "content": "brief",
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
