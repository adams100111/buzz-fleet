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


def test_orphaned_start_marker_does_not_eat_operator_text_on_a_second_apply() -> None:
    # Addition A (final review): `_ANY_BLOCK` requires both markers, so it
    # leaves an orphaned start marker (no matching end) untouched -- the
    # first apply then appends a second, well-formed block right after it.
    # A second apply's `_ANY_BLOCK` regex previously matched greedily from
    # that leftover orphan all the way to the *real* block's own end marker,
    # silently deleting everything in between, operator text included.
    orphan = "# Rules\nDo not touch prod without asking.\n<!-- buzz-fleet:coordination v1 -->\n"
    once = ins.apply_coordination_block(orphan)
    # The orphan itself is gone and exactly one well-formed block exists.
    assert once.count(ins.BLOCK_START) == 1 and once.count(ins.BLOCK_END) == 1
    assert "# Rules" in once and "Do not touch prod without asking." in once

    twice = ins.apply_coordination_block(once)
    # The operator's own text must still be there after a second apply --
    # before this fix it was silently deleted.
    assert "# Rules" in twice and "Do not touch prod without asking." in twice
    assert twice.count(ins.BLOCK_START) == 1 and twice.count(ins.BLOCK_END) == 1


def test_orphaned_start_marker_alone_is_stripped() -> None:
    once = ins.apply_coordination_block("<!-- buzz-fleet:coordination v1 -->\n")
    assert once == ins.apply_coordination_block(None)
