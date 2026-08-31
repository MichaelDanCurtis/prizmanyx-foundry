"""Typer entry point for the ``foundry`` CLI.

This module only wires the command surface. Slice 1+ commands (``create``,
``ls``, ``inspect``, ``run``, ``rm``) land in later tasks; this file's job
right now is to prove the console-script entry point works end to end.
"""

from __future__ import annotations

import typer

from foundry import __version__

app = typer.Typer(
    help=(
        "prizmanyx-foundry: manufactures Floci-backed AWS test estates and "
        "drives Prizmanyx's pipeline against them from outside."
    ),
)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Standalone estate-manufacturing and pipeline-loop tool for Prizmanyx."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


@app.command("version")
def version_command() -> None:
    """Print the installed prizmanyx-foundry version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
