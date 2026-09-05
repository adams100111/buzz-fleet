"""Ensure the `buzz-acp` runtime binary is installed, downloading it automatically if missing.

`buzz-acp` is not part of buzz-fleet — it's a binary from a separate repo
(block/buzz) that every `buzz-agent@<id>.service` unit execs. buzz-fleet
never built, vendored, or installed it, which meant every agent's systemd
unit would crash-loop forever with `status=203/EXEC` on a machine that
never separately installed it — a real incident, not a hypothetical.

Its only standalone (non-Tauri-bundled) distribution is Sprig
(block/sprout): a static musl multicall binary published to GitHub
Releases under the rolling `sprig-latest` tag, with `buzz-acp` as one of
its dispatch names (argv[0]-based, like `busybox`). Installed per-user
under `BUZZ_ACP_DIR` — not `/usr/local/bin` — specifically so this can
happen automatically with no sudo prompt and no manual step, matching
buzz-fleet's own "no root, anywhere" principle for anything that runs
after install.
"""

from __future__ import annotations

import hashlib
import platform
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

BUZZ_ACP_DIR = Path.home() / ".local" / "share" / "buzz-fleet" / "bin"
BUZZ_ACP_PATH = BUZZ_ACP_DIR / "buzz-acp"

_SPRIG_RELEASE_BASE = "https://github.com/block/sprout/releases/download/sprig-latest"

# Rust target triples Sprig publishes prebuilt musl binaries for, keyed by
# the arch strings platform.machine() actually returns on Linux.
_ARCH_TARGETS = {
    "x86_64": "x86_64-unknown-linux-musl",
    "aarch64": "aarch64-unknown-linux-musl",
    "arm64": "aarch64-unknown-linux-musl",
}


def _target_triple() -> str:
    machine = platform.machine()
    if machine not in _ARCH_TARGETS:
        raise RuntimeError(
            f"No buzz-acp (Sprig) build available for this architecture ({machine}) — "
            f"supported: {', '.join(sorted(set(_ARCH_TARGETS.values())))}"
        )
    return _ARCH_TARGETS[machine]


def _download_to_file(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url) as response, dest.open("wb") as f:
        shutil.copyfileobj(response, f)


def _download_text(url: str) -> str:
    with urllib.request.urlopen(url) as response:
        return response.read().decode()


def ensure_buzz_acp_installed() -> bool:
    """Install `buzz-acp` if it's missing or not executable. Returns True if it just installed it.

    The return value lets callers decide whether to restart already-running
    agents — a fresh install only matters to units that were previously
    crash-looping because the binary didn't exist; a no-op call shouldn't
    trigger unnecessary restarts of healthy agents.
    """
    if BUZZ_ACP_PATH.is_file() and BUZZ_ACP_PATH.stat().st_mode & 0o100:
        return False

    target = _target_triple()
    archive_name = f"sprig-{target}.tar.gz"
    archive_url = f"{_SPRIG_RELEASE_BASE}/{archive_name}"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        archive_path = tmp_path / archive_name
        _download_to_file(archive_url, archive_path)

        expected_checksum = _download_text(f"{archive_url}.sha256").split()[0]
        actual_checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        if actual_checksum != expected_checksum:
            raise RuntimeError(
                f"Checksum mismatch downloading buzz-acp (Sprig) from {archive_url} — "
                f"expected {expected_checksum}, got {actual_checksum}"
            )

        with tarfile.open(archive_path) as tar:
            tar.extractall(tmp_path, filter="data")

        BUZZ_ACP_DIR.mkdir(parents=True, exist_ok=True)
        # shutil.copy follows the symlink and copies the real binary's
        # bytes — the destination ends up a plain file literally named
        # "buzz-acp", which is what Sprig's argv[0]-based dispatch needs.
        shutil.copy(tmp_path / "buzz-acp", BUZZ_ACP_PATH)
        BUZZ_ACP_PATH.chmod(0o755)

    return True
