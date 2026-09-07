"""The fleet coordination block appended to every agent's team instructions (spec 5.0)."""

from __future__ import annotations

import re

COORDINATION_VERSION = "v1"
BLOCK_START = f"<!-- buzz-fleet:coordination {COORDINATION_VERSION} -->"
BLOCK_END = "<!-- /buzz-fleet:coordination -->"
_ANY_BLOCK = re.compile(r"<!-- buzz-fleet:coordination [^>]*-->.*?<!-- /buzz-fleet:coordination -->\n?", re.DOTALL)

COORDINATION_TEXT = """## Fleet coordination

You are one of several agents run by the same owner on different machines. Work moves between
agents with these commands (all on your PATH). Every command prints JSON; if one fails, say so in
your reply instead of pretending it worked.

**Receiving work.** A message containing `▶ task <id>` is a delegation to you.
1. Run `buzz-fleet task ack --task <id>` first, before any work.
2. If it names a repository and commit, work on exactly that commit: one shared clone per
   repository, one `git worktree` per run. In your working directory, clone the repository once
   (`git clone <repo> repos/<name>` if missing), then
   `git -C repos/<name> fetch && git -C repos/<name> worktree add ../../runs/<run or task id> <commit>`
   and work inside that worktree. Never work in a shared checkout.
3. When you finish, push your commit if you made one, then run
   `buzz-fleet task report --task <id> --status done|blocked|failed --summary - --input-commit <commit you received> [--output-commit <commit you pushed>] <<'EOF' ... EOF`
   The summary must stand alone: the reader is in another session and shares none of your context.
   Use `failed` for defects in the work you were given, `blocked` when you need input.
   Add `--next default` (the default) to let the pipeline continue, `--next <task id>` if you
   delegated onward yourself, or `--next none` to pause the run for the owner.

**Handing work to another agent.** Run
`buzz-fleet task delegate --to "<Agent Name>" --repo <url> --commit <sha> --brief - --wait 45m --thread <root event id from the Thread root: line of your prompt> <<'EOF' ... EOF`
Push first; the command refuses a dirty or unpushed checkout. Prefer this over a bare @-mention:
it records the task, the exact revision, and the deadline, and brings the answer back into this
thread. Add `--run <run id>` when the message that woke you names one.

A delegation may state a pipeline default such as "when done, delegate to @Builder". Follow it
unless you have a concrete reason not to, and say why in your report if you deviate.

A message saying a task was cancelled means stop that work immediately and report nothing for it.
`buzz-fleet task show <id>` prints a task's history if you need it. Keep chat replies short; the
report carries the details.
"""


def _block() -> str:
    return f"{BLOCK_START}\n{COORDINATION_TEXT.rstrip()}\n{BLOCK_END}\n"


def apply_coordination_block(text: str | None) -> str:
    base = _ANY_BLOCK.sub("", text or "").rstrip()
    return f"{base}\n\n{_block()}" if base else _block()


def has_current_block(text: str | None) -> bool:
    return bool(text) and BLOCK_START in (text or "")
