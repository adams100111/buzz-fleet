import subprocess
from pathlib import Path

import pytest

from buzz_fleet.orchestration.git_artifact import detect


def _git(responses: dict[str, tuple[int, str]]):
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        key = " ".join(args[1:])
        code, out = responses.get(key, (1, ""))
        return subprocess.CompletedProcess(args, code, stdout=out, stderr="")
    return run


CLEAN = {
    "rev-parse --is-inside-work-tree": (0, "true\n"),
    "status --porcelain": (0, ""),
    "rev-parse HEAD": (0, "c" * 40 + "\n"),
    "rev-parse --abbrev-ref HEAD": (0, "feat\n"),
    "remote get-url origin": (0, "git@github.com:o/r.git\n"),
    "branch -r --contains HEAD": (0, "  origin/feat\n"),
}


def test_detect_clean_pushed_checkout() -> None:
    art = detect(Path("/x"), _git(CLEAN))
    assert (art.repo, art.commit, art.branch) == ("git@github.com:o/r.git", "c" * 40, "feat")


@pytest.mark.parametrize("key,value,match", [
    ("rev-parse --is-inside-work-tree", (1, ""), "not a git"),
    ("status --porcelain", (0, " M file.py\n"), "dirty"),
    ("branch -r --contains HEAD", (0, ""), "not pushed"),
    ("remote get-url origin", (1, ""), "no remote"),
])
def test_detect_refusals(key, value, match) -> None:
    with pytest.raises(ValueError, match=match):
        detect(Path("/x"), _git({**CLEAN, key: value}))
