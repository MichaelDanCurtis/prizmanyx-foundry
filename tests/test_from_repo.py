"""Tests for foundry.from_repo: pure sections (https-only validation,
override.tf content, subtree-copy selection, the shared apply path's
children bookkeeping - all with a fake `_clone_repo` seam, no network, no
docker) plus the `@pytest.mark.docker` acceptance test that clones the
foundry's own pushed repo for real and applies `examples/aws-mini` against
a live floci environment, verified host-side via boto3.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import boto3
import pytest

from foundry import state, tf_runner
from foundry.engine import container_name, create_env, remove_env
from foundry.from_repo import (
    FromRepoError,
    InvalidRepoURLError,
    RepoCloneError,
    _clone_repo,
    _resolve_subtree,
    _validate_https_url,
    materialize_from_repo,
    placeholder_spec,
    render_override_tf,
)
from foundry.materialize import _provider_block
from foundry.state import EnvState
from foundry.tf_runner import ApplyResult

# A stand-in for a real ApplyResult, used by the pure tests that stub
# `tf_runner.apply_in_dir` out entirely to isolate the clone/copy/override
# steps above it - a real frozen dataclass, not a lookalike, so a change to
# ApplyResult's own shape surfaces here too.
_STUB_APPLY_RESULT = ApplyResult(ok=True, resources_applied=0, transcript_path=Path("/dev/null"))


# --- pure: https-only URL validation -----------------------------------


@pytest.mark.parametrize(
    "hostile_url",
    [
        "http://github.com/owner/repo.git",
        "git://github.com/owner/repo.git",
        "ssh://git@github.com/owner/repo.git",
        "git@github.com:owner/repo.git",
        "ftp://github.com/owner/repo.git",
        "file:///etc/passwd",
        "not-a-url-at-all",
        "",
    ],
)
def test_validate_https_url_rejects_every_non_https_shape_with_the_prizmanyx_message(hostile_url):
    # Message text mirrors Prizmanyx's own clone-seam rule verbatim
    # (packages/analysis-harness/prizmanyx_analysis_harness/clone.py's
    # CloneRequest.__post_init__, the rule convention_ingest.py's
    # clone_scan_parse_terraform relies on) - not merely the same idea in
    # different words, so an operator who already knows that rule
    # recognizes this one on sight.
    with pytest.raises(InvalidRepoURLError, match=r"^Only https git URLs are supported$"):
        _validate_https_url(hostile_url)


def test_validate_https_url_accepts_a_real_https_url():
    _validate_https_url("https://github.com/owner/repo")  # must not raise


@pytest.mark.parametrize(
    "url_with_credentials",
    [
        "https://user:ghp_secrettoken@github.com/owner/repo.git",
        "https://ghp_secrettoken@github.com/owner/repo.git",
    ],
)
def test_validate_https_url_rejects_credentials_embedded_in_the_url(url_with_credentials):
    # The same second half of Prizmanyx's own clone-seam rule, for the same
    # reason it exists there: this URL becomes `git clone <url>` ARGV, where
    # any embedded token is readable by every process on the machine (`ps`)
    # and lands verbatim in this module's own RepoCloneError text.
    with pytest.raises(InvalidRepoURLError, match="must not embed credentials"):
        _validate_https_url(url_with_credentials)


def test_the_credential_rejection_never_echoes_the_secret_back():
    with pytest.raises(InvalidRepoURLError) as excinfo:
        _validate_https_url("https://user:ghp_secrettoken@github.com/owner/repo.git")

    assert "ghp_secrettoken" not in str(excinfo.value)


def _env_in_isolated_dir(tmp_path, monkeypatch, name: str = "demo", port: int = 15650) -> EnvState:
    monkeypatch.setattr(state, "ENVS_DIR", tmp_path / "envs")
    env = EnvState(
        name=name,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ports={"aws": port},
        spec_path=tmp_path / "unused-spec.yaml",
        status="running",
    )
    state.save_state(env)
    return env


def test_materialize_from_repo_rejects_a_non_https_url_without_cloning_anything(
    tmp_path, monkeypatch
):
    def _must_not_be_called(url, dest):
        raise AssertionError(f"_clone_repo should not be called, got url={url!r} dest={dest!r}")

    monkeypatch.setattr("foundry.from_repo._clone_repo", _must_not_be_called)
    env = _env_in_isolated_dir(tmp_path, monkeypatch)

    with pytest.raises(InvalidRepoURLError, match="Only https git URLs are supported"):
        materialize_from_repo(env, "http://example.com/repo.git")


# --- pure: override.tf content -------------------------------------------


def test_render_override_tf_has_every_endpoint_and_the_forced_region():
    tf_source = render_override_tf(15650)

    assert 'provider "aws" {' in tf_source
    assert 'region                      = "us-east-1"' in tf_source
    for service in ("ec2", "iam", "rds", "s3", "sqs"):
        assert f'{service} = "http://host.docker.internal:15650"' in tf_source


def test_render_override_tf_uses_this_specific_ports_endpoints_not_anothers():
    tf_source = render_override_tf(15601)

    assert "15601" in tf_source
    assert "15600" not in tf_source


def test_render_override_tf_reuses_materializes_own_provider_block_verbatim():
    # The load-bearing "reuse, don't duplicate" proof: this must be the
    # SAME function call, not a lookalike that happens to produce similar
    # text today and silently drifts from it tomorrow.
    assert render_override_tf(15777) == _provider_block(15777) + "\n"


# --- pure: subtree selection copies only the subtree, never the rest -----


def _fake_clone_with_subdir_and_root_file(url, dest):  # noqa: ARG001 - fake never touches url
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "README.md").write_text("repo root file - must never be copied\n")
    subdir = dest / "examples" / "aws-mini"
    subdir.mkdir(parents=True)
    (subdir / "main.tf").write_text('resource "aws_vpc" "main" {}\n')


def _stub_apply_in_dir(monkeypatch) -> list[Path]:
    """Replace tf_runner's apply with a no-op, returning the list the
    workdirs it was called with land in."""
    workdirs: list[Path] = []

    def _fake(env, env_dir):  # noqa: ARG001 - env unused by the stub
        workdirs.append(env_dir)
        return _STUB_APPLY_RESULT

    monkeypatch.setattr("foundry.tf_runner.apply_in_dir", _fake)
    return workdirs


def test_materialize_from_repo_with_a_path_copies_only_that_subtree(tmp_path, monkeypatch):
    monkeypatch.setattr("foundry.from_repo._clone_repo", _fake_clone_with_subdir_and_root_file)
    _stub_apply_in_dir(monkeypatch)
    env = _env_in_isolated_dir(tmp_path, monkeypatch)

    materialize_from_repo(env, "https://example.com/repo.git", path="examples/aws-mini")

    workdir = state.env_dir(env.name)
    assert (workdir / "main.tf").read_text() == 'resource "aws_vpc" "main" {}\n'
    assert not (workdir / "README.md").exists()
    assert not (workdir / "examples").exists()  # only the subtree's CONTENTS land at workdir root


def test_materialize_from_repo_without_a_path_copies_the_whole_clone_but_never_git(
    tmp_path, monkeypatch
):
    def _fake_clone(url, dest):  # noqa: ARG001
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "main.tf").write_text('resource "aws_vpc" "main" {}\n')
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")

    monkeypatch.setattr("foundry.from_repo._clone_repo", _fake_clone)
    _stub_apply_in_dir(monkeypatch)
    env = _env_in_isolated_dir(tmp_path, monkeypatch)

    materialize_from_repo(env, "https://example.com/repo.git")

    workdir = state.env_dir(env.name)
    assert (workdir / "main.tf").read_text() == 'resource "aws_vpc" "main" {}\n'
    assert not (workdir / ".git").exists()


def test_materialize_from_repo_leaves_no_clone_behind_in_the_terraform_workdir(
    tmp_path, monkeypatch
):
    # The clone is transient - the same discipline Prizmanyx's own
    # clone_scan_parse_terraform applies ("Clones are transient; only the
    # proposal persists"). It also keeps the workdir - which is bind-mounted
    # into every `docker run ... terraform` invocation - down to the files
    # terraform is actually meant to read.
    monkeypatch.setattr("foundry.from_repo._clone_repo", _fake_clone_with_subdir_and_root_file)
    workdirs = _stub_apply_in_dir(monkeypatch)
    env = _env_in_isolated_dir(tmp_path, monkeypatch)

    materialize_from_repo(env, "https://example.com/repo.git", path="examples/aws-mini")

    workdir = state.env_dir(env.name)
    assert workdirs == [workdir], "apply must run against the env dir itself"
    survivors = sorted(p.name for p in workdir.iterdir())
    assert survivors == ["main.tf", "override.tf", "state.json"], survivors


def test_materialize_from_repo_removes_the_clone_even_when_the_subtree_is_missing(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("foundry.from_repo._clone_repo", _fake_clone_with_subdir_and_root_file)
    _stub_apply_in_dir(monkeypatch)
    env = _env_in_isolated_dir(tmp_path, monkeypatch)

    with pytest.raises(FromRepoError, match="does not exist"):
        materialize_from_repo(env, "https://example.com/repo.git", path="no-such-subdir")

    workdir = state.env_dir(env.name)
    assert sorted(p.name for p in workdir.iterdir()) == ["state.json"]


def test_materialize_from_repo_writes_override_tf_for_this_envs_own_port(tmp_path, monkeypatch):
    monkeypatch.setattr("foundry.from_repo._clone_repo", _fake_clone_with_subdir_and_root_file)
    _stub_apply_in_dir(monkeypatch)
    env = _env_in_isolated_dir(tmp_path, monkeypatch, port=15699)

    materialize_from_repo(env, "https://example.com/repo.git", path="examples/aws-mini")

    override_content = (state.env_dir(env.name) / "override.tf").read_text()
    assert override_content == render_override_tf(15699)


def test_materialize_from_repos_override_wins_over_a_provider_the_repo_ships_itself(
    tmp_path, monkeypatch
):
    # Terraform's own override semantics do the merging; what this module
    # owns is writing the override at all, ALONGSIDE (never on top of) a
    # repo that already has its own provider config. A repo whose own
    # provider file is literally named override.tf is the one case where
    # foundry's write would silently clobber it - proven here to raise
    # instead.
    def _fake_clone(url, dest):  # noqa: ARG001
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "main.tf").write_text('provider "aws" {\n  region = "eu-west-1"\n}\n')

    monkeypatch.setattr("foundry.from_repo._clone_repo", _fake_clone)
    _stub_apply_in_dir(monkeypatch)
    env = _env_in_isolated_dir(tmp_path, monkeypatch, port=15688)

    materialize_from_repo(env, "https://example.com/repo.git")

    workdir = state.env_dir(env.name)
    assert 'region = "eu-west-1"' in (workdir / "main.tf").read_text(), (
        "the repo's own terraform is copied verbatim, never rewritten"
    )
    assert (workdir / "override.tf").read_text() == render_override_tf(15688)


@pytest.mark.parametrize("colliding_name", ["override.tf", "zzz_override.tf"])
def test_materialize_from_repo_refuses_a_repo_that_ships_its_own_override_file(
    tmp_path, monkeypatch, colliding_name
):
    # Terraform treats `override.tf` AND every `*_override.tf` as override
    # files, applied in filename order with the last one winning. A repo
    # carrying either could clobber (same name) or out-precedence (a later
    # name) foundry's endpoint injection - and the apply would then quietly
    # be pointed somewhere other than this environment's own floci. Refused
    # loudly, up front, rather than discovered in a terraform error.
    def _fake_clone(url, dest):  # noqa: ARG001
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "main.tf").write_text('resource "aws_vpc" "main" {}\n')
        (dest / colliding_name).write_text('provider "aws" {\n  region = "eu-west-1"\n}\n')

    monkeypatch.setattr("foundry.from_repo._clone_repo", _fake_clone)
    _stub_apply_in_dir(monkeypatch)
    env = _env_in_isolated_dir(tmp_path, monkeypatch)

    with pytest.raises(FromRepoError, match=colliding_name):
        materialize_from_repo(env, "https://example.com/repo.git")


def test_a_nested_override_file_is_not_mistaken_for_a_top_level_one(tmp_path, monkeypatch):
    # Terraform only reads `.tf` files in the working directory itself, so
    # an override file in a SUBDIRECTORY of the copied tree cannot affect
    # this apply and must not block it.
    def _fake_clone(url, dest):  # noqa: ARG001
        nested = dest / "modules" / "net"
        nested.mkdir(parents=True)
        (dest / "main.tf").write_text('resource "aws_vpc" "main" {}\n')
        (nested / "override.tf").write_text('provider "aws" {\n  region = "eu-west-1"\n}\n')

    monkeypatch.setattr("foundry.from_repo._clone_repo", _fake_clone)
    _stub_apply_in_dir(monkeypatch)
    env = _env_in_isolated_dir(tmp_path, monkeypatch)

    materialize_from_repo(env, "https://example.com/repo.git")  # must not raise

    assert (state.env_dir(env.name) / "modules" / "net" / "override.tf").is_file()


def test_resolve_subtree_rejects_a_path_that_escapes_the_clone_via_dotdot(tmp_path):
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()

    with pytest.raises(FromRepoError, match="escapes"):
        _resolve_subtree(clone_dir, "../../etc")


def test_resolve_subtree_rejects_an_absolute_path(tmp_path):
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()

    with pytest.raises(FromRepoError, match="escapes"):
        _resolve_subtree(clone_dir, "/etc")


def test_resolve_subtree_rejects_a_path_that_does_not_exist_in_the_clone(tmp_path):
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()

    with pytest.raises(FromRepoError, match="does not exist"):
        _resolve_subtree(clone_dir, "no-such-subdir")


def test_resolve_subtree_rejects_a_path_that_names_a_file_not_a_directory(tmp_path):
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    (clone_dir / "main.tf").write_text("")

    with pytest.raises(FromRepoError, match="not a directory"):
        _resolve_subtree(clone_dir, "main.tf")


def test_resolve_subtree_with_no_path_returns_the_clone_dir_itself(tmp_path):
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()

    assert _resolve_subtree(clone_dir, None) == clone_dir.resolve()


# --- pure: the shared apply path (children recording included) -----------


def _fake_terraform_docker(ps_snapshots: list[set[str]], resources_added: int = 1):
    """A minimal stand-in for the real docker binary, answering `docker ps`
    from a queue of canned snapshots and every terraform invocation with a
    canned successful transcript - just enough to drive
    `tf_runner.apply_in_dir`'s own before/after children bookkeeping,
    mirroring test_apply_docker.py's own `_FakeTerraformDocker` at the scope
    this one test actually needs (no destroy support - from_repo never calls
    it).
    """
    snapshots = list(ps_snapshots)

    def _fake(args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:  # noqa: ARG001
        if args[0] == "ps":
            names = snapshots.pop(0)
            stdout = "".join(f"{name}\n" for name in sorted(names))
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
        if args[0] == "run":
            if "apply" in args:
                stdout = (
                    f"Apply complete! Resources: {resources_added} added, 0 changed, 0 destroyed.\n"
                )
            else:
                stdout = "Terraform has been successfully initialized!\n"
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    return _fake


def test_materialize_from_repo_records_children_via_the_shared_apply_path(tmp_path, monkeypatch):
    # Proves from_repo genuinely reuses tf_runner's own before/after-docker-
    # ps-delta bookkeeping - the identical function `tf_runner.apply` calls
    # after writing its own main.tf (see test_apply_docker.py's own
    # test_apply_unions_new_children_across_repeated_applies_rather_than_
    # replacing) - rather than a parallel reimplementation that happens to
    # look similar today. Monkeypatching `foundry.tf_runner.run_docker` (not
    # some from_repo-local double) is what makes this a genuine reuse proof.
    env = _env_in_isolated_dir(tmp_path, monkeypatch)

    def _fake_clone(url, dest):  # noqa: ARG001
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "main.tf").write_text('resource "aws_vpc" "main" {}\n')

    monkeypatch.setattr("foundry.from_repo._clone_repo", _fake_clone)
    fake_docker = _fake_terraform_docker(
        ps_snapshots=[{f"floci-{env.name}"}, {f"floci-{env.name}", "floci-ec2-i-a"}]
    )
    monkeypatch.setattr("foundry.tf_runner.run_docker", fake_docker)

    result = materialize_from_repo(env, "https://example.com/repo.git")

    assert result.ok
    assert result.resources_applied == 1
    assert state.load_state(env.name).children == ["floci-ec2-i-a"]


def test_materialize_from_repo_never_re_renders_main_tf_from_the_spec(tmp_path, monkeypatch):
    # The whole point of routing through `apply_in_dir` rather than
    # `apply`: `apply`'s first act is `_write_main_tf`, which would render
    # the env's (placeholder) spec straight over the terraform this module
    # just copied out of the cloned repo.
    env = _env_in_isolated_dir(tmp_path, monkeypatch)

    def _fake_clone(url, dest):  # noqa: ARG001
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "main.tf").write_text('resource "aws_sqs_queue" "from_the_repo" {}\n')

    monkeypatch.setattr("foundry.from_repo._clone_repo", _fake_clone)
    monkeypatch.setattr(
        "foundry.tf_runner._write_main_tf",
        lambda *_: pytest.fail("from-repo must never re-render main.tf from the spec"),
    )
    monkeypatch.setattr(
        "foundry.tf_runner.run_docker",
        _fake_terraform_docker(ps_snapshots=[set(), set()]),
    )

    materialize_from_repo(env, "https://example.com/repo.git")

    main_tf = (state.env_dir(env.name) / "main.tf").read_text()
    assert main_tf == 'resource "aws_sqs_queue" "from_the_repo" {}\n'


def test_apply_in_dir_is_the_same_function_tf_runners_own_apply_delegates_to():
    # Structural half of the reuse proof: from_repo holds no apply logic of
    # its own at all - it calls tf_runner's, by name.
    assert callable(tf_runner.apply_in_dir)


# --- pure: _clone_repo's own subprocess seam -----------------------------


def test_clone_repo_raises_repo_clone_error_on_a_failed_git_invocation(tmp_path, monkeypatch):
    def _failing_git(args, *, timeout=None):  # noqa: ARG001
        return subprocess.CompletedProcess(
            args, 128, stdout="", stderr="fatal: repository not found\n"
        )

    monkeypatch.setattr("foundry.from_repo.run_git", _failing_git)

    with pytest.raises(RepoCloneError, match="repository not found"):
        _clone_repo("https://example.com/nope.git", tmp_path / "dest")


def test_clone_repo_asks_git_for_a_shallow_clone(tmp_path, monkeypatch):
    recorded: list[list[str]] = []

    def _recording_git(args, *, timeout=None):  # noqa: ARG001
        recorded.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("foundry.from_repo.run_git", _recording_git)
    dest = tmp_path / "dest"

    _clone_repo("https://example.com/repo.git", dest)

    assert recorded == [["clone", "--depth", "1", "https://example.com/repo.git", str(dest)]]


def test_run_git_forces_git_terminal_prompt_off_regardless_of_the_ambient_environment(monkeypatch):
    # GIT_TERMINAL_PROMPT=0 turns "this repo needs credentials I don't
    # have" into an immediate, readable failure instead of a `foundry
    # create` that hangs on a username prompt nobody can see until the
    # clone timeout fires.
    captured: dict[str, object] = {}

    def _fake_run(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("foundry.from_repo.subprocess.run", _fake_run)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")

    from foundry.from_repo import run_git

    run_git(["clone", "--depth", "1", "https://example.com/r.git", "/tmp/x"])

    assert captured["args"][0] == "git"
    assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"


# --- pure: placeholder_spec ------------------------------------------------


def test_placeholder_spec_has_no_resources_and_a_valid_name_prefix():
    spec = placeholder_spec("demo")

    assert spec.name_prefix == "demo"
    assert spec.vpcs == []
    assert spec.instances == []


def test_placeholder_spec_truncates_an_env_name_too_long_for_name_prefix():
    long_name = "a" * 40  # valid per state.ENV_NAME_PATTERN, too long for name_prefix's 25 chars
    spec = placeholder_spec(long_name)

    assert spec.name_prefix == "a" * 25


# --- docker acceptance: the foundry's own pushed repo, examples/aws-mini --

FOUNDRY_REPO_URL = "https://github.com/MichaelDanCurtis/prizmanyx-foundry"
ENV_NAME_FROM_REPO = "t5-from-repo-test"


def _aws_client(service: str, port: int):
    return boto3.client(
        service,
        endpoint_url=f"http://localhost:{port}",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def _force_cleanup(env_name: str) -> None:
    """Best-effort teardown for a `finally` block: the real `remove_env`
    (parent + recorded children + state dir) when there is still a state
    file to attribute anything to, else a raw `docker rm -f` of the parent
    plus an explicit state-dir delete - mirroring test_apply_docker.py's own
    belt-and-suspenders convention so a failed assertion earlier in the test
    can never leak a container or wedge a later run under the same name.
    """
    if state.state_file(env_name).is_file():
        remove_env(env_name)
    else:
        subprocess.run(
            ["docker", "rm", "-f", container_name(env_name)], capture_output=True, check=False
        )
        state.delete_env_dir(env_name)


@pytest.mark.docker
def test_materialize_from_repo_against_the_foundrys_own_repo_applies_clean():
    try:
        env = create_env(ENV_NAME_FROM_REPO, placeholder_spec(ENV_NAME_FROM_REPO))
        port = env.ports["aws"]

        result = materialize_from_repo(env, FOUNDRY_REPO_URL, path="examples/aws-mini")

        assert result.ok, result.transcript_path.read_text()
        # 1 vpc + 1 subnet + 1 bucket + 1 queue - examples/aws-mini/main.tf's
        # own fixed shape.
        assert result.resources_applied == 4

        # The estate is real: boto3, host-side, against this env's own port.
        s3 = _aws_client("s3", port)
        buckets = [b["Name"] for b in s3.list_buckets()["Buckets"]]
        assert buckets == ["prizmanyx-foundry-aws-mini-example"], buckets

        sqs = _aws_client("sqs", port)
        assert len(sqs.list_queues().get("QueueUrls", [])) == 1

        # The repo's own terraform is what ran - not a spec-rendered main.tf.
        workdir = state.env_dir(ENV_NAME_FROM_REPO)
        assert "from-repo acceptance fixture" in (workdir / "main.tf").read_text()
        assert (workdir / "override.tf").read_text() == render_override_tf(port)
        # The clone itself is transient and must not survive the call.
        assert not any(p.name.startswith(".clone-") for p in workdir.iterdir())
    finally:
        _force_cleanup(ENV_NAME_FROM_REPO)
