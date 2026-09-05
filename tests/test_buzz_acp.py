from __future__ import annotations

import hashlib
import io
import tarfile
from typing import Self

import pytest

from buzz_fleet import buzz_acp


class _FakeResponse(io.BytesIO):
    """Stands in for the object `urllib.request.urlopen` returns — a
    context manager wrapping a readable stream of bytes."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _build_sprig_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        binary_content = b"#!/bin/sh\necho fake sprig binary\n"
        info = tarfile.TarInfo(name="sprig")
        info.size = len(binary_content)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(binary_content))

        link_info = tarfile.TarInfo(name="buzz-acp")
        link_info.type = tarfile.SYMTYPE
        link_info.linkname = "sprig"
        tar.addfile(link_info)
    return buf.getvalue()


def _fake_urlopen(archive_bytes: bytes, checksum_text: str):
    def urlopen(url: str, *args: object, **kwargs: object) -> _FakeResponse:
        if url.endswith(".sha256"):
            return _FakeResponse(checksum_text.encode())
        return _FakeResponse(archive_bytes)

    return urlopen


@pytest.fixture(autouse=True)
def _isolated_buzz_acp_path(tmp_path, monkeypatch):
    monkeypatch.setattr(buzz_acp, "BUZZ_ACP_DIR", tmp_path / "bin")
    monkeypatch.setattr(buzz_acp, "BUZZ_ACP_PATH", tmp_path / "bin" / "buzz-acp")


def test_target_triple_maps_known_arches(monkeypatch) -> None:
    monkeypatch.setattr(buzz_acp.platform, "machine", lambda: "x86_64")
    assert buzz_acp._target_triple() == "x86_64-unknown-linux-musl"

    monkeypatch.setattr(buzz_acp.platform, "machine", lambda: "aarch64")
    assert buzz_acp._target_triple() == "aarch64-unknown-linux-musl"

    monkeypatch.setattr(buzz_acp.platform, "machine", lambda: "arm64")
    assert buzz_acp._target_triple() == "aarch64-unknown-linux-musl"


def test_target_triple_raises_for_unsupported_arch(monkeypatch) -> None:
    monkeypatch.setattr(buzz_acp.platform, "machine", lambda: "riscv64")

    with pytest.raises(RuntimeError, match="riscv64"):
        buzz_acp._target_triple()


def test_noop_when_already_present_and_executable(monkeypatch) -> None:
    buzz_acp.BUZZ_ACP_DIR.mkdir(parents=True)
    buzz_acp.BUZZ_ACP_PATH.write_bytes(b"already here")
    buzz_acp.BUZZ_ACP_PATH.chmod(0o755)

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("should not attempt a download when already installed")

    monkeypatch.setattr(buzz_acp.urllib.request, "urlopen", explode)

    assert buzz_acp.ensure_buzz_acp_installed() is False


def test_downloads_and_installs_when_missing(monkeypatch) -> None:
    monkeypatch.setattr(buzz_acp.platform, "machine", lambda: "x86_64")
    archive_bytes = _build_sprig_tarball()
    checksum = hashlib.sha256(archive_bytes).hexdigest()
    monkeypatch.setattr(
        buzz_acp.urllib.request,
        "urlopen",
        _fake_urlopen(archive_bytes, f"{checksum}  sprig-x86_64-unknown-linux-musl.tar.gz"),
    )

    result = buzz_acp.ensure_buzz_acp_installed()

    assert result is True
    assert buzz_acp.BUZZ_ACP_PATH.is_file()
    assert buzz_acp.BUZZ_ACP_PATH.read_bytes() == b"#!/bin/sh\necho fake sprig binary\n"
    assert buzz_acp.BUZZ_ACP_PATH.stat().st_mode & 0o100


def test_raises_on_checksum_mismatch(monkeypatch) -> None:
    monkeypatch.setattr(buzz_acp.platform, "machine", lambda: "x86_64")
    archive_bytes = _build_sprig_tarball()
    monkeypatch.setattr(
        buzz_acp.urllib.request,
        "urlopen",
        _fake_urlopen(archive_bytes, "0" * 64 + "  sprig-x86_64-unknown-linux-musl.tar.gz"),
    )

    with pytest.raises(RuntimeError, match="Checksum mismatch"):
        buzz_acp.ensure_buzz_acp_installed()

    assert not buzz_acp.BUZZ_ACP_PATH.exists()
