from buzz_fleet.orchestration import instructions as ins


def test_apply_to_empty_and_idempotent() -> None:
    once = ins.apply_coordination_block(None)
    assert once.startswith(ins.BLOCK_START) and once.rstrip().endswith(ins.BLOCK_END)
    for needle in ("buzz-fleet task delegate", "buzz-fleet task ack", "buzz-fleet task report", "git worktree", "--commit"):
        assert needle in once
    assert ins.apply_coordination_block(once) == once


def test_apply_keeps_operator_text_and_replaces_old_version() -> None:
    old = "# Rules\n\n<!-- buzz-fleet:coordination v0 -->\nold text\n<!-- /buzz-fleet:coordination -->\n"
    out = ins.apply_coordination_block(old)
    assert out.startswith("# Rules") and "old text" not in out and out.count(ins.BLOCK_START) == 1


def test_has_current_block() -> None:
    assert ins.has_current_block(ins.apply_coordination_block("x")) is True
    assert ins.has_current_block("x") is False and ins.has_current_block(None) is False
