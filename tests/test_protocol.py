import json

from buzz_fleet.orchestration import protocol as p

RK = "f" * 64
A, B = "a" * 64, "b" * 64
T, AT = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


def test_build_delegate_content_tags_and_payload() -> None:
    msg = p.build_delegate(
        task_id=T, attempt_id=AT, from_pubkey=A, to_pubkey=B, to_name="Reviewer", retrieval_key=RK,
        brief="Review the CSV export.", deadline=1_800_000_000, acceptance=["tests pass", "no new deps"],
        artifact=p.Artifact(repo="git@github.com:o/r.git", commit="c" * 40, branch="feat"),
        run_id="33333333-3333-4333-8333-333333333333", step=2, parent_task=None, required=True,
        rework_target=A, default_next="Builder", thread_root="d" * 64, thread_parent="e" * 64,
    )
    assert msg.content.startswith("@Reviewer ▶ task 11111111 (run 33333333, step 2)")
    assert "Review the CSV export." in msg.content
    assert "git@github.com:o/r.git @ " + "c" * 40 in msg.content
    assert "- tests pass" in msg.content and "- no new deps" in msg.content
    assert f"buzz-fleet task ack --task {T}" in msg.content
    assert f"buzz-fleet task report --task {T}" in msg.content
    assert "Default when done: delegate to @Builder" in msg.content
    assert msg.mentions == [B, RK]
    assert ("t", "fleet") in msg.tags and ("t", f"fleet:task:{T}") in msg.tags
    assert ("t", "fleet:run:33333333-3333-4333-8333-333333333333") in msg.tags
    payload = json.loads(dict(msg.tags)["fleet"])
    assert payload["v"] == 1 and payload["type"] == "delegate" and payload["task"] == T and payload["attempt"] == AT
    assert payload["from"] == A and payload["to"] == B and payload["deadline"] == 1_800_000_000
    assert payload["artifact"] == {"repo": "git@github.com:o/r.git", "commit": "c" * 40, "branch": "feat", "base": None}
    assert payload["rework_target"] == A and payload["required"] is True and payload["step"] == 2
    assert msg.root == "d" * 64 and msg.parent == "e" * 64


def test_build_delegate_minimal() -> None:
    msg = p.build_delegate(task_id=T, attempt_id=AT, from_pubkey=A, to_pubkey=B, to_name="Reviewer", retrieval_key=RK,
                           brief="x", deadline=1, acceptance=[], artifact=None, run_id=None, step=None, parent_task=None,
                           required=True, rework_target=None, default_next=None, thread_root=None, thread_parent=None)
    assert msg.content.startswith("@Reviewer ▶ task 11111111\n") and "Default when done" not in msg.content
    assert msg.root is None and not any(v.startswith("fleet:run:") for _, v in msg.tags)


def test_build_ack_and_report_and_cancel() -> None:
    ack = p.build_ack(task_id=T, attempt_id=AT, from_pubkey=B, retrieval_key=RK, root="d" * 64, parent="d" * 64)
    assert json.loads(dict(ack.tags)["fleet"]) == {"v": 1, "type": "ack", "task": T, "attempt": AT, "from": B}
    assert ack.mentions == [RK]

    rep = p.build_report(task_id=T, attempt_id=AT, status="failed", summary="Quoting bug.", from_pubkey=B,
                         recipient_pubkey=A, recipient_name="Implementer", retrieval_key=RK, next_task="default",
                         input_commit="c" * 40, output_commit=None, evidence=["pytest: 3 failed"], run_id=None,
                         root="d" * 64, parent="e" * 64)
    assert rep.content.startswith("@Implementer ❌ task 11111111 failed: Quoting bug.")
    assert "pytest: 3 failed" in rep.content
    assert rep.mentions == [A, RK]
    payload = json.loads(dict(rep.tags)["fleet"])
    assert payload["status"] == "failed" and payload["next"] == "default" and payload["input_commit"] == "c" * 40

    can = p.build_cancel_task(task_id=T, reason="superseded", from_pubkey=A, assignee_pubkey=B, retrieval_key=RK,
                              root="d" * 64, parent="d" * 64)
    assert json.loads(dict(can.tags)["fleet"])["type"] == "cancel-task" and can.mentions == [B, RK]


def test_parse_event_full() -> None:
    raw = {"id": "1" * 64, "pubkey": B, "created_at": 1700, "kind": 9, "content": "hi",
           "tags": [["h", "6f1c0000-0000-4000-8000-000000000000"], ["t", "fleet"], ["t", f"fleet:task:{T}"],
                    ["fleet", json.dumps({"v": 1, "type": "ack", "task": T, "attempt": AT, "from": B})],
                    ["p", A], ["p", RK], ["e", "d" * 64, "", "root"], ["e", "e" * 64, "", "reply"]]}
    ev = p.parse_event(raw)
    assert ev.type == "ack" and ev.task_id == T and ev.channel_id == "6f1c0000-0000-4000-8000-000000000000"
    assert ev.mentions == [A, RK] and ev.root == "d" * 64 and ev.parent == "e" * 64


def test_parse_event_rejects_spoofed_from_and_bad_payloads() -> None:
    base = {"id": "2" * 64, "pubkey": B, "created_at": 1, "kind": 9, "content": ""}
    spoof = {**base, "tags": [["fleet", json.dumps({"v": 1, "type": "ack", "task": T, "attempt": AT, "from": A})]]}
    assert p.parse_event(spoof).payload is None
    assert p.parse_event({**base, "tags": [["fleet", "{not json"]]}).payload is None
    assert p.parse_event({**base, "tags": [["fleet", json.dumps({"v": 2, "type": "ack", "from": B})]]}).payload is None
    huge = {**base, "tags": [["fleet", json.dumps({"v": 1, "type": "ack", "from": B, "pad": "x" * 9000})]]}
    assert p.parse_event(huge).payload is None
    plain = {**base, "tags": [["e", "d" * 64, "", "reply"], ["p", A]]}
    ev = p.parse_event(plain)
    assert ev.payload is None and ev.root == "d" * 64 and ev.parent == "d" * 64
