"""Detect the exact revision a checkout is at; refuse anything a peer could not fetch."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from buzz_fleet.orchestration.protocol import Artifact

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _git(run: Runner, *args: str) -> subprocess.CompletedProcess[str]:
    # `run` is expected to already execute in `cwd` (bound by whoever constructs it) —
    # no `-C` here, since that would fold the checkout path into the argv the fake
    # runners in tests match on.
    return run(["git", *args])


def detect(cwd: Path, run: Runner) -> Artifact:
    if _git(run, "rev-parse", "--is-inside-work-tree").returncode != 0:
        raise ValueError(f"{cwd} is not a git checkout; pass --repo and --commit explicitly")
    if _git(run, "status", "--porcelain").stdout.strip():
        raise ValueError("checkout is dirty; commit or stash before delegating so the peer sees the same code")
    head = _git(run, "rev-parse", "HEAD").stdout.strip()
    branch = _git(run, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or None
    remote = _git(run, "remote", "get-url", "origin")
    if remote.returncode != 0 or not remote.stdout.strip():
        raise ValueError("no remote named origin; pass --repo explicitly")
    if not _git(run, "branch", "-r", "--contains", "HEAD").stdout.strip():
        raise ValueError(f"HEAD {head[:12]} is not pushed to any remote branch; push first")
    return Artifact(repo=remote.stdout.strip(), commit=head, branch=None if branch == "HEAD" else branch)
