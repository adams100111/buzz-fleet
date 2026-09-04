"""The buzz-fleet Typer CLI."""

from __future__ import annotations

import typer

from buzz_fleet import __version__

app = typer.Typer(help="buzz-fleet — manage headless Buzz agents", no_args_is_help=True)


@app.callback(invoke_without_command=True)
def callback(ctx: typer.Context) -> None:
    """Callback for the main app."""
    pass


@app.command()
def version() -> None:
    """Print the installed buzz-fleet version."""
    typer.echo(__version__)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
