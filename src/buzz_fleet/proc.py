"""Process-execution seam so higher-level code is testable without shelling out for real."""

from __future__ import annotations

import subprocess
from typing import Protocol


class CommandRunner(Protocol):
    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]: ...


class RealCommandRunner:
    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(args, capture_output=True, text=True, check=False)
        except FileNotFoundError as e:
            raise RuntimeError(f"{args[0]} not found on PATH — is it installed?") from e
