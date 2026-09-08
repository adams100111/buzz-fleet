"""The only way buzz-fleet writes a file, and the only lock it takes.

`os.replace` alone is not durable: the bytes can be synced while the rename
itself is lost on power failure. Syncing the *containing directory* after the
rename is the step that closes that, and it is the one usually left out.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path


def _ensure_dir_mode(path: Path, mode: int) -> None:
    """Create missing ancestor directories with specified mode.

    Path.mkdir(parents=True, mode=...) only applies mode to the leaf directory.
    This function creates each missing directory with the specified mode,
    without changing existing directories.
    """
    # Find which directories need to be created
    to_create = []
    current = path
    while not current.exists() and current != current.parent:
        to_create.append(current)
        current = current.parent

    # Create from root to leaf with the specified mode
    for p in reversed(to_create):
        p.mkdir(mode=mode, exist_ok=True)


def write_secure(
    path: Path, content: str, *, mode: int = 0o600, dir_mode: int = 0o700
) -> None:
    """Write `content` to `path` atomically and durably.

    The temporary file is created in the target's own directory so the rename
    never crosses a filesystem — `os.replace` raises `OSError` if it would.
    """
    _ensure_dir_mode(path.parent, dir_mode)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        try:
            os.write(fd, content.encode())
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


@contextlib.contextmanager
def locked(lock_path: Path, *, dir_mode: int = 0o700) -> Iterator[None]:
    """Hold an exclusive advisory lock for a read-modify-write sequence.

    Atomic writes stop a *torn* file; they do not stop a lost update when two
    processes read, modify and write the same state. The CLI runs standalone
    inside agents' own systemd units, so there is always a second writer.
    """
    _ensure_dir_mode(lock_path.parent, dir_mode)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
