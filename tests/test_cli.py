"""Tests for foundry.cli: the thin command surface over the engine, the
materializers and the reaper.

Every test here is pure - `create_env`, `apply`, `materialize_from_repo`,
`list_envs`, `env_status` and `remove_env` are monkeypatched in cli's own
namespace, so nothing starts a container, clones a repo or shells out to
terraform. What these pin is exactly what the CLI itself owns: which
source a `create` invocation routes to, which combinations it refuses
before touching anything, what exit code a failure produces, and whether
`ls`/`rm` report the fields the engine actually returned (rather than a
prettier subset of them).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from foundry import cli
from foundry.engine import EnvNotFoundError, EnvStartError, RemovalReport
from foundry.envspec import EnvSpec
from foundry.loop import Decision, LoopReport, StepResult
from foundry.state import EnvState
from foundry.tf_runner import ApplyResult

runner = CliRunner()


def _env(name: str = "demo", port: int = 15600, seed: int | None = None) -> EnvState:
    return EnvState(
        name=name,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ports={"aws": port},
        spec_path=Path(f"/envs/{name}/spec.yaml"),
        seed=seed,
        status="running",
    )


def _ok_apply(tmp_path: Path, resources: int = 4) -> ApplyResult:
    return ApplyResult(
        ok=True, resources_applied=resources, transcript_path=tmp_path / "terraform-apply.log"
    )


class _RecordingCreateEnv:
    """Stands in for `engine.create_env`, recording what the CLI asked for."""

    def __init__(self, result: EnvState | None = None):
        self.calls: list[tuple[str, EnvSpec, int | None]] = []
        self._result = result or _env()

    def __call__(self, name: str, spec: EnvSpec, *, seed: int | None = None) -> EnvState:
        self.calls.append((name, spec, seed))
        return self._result


# --- create: source selection is exclusive and required --------------------


def test_create_without_any_source_refuses_and_exits_nonzero(monkeypatch):
    create_env = _RecordingCreateEnv()
    monkeypatch.setattr(cli, "create_env", create_env)

    result = runner.invoke(cli.app, ["create", "demo"])

    assert result.exit_code != 0
    assert "exactly one of --spec, --random or --from-repo" in result.output
    assert create_env.calls == [], "nothing may be created before the source is settled"


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--spec", "s.yaml", "--random", "S", "--seed", "1"],
        ["--spec", "s.yaml", "--from-repo", "https://example.com/r.git"],
        ["--random", "S", "--seed", "1", "--from-repo", "https://example.com/r.git"],
    ],
)
def test_create_with_two_sources_refuses_and_exits_nonzero(monkeypatch, extra_args):
    create_env = _RecordingCreateEnv()
    monkeypatch.setattr(cli, "create_env", create_env)

    result = runner.invoke(cli.app, ["create", "demo", *extra_args])

    assert result.exit_code != 0
    assert "exactly one of --spec, --random or --from-repo" in result.output
    assert create_env.calls == []


def test_create_random_without_a_seed_refuses(monkeypatch):
    create_env = _RecordingCreateEnv()
    monkeypatch.setattr(cli, "create_env", create_env)

    result = runner.invoke(cli.app, ["create", "demo", "--random", "S"])

    assert result.exit_code != 0
    assert "--seed" in result.output
    assert create_env.calls == []


def test_create_path_without_from_repo_refuses(monkeypatch):
    create_env = _RecordingCreateEnv()
    monkeypatch.setattr(cli, "create_env", create_env)

    result = runner.invoke(
        cli.app, ["create", "demo", "--spec", "s.yaml", "--path", "examples/aws-mini"]
    )

    assert result.exit_code != 0
    assert "--path" in result.output
    assert create_env.calls == []


def test_create_rejects_an_unknown_random_size_with_the_generators_own_message(monkeypatch):
    create_env = _RecordingCreateEnv()
    monkeypatch.setattr(cli, "create_env", create_env)

    result = runner.invoke(cli.app, ["create", "demo", "--random", "XXL", "--seed", "1"])

    assert result.exit_code != 0
    # envspec.random_spec owns the list of valid tiers; the CLI surfaces its
    # message rather than keeping a second copy of that list.
    assert "unknown size 'XXL'" in result.output
    assert create_env.calls == []


# --- create: each source routes to its own materializer --------------------


def test_create_from_random_seeds_the_generator_and_records_the_seed(monkeypatch, tmp_path):
    create_env = _RecordingCreateEnv()
    monkeypatch.setattr(cli, "create_env", create_env)
    monkeypatch.setattr(cli, "apply", lambda *_: _ok_apply(tmp_path, resources=10))
    monkeypatch.setattr(
        cli,
        "materialize_from_repo",
        lambda *_: pytest.fail("--random must not take the from-repo path"),
    )

    result = runner.invoke(cli.app, ["create", "demo", "--random", "S", "--seed", "42"])

    assert result.exit_code == 0, result.output
    (name, spec, seed) = create_env.calls[0]
    assert name == "demo"
    assert seed == 42, "the seed must reach EnvState so the estate stays reproducible"
    # random_spec("S", 42)'s own deterministic prefix - proof the CLI called
    # the generator rather than inventing a spec of its own.
    assert spec.name_prefix == "foundry-s-42"
    assert "10" in result.output


def test_create_from_spec_loads_that_file_and_applies_it(monkeypatch, tmp_path):
    spec_path = tmp_path / "small.yaml"
    spec_path.write_text(yaml.safe_dump(EnvSpec(name_prefix="from-file").model_dump()))
    created = _env("demo")
    create_env = _RecordingCreateEnv(result=created)
    monkeypatch.setattr(cli, "create_env", create_env)
    applied: list[EnvState] = []

    def _fake_apply(env):
        applied.append(env)
        return _ok_apply(tmp_path)

    monkeypatch.setattr(cli, "apply", _fake_apply)

    result = runner.invoke(cli.app, ["create", "demo", "--spec", str(spec_path)])

    assert result.exit_code == 0, result.output
    assert create_env.calls[0][1].name_prefix == "from-file"
    assert create_env.calls[0][2] is None, "a hand-written spec has no seed"
    assert applied == [created], "apply() must run against the environment create_env returned"


def test_create_from_a_missing_spec_file_fails_without_creating_anything(monkeypatch, tmp_path):
    create_env = _RecordingCreateEnv()
    monkeypatch.setattr(cli, "create_env", create_env)

    result = runner.invoke(cli.app, ["create", "demo", "--spec", str(tmp_path / "nope.yaml")])

    assert result.exit_code != 0
    assert "nope.yaml" in result.output
    assert create_env.calls == [], "a spec that cannot be loaded must not start a container"


def test_create_from_repo_passes_the_url_and_path_through_to_the_materializer(
    monkeypatch, tmp_path
):
    create_env = _RecordingCreateEnv()
    monkeypatch.setattr(cli, "create_env", create_env)
    monkeypatch.setattr(cli, "apply", lambda *_: pytest.fail("--from-repo must not run apply()"))
    calls: list[tuple[EnvState, str, str | None]] = []

    def _fake_materialize(env, url, path=None):
        calls.append((env, url, path))
        return _ok_apply(tmp_path)

    monkeypatch.setattr(cli, "materialize_from_repo", _fake_materialize)

    result = runner.invoke(
        cli.app,
        [
            "create",
            "demo",
            "--from-repo",
            "https://github.com/owner/repo",
            "--path",
            "examples/aws-mini",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    _, url, path = calls[0]
    assert url == "https://github.com/owner/repo"
    assert path == "examples/aws-mini"
    # A from-repo environment still gets a spec (create_env needs one), but
    # never a seed - it did not come from the generator.
    assert create_env.calls[0][2] is None


def test_create_from_repo_without_a_path_passes_none(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "create_env", _RecordingCreateEnv())
    calls: list[str | None] = []
    monkeypatch.setattr(
        cli,
        "materialize_from_repo",
        lambda env, url, path=None: calls.append(path) or _ok_apply(tmp_path),  # noqa: ARG005
    )

    result = runner.invoke(cli.app, ["create", "demo", "--from-repo", "https://example.com/r.git"])

    assert result.exit_code == 0, result.output
    assert calls == [None]


# --- create: honest failures ------------------------------------------------


def test_create_reports_a_failed_apply_as_a_nonzero_exit_and_names_the_transcript(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(cli, "create_env", _RecordingCreateEnv())
    transcript = tmp_path / "terraform-apply.log"
    monkeypatch.setattr(
        cli,
        "apply",
        lambda env: ApplyResult(ok=False, resources_applied=0, transcript_path=transcript),  # noqa: ARG005
    )

    result = runner.invoke(cli.app, ["create", "demo", "--random", "S", "--seed", "1"])

    assert result.exit_code != 0
    assert str(transcript) in result.output
    # The environment is deliberately LEFT standing for inspection; the CLI
    # must say so rather than silently leaving a surprise behind.
    assert "foundry rm demo" in result.output


def test_create_surfaces_an_engine_failure_as_a_message_not_a_traceback(monkeypatch):
    def _boom(name, spec, *, seed=None):  # noqa: ARG001
        raise EnvStartError("container 'floci-demo' did not become healthy within 60.0s")

    monkeypatch.setattr(cli, "create_env", _boom)

    result = runner.invoke(cli.app, ["create", "demo", "--random", "S", "--seed", "1"])

    assert result.exit_code != 0
    assert "did not become healthy" in result.output
    assert "Traceback" not in result.output


def test_create_surfaces_a_non_https_from_repo_url_with_the_prizmanyx_message(monkeypatch):
    # No monkeypatching of materialize_from_repo here: the real https-only
    # rule must be what refuses this, end to end through the CLI.
    monkeypatch.setattr(cli, "create_env", _RecordingCreateEnv())

    result = runner.invoke(
        cli.app, ["create", "demo", "--from-repo", "http://github.com/owner/repo"]
    )

    assert result.exit_code != 0
    assert "Only https git URLs are supported" in result.output


# --- ls ---------------------------------------------------------------------


def test_ls_reports_live_status_per_environment_not_the_persisted_field(monkeypatch):
    # engine.list_envs()'s own `.status` is whatever was last persisted;
    # env_status() is the one that asks docker. `ls` must show the latter.
    monkeypatch.setattr(
        cli, "list_envs", lambda: [_env("alpha", 15600), _env("beta", 15601, seed=7)]
    )
    live = {
        "alpha": _env("alpha", 15600).model_copy(update={"status": "healthy"}),
        "beta": _env("beta", 15601, seed=7).model_copy(update={"status": "not_found"}),
    }
    monkeypatch.setattr(cli, "env_status", lambda name: live[name])

    result = runner.invoke(cli.app, ["ls"])

    assert result.exit_code == 0, result.output
    assert "alpha" in result.output
    assert "healthy" in result.output
    assert "beta" in result.output
    assert "not_found" in result.output
    assert "15601" in result.output
    assert "running" not in result.output, "the persisted status must not be what ls shows"


def test_ls_with_no_environments_says_so(monkeypatch):
    monkeypatch.setattr(cli, "list_envs", lambda: [])

    result = runner.invoke(cli.app, ["ls"])

    assert result.exit_code == 0
    assert "no environments" in result.output.lower()


# --- rm ---------------------------------------------------------------------


def test_rm_prints_every_removal_report_field_including_the_empty_ones(monkeypatch):
    monkeypatch.setattr(
        cli,
        "remove_env",
        lambda name: RemovalReport(  # noqa: ARG005
            reaped=["floci-ec2-i-a"],
            spared=["floci-rds-db-b"],
            unparsed=["floci-weird-row"],
            already_gone=["floci-ec2-i-c"],
        ),
    )

    result = runner.invoke(cli.app, ["rm", "demo"])

    assert result.exit_code == 0, result.output
    for name in ("floci-ec2-i-a", "floci-rds-db-b", "floci-weird-row", "floci-ec2-i-c"):
        assert name in result.output
    for label in ("reaped", "spared", "unparsed", "already gone"):
        assert label in result.output.lower()


def test_rm_says_nothing_was_spared_rather_than_omitting_the_line(monkeypatch):
    # "Visible truth, not silence": an empty `spared` is a statement (the
    # reaper found nothing ambiguous), not an absence to hide.
    monkeypatch.setattr(
        cli,
        "remove_env",
        lambda name: RemovalReport(reaped=[], spared=[], unparsed=[], already_gone=[]),  # noqa: ARG005
    )

    result = runner.invoke(cli.app, ["rm", "demo"])

    assert result.exit_code == 0, result.output
    assert "spared" in result.output.lower()
    assert "none" in result.output.lower()


def test_rm_of_an_unknown_environment_fails_with_a_message(monkeypatch):
    def _boom(name):
        raise EnvNotFoundError(f"no tracked environment named {name!r}")

    monkeypatch.setattr(cli, "remove_env", _boom)

    result = runner.invoke(cli.app, ["rm", "ghost"])

    assert result.exit_code != 0
    assert "no tracked environment named 'ghost'" in result.output
    assert "Traceback" not in result.output


# --- run ----------------------------------------------------------------
# `run_loop` and `write_report` are monkeypatched throughout - what this
# file pins is the CLI's OWN job (resolve the environment, hand it and a
# real client to run_loop, print the verdict, exit non-zero on FAIL), not
# the loop's internal pipeline walk, which is tests/test_loop.py's job.


def _now():
    return datetime(2026, 1, 1, tzinfo=UTC)


def _pass_report(env_name: str = "demo") -> LoopReport:
    return LoopReport(
        env_name=env_name,
        verdict="PASS",
        steps=[StepResult(name="login", status="PASSED", duration_seconds=0.01)],
        decisions=[Decision(step="accept_catalog_defaults", choice="accept-defaults")],
        program_id=7,
        graph_run_id=55,
        unit_ids=[1],
        translation_ids=[101],
        artifact_paths=["/envs/demo/export.csv"],
        started_at=_now(),
        finished_at=_now(),
    )


def _fail_report(env_name: str = "demo") -> LoopReport:
    return LoopReport(
        env_name=env_name,
        verdict="FAIL",
        failed_step="translate",
        failure_detail="migration unit is not approved",
        steps=[
            StepResult(
                name="translate",
                status="FAILED",
                duration_seconds=0.01,
                detail="migration unit is not approved",
            )
        ],
        decisions=[],
        started_at=_now(),
        finished_at=_now(),
    )


def test_run_reports_pass_and_exits_zero(monkeypatch):
    monkeypatch.setattr(cli, "env_status", lambda name: _env(name))
    recorded_calls: list[tuple[object, object]] = []

    def _fake_run_loop(env, client):
        recorded_calls.append((env, client))
        return _pass_report(env.name)

    monkeypatch.setattr(cli, "run_loop", _fake_run_loop)
    written: dict[str, object] = {}

    def _fake_write_report(report, directory):
        written["report"] = report
        written["directory"] = directory
        return directory / "report.md", directory / "report.json"

    monkeypatch.setattr(cli, "write_report", _fake_write_report)

    result = runner.invoke(cli.app, ["run", "demo"])

    assert result.exit_code == 0, result.output
    assert "PASS" in result.output
    assert len(recorded_calls) == 1
    env_arg, client_arg = recorded_calls[0]
    assert env_arg.name == "demo"
    assert isinstance(client_arg, cli.PrizmanyxClient)
    assert written["report"].verdict == "PASS"


def test_run_defaults_to_localhost_8000_and_honors_the_prizmanyx_flag(monkeypatch):
    monkeypatch.setattr(cli, "env_status", lambda name: _env(name))
    monkeypatch.setattr(cli, "write_report", lambda _report, directory: (directory, directory))
    captured_urls: list[str] = []

    class _RecordingClient(cli.PrizmanyxClient):
        def __init__(self, base_url, *args, **kwargs):
            captured_urls.append(base_url)
            super().__init__(base_url, *args, **kwargs)

    monkeypatch.setattr(cli, "PrizmanyxClient", _RecordingClient)
    monkeypatch.setattr(cli, "run_loop", lambda env, _client: _pass_report(env.name))

    runner.invoke(cli.app, ["run", "demo"])
    assert captured_urls == ["http://localhost:8000"]

    runner.invoke(cli.app, ["run", "demo", "--prizmanyx", "http://example.test:9000"])
    assert captured_urls[-1] == "http://example.test:9000"


def test_run_reports_fail_and_exits_nonzero_naming_the_failed_step(monkeypatch):
    monkeypatch.setattr(cli, "env_status", lambda name: _env(name))
    monkeypatch.setattr(cli, "run_loop", lambda env, _client: _fail_report(env.name))
    monkeypatch.setattr(
        cli, "write_report", lambda _report, directory: (directory / "report.md", directory)
    )

    result = runner.invoke(cli.app, ["run", "demo"])

    assert result.exit_code != 0
    assert "FAIL" in result.output
    assert "translate" in result.output
    assert "migration unit is not approved" in result.output


def test_run_of_an_unknown_environment_fails_with_a_message_and_never_calls_run_loop(monkeypatch):
    def _boom(name):
        raise EnvNotFoundError(f"no tracked environment named {name!r}")

    monkeypatch.setattr(cli, "env_status", _boom)
    monkeypatch.setattr(
        cli, "run_loop", lambda *_: pytest.fail("run_loop must not be called for an unknown env")
    )

    result = runner.invoke(cli.app, ["run", "ghost"])

    assert result.exit_code != 0
    assert "no tracked environment named 'ghost'" in result.output
    assert "Traceback" not in result.output


def test_run_closes_the_client_even_when_run_loop_raises_unexpectedly(monkeypatch):
    # run_loop's own contract is "never raises LoopStepError", but a genuine
    # bug raising anything else must not leak the client's connection pool.
    monkeypatch.setattr(cli, "env_status", lambda name: _env(name))
    closed = []

    class _TrackingClient(cli.PrizmanyxClient):
        def close(self):
            closed.append(True)
            super().close()

    monkeypatch.setattr(cli, "PrizmanyxClient", _TrackingClient)

    def _boom(_env, _client):
        raise RuntimeError("unexpected bug")

    monkeypatch.setattr(cli, "run_loop", _boom)

    with pytest.raises(RuntimeError, match="unexpected bug"):
        runner.invoke(cli.app, ["run", "demo"], catch_exceptions=False)

    assert closed == [True]


# --- the surface itself -----------------------------------------------------


def test_version_still_prints_the_package_version():
    result = runner.invoke(cli.app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == cli.__version__


@pytest.mark.parametrize("command", ["create", "ls", "rm", "run"])
def test_every_command_is_registered_on_the_app(command):
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert command in result.output
