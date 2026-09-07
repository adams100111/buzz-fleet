"""Detect the exact revision a checkout is at; refuse anything a peer could not fetch."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from buzz_fleet.orchestration.protocol import Artifact

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _git(run: Runner, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(cwd), *args])


def _sanitize_remote(url: str) -> str:
    """Strip embedded userinfo (e.g. `x-access-token:<token>@`) from an http(s)
    remote before it can be returned. This URL is broadcast verbatim into a
    durable, replicated fleet-channel event -- readable by every channel member,
    cleanable only by a purge -- so a token that a routine `gh`/CI checkout leaves
    on `origin` must never leave this machine through it. The peer that receives
    the delegate uses its own credentials to fetch; nothing here depends on the
    embedded ones surviving. SSH remotes (`git@host:owner/repo.git`) don't carry
    credentials in this form and are returned unchanged.
    """
    parts = urlsplit(url)
    if parts.scheme in ("http", "https") and "@" in parts.netloc:
        netloc = parts.netloc.rsplit("@", 1)[1]
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return url


def detect(cwd: Path, run: Runner) -> Artifact:
    if _git(run, cwd, "rev-parse", "--is-inside-work-tree").returncode != 0:
        raise ValueError(f"{cwd} is not a git checkout; pass --repo and --commit explicitly")
    if _git(run, cwd, "status", "--porcelain").stdout.strip():
        raise ValueError("checkout is dirty; commit or stash before delegating so the peer sees the same code")
    head = _git(run, cwd, "rev-parse", "HEAD").stdout.strip()
    branch = _git(run, cwd, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or None
    remote = _git(run, cwd, "remote", "get-url", "origin")
    if remote.returncode != 0 or not remote.stdout.strip():
        raise ValueError("no remote named origin; pass --repo explicitly")
    if not _git(run, cwd, "branch", "-r", "--contains", "HEAD").stdout.strip():
        raise ValueError(f"HEAD {head[:12]} is not pushed to any remote branch; push first")
    repo = _sanitize_remote(remote.stdout.strip())
    return Artifact(repo=repo, commit=head, branch=None if branch == "HEAD" else branch)
