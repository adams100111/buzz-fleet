"""PyInstaller entry point — imports and calls the same main() the
`buzz-fleet` console script (pyproject.toml [project.scripts]) uses, so the
standalone binary and the `uv run buzz-fleet` invocation behave identically.
"""

from __future__ import annotations

from buzz_fleet.cli.app import main

if __name__ == "__main__":
    main()
