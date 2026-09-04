import pytest

from buzz_fleet.proc import RealCommandRunner


def test_real_command_runner_raises_clear_error_for_missing_binary() -> None:
    """Regression test for Fix 5(b): a missing binary on PATH must surface as a
    clear RuntimeError, not a raw FileNotFoundError traceback.
    """
    runner = RealCommandRunner()
    with pytest.raises(RuntimeError, match="definitely-not-a-real-binary-xyz not found on PATH"):
        runner.run(["definitely-not-a-real-binary-xyz"])
