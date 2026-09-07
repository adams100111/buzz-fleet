import subprocess
from pathlib import Path

import pytest

from buzz_fleet.orchestration.git_artifact import detect


def _git(responses: dict[str, tuple[int, str]], calls: list[list[str]] | None = None):
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append(args)
        key = " ".join(args[3:] if args[1] == "-C" else args[1:])
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
    calls: list[list[str]] = []
    art = detect(Path("/x"), _git(CLEAN, calls))
    assert art is not None
    assert (art.repo, art.commit, art.branch) == ("git@github.com:o/r.git", "c" * 40, "feat")
    # Every git invocation must actually target the requested checkout -- proves
    # `cwd` is wired into the command, not just decorative in the error message.
    assert calls and all(c[1] == "-C" and c[2] == "/x" for c in calls)


def test_detect_strips_credentials_from_https_remote_origin() -> None:
    creds = {**CLEAN, "remote get-url origin": (0, "https://x-access-token:ghs_secrettoken@github.com/o/r.git\n")}
    art = detect(Path("/x"), _git(creds))
    assert art is not None
    assert art.repo == "https://github.com/o/r.git"
    assert "ghs_secrettoken" not in art.repo and "x-access-token" not in art.repo


def test_detect_leaves_ssh_remote_unchanged() -> None:
    art = detect(Path("/x"), _git(CLEAN))
    assert art is not None
    assert art.repo == "git@github.com:o/r.git"


@pytest.mark.parametrize("key,value,match", [
    ("status --porcelain", (0, " M file.py\n"), "dirty"),
    ("branch -r --contains HEAD", (0, ""), "not pushed"),
    ("remote get-url origin", (1, ""), "no remote"),
])
def test_detect_refusals(key, value, match) -> None:
    # A dirty, unpushed, or remote-less checkout means the caller plainly
    # meant to attach a revision a peer could not fetch -- these stay hard
    # refusals, never silently downgraded to "no artifact".
    with pytest.raises(ValueError, match=match):
        detect(Path("/x"), _git({**CLEAN, key: value}))


def test_detect_returns_none_when_cwd_is_not_a_git_checkout_at_all() -> None:
    # Finding 3 (final review): an agent unit's WorkingDirectory
    # (WORK_DIR/%i) is a plain directory, not a checkout -- "not a git
    # checkout" is what every non-code delegation issued by a live agent
    # looks like, and spec 5.3 / the README both treat the artifact as
    # optional. This must be "no artifact", not a refusal, unlike the dirty/
    # unpushed/no-remote cases above (which mean the caller plainly did mean
    # to attach a revision a peer could not fetch).
    assert detect(Path("/x"), _git({**CLEAN, "rev-parse --is-inside-work-tree": (1, "")})) is None
