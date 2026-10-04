"""Command-line entry point."""

import typer
from rich.console import Console

from adsight import __version__

app = typer.Typer(no_args_is_help=True, help="adsight: text-to-SQL marketing analytics agent.")
console = Console()


@app.callback()
def _root() -> None:
    """adsight: text-to-SQL marketing analytics agent."""


@app.command()
def version() -> None:
    """Print the adsight version."""
    console.print(f"adsight {__version__}")


def main() -> None:
    app()
