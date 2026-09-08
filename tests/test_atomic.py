"""Durable writes. The fourth step — fsyncing the directory — is the one most
implementations omit, and without it the rename itself can be lost."""

import fcntl
import os
from pathlib import Path

import pytest

from buzz_fleet import atomic


def test_writes_content(tmp_path: Path) -> None:
    target = tmp_path / "a.json"
    atomic.write_secure(target, '{"x":1}')
    assert target.read_text() == '{"x":1}'


def test_creates_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "a.json"
    atomic.write_secure(target, "hi")
    assert target.read_text() == "hi"


def test_file_is_0600_by_default(tmp_path: Path) -> None:
    target = tmp_path / "secret.json"
    atomic.write_secure(target, "s")
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_mode_is_overridable(tmp_path: Path) -> None:
    target = tmp_path / "public.toml"
    atomic.write_secure(target, "s", mode=0o644)
    assert oct(target.stat().st_mode & 0o777) == "0o644"


def test_replaces_existing_content_entirely(tmp_path: Path) -> None:
    """os.replace, not truncate-and-write: a shorter payload must not leave
    a tail of the previous one behind."""
    target = tmp_path / "a.json"
    atomic.write_secure(target, "a-very-long-previous-value")
    atomic.write_secure(target, "short")
    assert target.read_text() == "short"


def test_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    target = tmp_path / "a.json"
    atomic.write_secure(target, "x")
    assert [p.name for p in tmp_path.iterdir()] == ["a.json"]


def test_failed_write_leaves_original_intact_and_no_temp(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "a.json"
    atomic.write_secure(target, "original")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(atomic.os, "replace", boom)
    with pytest.raises(OSError):
        atomic.write_secure(target, "replacement")

    assert target.read_text() == "original"
    assert [p.name for p in tmp_path.iterdir()] == ["a.json"]


def test_locked_is_exclusive(tmp_path: Path) -> None:
    """flock is per open-file-description, so a second open() in this same
    process contends exactly as another process would."""
    lock = tmp_path / "community.lock"
    with atomic.locked(lock):
        fd = os.open(lock, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


def test_lock_is_released_on_exit(tmp_path: Path) -> None:
    lock = tmp_path / "community.lock"
    with atomic.locked(lock):
        pass
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_lock_is_released_when_body_raises(tmp_path: Path) -> None:
    lock = tmp_path / "community.lock"
    with pytest.raises(ValueError), atomic.locked(lock):
        raise ValueError("boom")
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_creates_nested_directories_at_0700(tmp_path: Path) -> None:
    """All created intermediate directories must be 0o700, not affected by umask."""
    target = tmp_path / "a" / "b" / "c" / "secret.json"
    atomic.write_secure(target, "content")

    # Check all intermediate directories are 0o700
    assert oct((tmp_path / "a").stat().st_mode & 0o777) == "0o700"
    assert oct((tmp_path / "a" / "b").stat().st_mode & 0o777) == "0o700"
    assert oct((tmp_path / "a" / "b" / "c").stat().st_mode & 0o777) == "0o700"
    assert target.read_text() == "content"


def test_dir_mode_is_overridable(tmp_path: Path) -> None:
    """dir_mode parameter overrides the default 0o700."""
    target = tmp_path / "x" / "y" / "file.json"
    atomic.write_secure(target, "data", dir_mode=0o755)

    # Check directories were created at 0o755
    assert oct((tmp_path / "x").stat().st_mode & 0o777) == "0o755"
    assert oct((tmp_path / "x" / "y").stat().st_mode & 0o777) == "0o755"
    assert target.read_text() == "data"


def test_preexisting_directory_mode_unchanged(tmp_path: Path) -> None:
    """Pre-existing directories are not modified, only missing ones are created."""
    # Create a directory with specific permissions (0o755)
    existing_dir = tmp_path / "existing"
    existing_dir.mkdir(mode=0o755)
    os.chmod(existing_dir, 0o755)  # Ensure it's set despite umask

    # Write to a file in that directory with different dir_mode
    target = existing_dir / "file.json"
    atomic.write_secure(target, "data", dir_mode=0o700)

    # The existing directory should still be 0o755
    assert oct(existing_dir.stat().st_mode & 0o777) == "0o755"
    assert target.read_text() == "data"


def test_locked_creates_nested_directories_at_0700(tmp_path: Path) -> None:
    """Lock's parent directories must also be 0o700."""
    lock_path = tmp_path / "locks" / "nested" / "community.lock"
    with atomic.locked(lock_path):
        pass

    # Check all directories were created at 0o700
    assert oct((tmp_path / "locks").stat().st_mode & 0o777) == "0o700"
    assert oct((tmp_path / "locks" / "nested").stat().st_mode & 0o777) == "0o700"


def test_locked_dir_mode_is_overridable(tmp_path: Path) -> None:
    """locked's dir_mode parameter is honored."""
    lock_path = tmp_path / "locks" / "open" / "lock"
    with atomic.locked(lock_path, dir_mode=0o755):
        pass

    assert oct((tmp_path / "locks").stat().st_mode & 0o777) == "0o755"
    assert oct((tmp_path / "locks" / "open").stat().st_mode & 0o777) == "0o755"
