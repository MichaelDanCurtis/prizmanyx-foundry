"""Typer entry point for the ``foundry`` CLI.

Deliberately thin. Every command here is argument plumbing plus honest
reporting: which source a `create` invocation routes to, what the engine
and the materializers actually returned, and a non-zero exit whenever
something failed. No lifecycle logic, no docker knowledge, no terraform
knowledge lives in this file - those belong to `engine`, `tf_runner` and
`from_repo`, which are where the tests for them point.

Every engine/materializer entry point is imported into this module's own
namespace on purpose: it makes each one a single monkeypatchable name for
`tests/test_cli.py`, so the command surface can be tested exhaustively
without starting a container, cloning a repo, or shelling out to
terraform.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from foundry import __version__
from foundry.engine import (
    EngineError,
    RemovalReport,
    create_env,
    env_status,
    list_envs,
    remove_env,
)
from foundry.envspec import EnvSpec, SpecLoadError, load_spec, random_spec
from foundry.from_repo import FromRepoError, materialize_from_repo, placeholder_spec
from foundry.loop import DEFAULT_WORKER_HOST, run_loop
from foundry.prizmanyx_client import PrizmanyxClient
from foundry.report import write_report
from foundry.state import env_dir
from foundry.tf_runner import ApplyResult, apply

# `foundry run`'s own default - Prizmanyx's own dev launch command
# (`uv run uvicorn prizmanyx_api.main:app --port 8000`, per its
# .claude/launch.json and CI) listens here; overridable with `--prizmanyx`
# for anything else.
_DEFAULT_PRIZMANYX_URL = "http://localhost:8000"

app = typer.Typer(
    help=(
        "prizmanyx-foundry: manufactures Floci-backed AWS test estates and "
        "drives Prizmanyx's pipeline against them from outside."
    ),
)

# Errors this CLI turns into a one-line message and a non-zero exit rather
# than a traceback. ValueError covers `state.InvalidEnvNameError` (a
# ValueError subclass) and `envspec.random_spec`'s own unknown-size raise -
# both are operator input mistakes, not defects. Deliberately NOT a bare
# `Exception`: an unexpected failure should still surface with its full
# traceback rather than being flattened into a tidy-looking message.
_OPERATOR_ERRORS = (EngineError, FromRepoError, SpecLoadError, ValueError)


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


def _fail(message: str) -> typer.Exit:
    """Report an operator-facing failure on stderr and end with exit 1."""
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=1)


def _resolve_source(
    name: str,
    spec_path: Path | None,
    random_size: str | None,
    seed: int | None,
    from_repo_url: str | None,
    subtree_path: str | None,
) -> tuple[EnvSpec, int | None]:
    """Validate the mutually exclusive `create` sources and produce the
    EnvSpec `create_env` will record, plus the seed (generator only).

    A from-repo environment still gets a spec here - `placeholder_spec` -
    because `create_env` requires one; its real resources come from the
    cloned repo instead (see `from_repo.placeholder_spec`).
    """
    chosen = [flag for flag in (spec_path, random_size, from_repo_url) if flag is not None]
    if len(chosen) != 1:
        raise _fail("exactly one of --spec, --random or --from-repo is required")
    if random_size is not None and seed is None:
        raise _fail("--random requires --seed, so the generated estate is reproducible")
    if seed is not None and random_size is None:
        raise _fail("--seed applies only to --random")
    if subtree_path is not None and from_repo_url is None:
        raise _fail("--path applies only to --from-repo")

    if spec_path is not None:
        return load_spec(spec_path), None
    if random_size is not None and seed is not None:
        # `envspec.random_spec` owns both the list of valid size tiers and
        # the message for an unknown one; keeping a second copy of that
        # list here is exactly the drift this defers to avoid.
        return random_spec(random_size, seed), seed  # type: ignore[arg-type]
    return placeholder_spec(name), None


def _report_apply(name: str, result: ApplyResult) -> None:
    """Print an apply outcome and end non-zero if it failed.

    A failed apply deliberately LEAVES the environment standing - its
    container, its state, and its transcript are the evidence needed to
    find out why - so this says so explicitly rather than leaving an
    operator to discover a surprise container later.
    """
    typer.echo(f"transcript: {result.transcript_path}")
    if not result.ok:
        typer.echo(
            f"environment {name!r} is left standing for inspection; "
            f"remove it with `foundry rm {name}`",
            err=True,
        )
        raise _fail(f"terraform apply failed for {name!r}")
    typer.echo(f"applied {result.resources_applied} resource(s)")


@app.command("create")
def create_command(
    name: Annotated[
        str, typer.Argument(help="Environment name (also the floci-<name> container).")
    ],
    spec: Annotated[
        Path | None, typer.Option("--spec", help="Path to an EnvSpec YAML file to materialize.")
    ] = None,
    random_size: Annotated[
        str | None,
        typer.Option(
            "--random",
            metavar="SIZE",
            help="Generate a random estate at this size tier (S, M, L, XL). Requires --seed.",
        ),
    ] = None,
    seed: Annotated[
        int | None,
        typer.Option(
            "--seed",
            help="Seed for --random; the same (size, seed) always yields the same spec.",
        ),
    ] = None,
    from_repo: Annotated[
        str | None,
        typer.Option(
            "--from-repo",
            metavar="URL",
            help="Https git URL whose terraform to apply at this environment's endpoints.",
        ),
    ] = None,
    path: Annotated[
        str | None,
        typer.Option(
            "--path",
            metavar="SUBDIR",
            help="Subdirectory of --from-repo's repository to apply (default: its root).",
        ),
    ] = None,
) -> None:
    """Create an environment and materialize an estate in it."""
    try:
        env_spec, resolved_seed = _resolve_source(name, spec, random_size, seed, from_repo, path)
    except _OPERATOR_ERRORS as exc:
        raise _fail(str(exc)) from exc

    try:
        env = create_env(name, env_spec, seed=resolved_seed)
    except _OPERATOR_ERRORS as exc:
        raise _fail(str(exc)) from exc

    typer.echo(f"created {env.name} (aws endpoint: http://localhost:{env.ports['aws']})")

    try:
        if from_repo is not None:
            result = materialize_from_repo(env, from_repo, path)
        else:
            result = apply(env)
    except _OPERATOR_ERRORS as exc:
        typer.echo(
            f"environment {name!r} is left standing; remove it with `foundry rm {name}`", err=True
        )
        raise _fail(str(exc)) from exc

    _report_apply(name, result)


@app.command("ls")
def ls_command() -> None:
    """List every tracked environment with its LIVE container status.

    Status comes from `env_status` per environment - one docker round trip
    each - not from `list_envs`'s own persisted `.status` field, which is
    only ever whatever was last written to state.json.
    """
    envs = list_envs()
    if not envs:
        typer.echo("no environments")
        return

    typer.echo(f"{'NAME':<24} {'STATUS':<12} {'AWS PORT':<9} {'SEED':<8} CREATED")
    for env in envs:
        try:
            live = env_status(env.name)
        except EngineError as exc:  # the state file vanished between the two calls
            typer.echo(f"{env.name:<24} {'unknown':<12} {'-':<9} {'-':<8} {exc}")
            continue
        seed = "-" if live.seed is None else str(live.seed)
        typer.echo(
            f"{live.name:<24} {live.status:<12} {live.ports['aws']:<9} "
            f"{seed:<8} {live.created_at.isoformat()}"
        )


def _echo_report_field(label: str, names: list[str]) -> None:
    """Print one RemovalReport list, ALWAYS - an empty one is a statement
    ("nothing was ambiguous", "nothing was already gone"), not an absence
    to hide."""
    typer.echo(f"{label}: {', '.join(names) if names else 'none'}")


@app.command("rm")
def rm_command(
    name: Annotated[str, typer.Argument(help="Environment to remove.")],
) -> None:
    """Remove an environment: its container, its attributable children, its state.

    Prints every field of the engine's own RemovalReport, including the
    empty ones - what was spared or found already gone is exactly the part
    an operator needs to see, and is the part a prettier summary would drop.
    """
    try:
        report: RemovalReport = remove_env(name)
    except _OPERATOR_ERRORS as exc:
        raise _fail(str(exc)) from exc

    typer.echo(f"removed {name}")
    _echo_report_field("reaped", report.reaped)
    _echo_report_field("already gone", report.already_gone)
    _echo_report_field("spared", report.spared)
    _echo_report_field("unparsed", report.unparsed)


@app.command("run")
def run_command(
    name: Annotated[
        str, typer.Argument(help="Environment to run the Prizmanyx pipeline loop against.")
    ],
    prizmanyx: Annotated[
        str,
        typer.Option("--prizmanyx", help="Base URL of the running Prizmanyx instance."),
    ] = _DEFAULT_PRIZMANYX_URL,
    worker_host: Annotated[
        str,
        typer.Option(
            "--worker-host",
            help=(
                "Hostname Prizmanyx's extraction worker container uses to reach "
                "this environment's AWS endpoint (extraction runs there, not in "
                "the host Prizmanyx process). Defaults to Docker Desktop's "
                "host-gateway name; override for a Linux host-gateway address or "
                "a worker sharing this environment's own compose network."
            ),
        ),
    ] = DEFAULT_WORKER_HOST,
) -> None:
    """Run the full Prizmanyx pipeline loop against ENV and report PASS/FAIL.

    Operator credentials come from FOUNDRY_PRIZMANYX_USER/
    FOUNDRY_PRIZMANYX_PASSWORD (see prizmanyx_client's module docstring) -
    never a CLI flag, so they never land in shell history. `run_loop` itself
    never raises `LoopStepError`: a failing step becomes part of the written
    report, not an exception here - so the only failure this command handles
    itself is ENV not being a tracked environment at all.
    """
    try:
        env = env_status(name)
    except _OPERATOR_ERRORS as exc:
        raise _fail(str(exc)) from exc

    client = PrizmanyxClient(prizmanyx)
    try:
        report = run_loop(env, client, worker_host=worker_host)
    finally:
        client.close()

    md_path, _json_path = write_report(report, env_dir(name))
    typer.echo(f"verdict: {report.verdict}")
    typer.echo(f"report: {md_path}")

    if report.verdict == "FAIL":
        typer.echo(f"failed step: {report.failed_step} ({report.failure_detail})", err=True)
        raise _fail(f"loop run against {name!r} did not pass")


if __name__ == "__main__":
    app()
