"""The buzz-fleet Typer CLI."""

from __future__ import annotations

import typer

app = typer.Typer(help="buzz-fleet — manage headless Buzz agents", no_args_is_help=True)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
