"""Tests for foundry.tf_runner: a pure section (fake docker, no daemon, no
terraform binary) proving `apply()`'s own children-bookkeeping, plus the
`@pytest.mark.docker` acceptance tests that run real `terraform`
apply/destroy against a real floci environment, verified host-side via
boto3.

Two docker-marked scenarios, because they are mutually exclusive within
one environment's
lifecycle (empirically confirmed while building this module: a clean
`terraform destroy` makes floci tear down its own real-engine child
containers itself, the same way deleting a real RDS instance or
terminating a real EC2 instance would - there is nothing left over for
`remove_env` to reap afterward):

1. `test_apply_then_destroy_round_trips_resources_via_boto3` - the brief's
   own literal flow: create an S-shaped env -> apply -> boto3 (host side)
   sees the bucket/instance/db -> the RDS child container exists while
   applied -> destroy -> boto3 sees them gone.
2. `test_remove_env_reaps_a_still_running_recorded_child` - the scenario
   ownership-by-recording actually exists for: an environment whose
   terraform was applied but never (or not yet) cleanly destroyed - e.g.
   a plain `foundry rm` without `--applied`, an abandoned loop run. This
   is the first REAL proof that `engine.remove_env`'s recorded-children
   primary path removes a still-running container, not just a canned
   table (see test_engine_docker.py's own dispatch tests for that half).
   Uses an EC2-only spec (no RDS) to keep the docker-marked suite's total
   runtime reasonable - the real-engine RDS child path is already proven
   live by scenario 1 above.

Both are skipped cleanly (conftest.py) when no docker daemon is reachable.
Each creates and fully tears down its own environment in a `finally`,
mirroring test_engine_docker.py's own belt-and-suspenders cleanup so a
failed run never wedges a later one.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime

import boto3
import pytest
import yaml

from foundry import state
from foundry.engine import container_name, create_env, remove_env
from foundry.envspec import (
    BucketSpec,
    DatabaseSpec,
    EnvSpec,
    InstanceSpec,
    SubnetSpec,
    VpcSpec,
)
from foundry.state import EnvState
from foundry.tf_runner import apply, destroy

# --- pure: apply()'s own children bookkeeping (fake docker, no daemon) ------


class _FakeTerraformDocker:
    """Answers `ps --filter name=floci-` (no `-a` - matches
    `tf_runner._running_floci_container_names`) from a queue of canned
    snapshots consumed one per call, and every `docker run ... terraform`
    invocation with a canned successful transcript - enough to drive
    `apply()`'s own before/after-delta bookkeeping with no real docker or
    terraform process involved at all.
    """

    def __init__(self, ps_snapshots: list[set[str]], resources_added: int = 1):
        self._ps_snapshots = list(ps_snapshots)
        self._resources_added = resources_added
        self.calls: list[list[str]] = []

    def __call__(
        self,
        args: list[str],
        *,
        timeout: float | None = None,  # noqa: ARG002 - accepted because _run_terraform passes it
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[0] == "ps":
            names = self._ps_snapshots.pop(0)
            stdout = "".join(f"{name}\n" for name in sorted(names))
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
        if args[0] == "run":
            if "apply" in args:
                stdout = (
                    f"Apply complete! Resources: {self._resources_added} added, "
                    "0 changed, 0 destroyed.\n"
                )
            elif "destroy" in args:
                stdout = "Destroy complete! Resources: 1 destroyed.\n"
            else:
                stdout = "Terraform has been successfully initialized!\n"
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _seed_env_with_a_real_spec_file(tmp_path, monkeypatch, name: str = "demo") -> EnvState:
    # apply() calls envspec.load_spec(env.spec_path) for real (pure file
    # I/O, no docker needed) - so this needs an actual spec.yaml on disk,
    # not just an in-memory EnvSpec.
    monkeypatch.setattr(state, "ENVS_DIR", tmp_path / "envs")
    directory = state.ensure_env_dir(name)
    spec_path = directory / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(EnvSpec(name_prefix="demo-prefix").model_dump()))
    env = EnvState(
        name=name,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ports={"aws": 15650},
        spec_path=spec_path,
        status="running",
    )
    state.save_state(env)
    return env


def test_apply_unions_new_children_across_repeated_applies_rather_than_replacing(
    tmp_path, monkeypatch
):
    # Finding 3: a second apply() on the same environment must not lose
    # track of the first apply's own children - apply()'s own docstring
    # promises a union, not an overwrite. Driven entirely through a fake
    # `run_docker` and canned `docker ps` snapshots, so this pins the
    # bookkeeping logic itself, independent of whether any particular real
    # terraform apply happens to spawn anything.
    env = _seed_env_with_a_real_spec_file(tmp_path, monkeypatch)
    fake_docker = _FakeTerraformDocker(
        ps_snapshots=[
            {"floci-demo"},  # before 1st apply: just the parent
            {"floci-demo", "floci-ec2-i-a"},  # after 1st apply: ec2 child spawned
            {"floci-demo", "floci-ec2-i-a"},  # before 2nd apply: unchanged
            {"floci-demo", "floci-ec2-i-a", "floci-ec2-i-b"},  # after 2nd: another spawned
        ]
    )
    monkeypatch.setattr("foundry.tf_runner.run_docker", fake_docker)

    first_result = apply(env)
    assert first_result.ok
    env_after_first = state.load_state(env.name)
    assert env_after_first.children == ["floci-ec2-i-a"]

    second_result = apply(env_after_first)
    assert second_result.ok
    env_after_second = state.load_state(env.name)
    assert env_after_second.children == ["floci-ec2-i-a", "floci-ec2-i-b"]


def _aws_client(service: str, port: int):
    return boto3.client(
        service,
        endpoint_url=f"http://localhost:{port}",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def _non_terminated_instance_ids(ec2) -> list[str]:
    """EC2 instance ids still present and not in a terminated state.

    Guards against either plausible floci behavior after a `TerminateInstances`
    call: the instance vanishing from `describe_instances` entirely, or it
    staying listed with `State.Name == "terminated"` (both are legitimate
    "gone" outcomes; only a lingering *non*-terminated instance is a real
    "still there" failure).
    """
    ids = []
    for reservation in ec2.describe_instances()["Reservations"]:
        for instance in reservation["Instances"]:
            if instance["State"]["Name"] != "terminated":
                ids.append(instance["InstanceId"])
    return ids


def _all_floci_container_names() -> set[str]:
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=floci-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {line for line in result.stdout.splitlines() if line.strip()}


def _running_floci_container_names() -> set[str]:
    result = subprocess.run(
        ["docker", "ps", "--filter", "name=floci-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {line for line in result.stdout.splitlines() if line.strip()}


def _force_cleanup(env_name: str) -> None:
    """Best-effort teardown for a `finally` block: reap via the real
    `remove_env` (recorded children + parent + state dir) when there is
    still a state file to attribute anything to, else fall back to a raw
    `docker rm -f` of just the parent - mirroring
    test_engine_docker.py's own belt-and-suspenders convention so a failed
    assertion earlier in the test can never leak a container or wedge a
    later run under the same name.
    """
    if state.state_file(env_name).is_file():
        remove_env(env_name)
    else:
        subprocess.run(
            ["docker", "rm", "-f", container_name(env_name)], capture_output=True, check=False
        )


# --- scenario 1: the brief's own literal flow -------------------------------

ENV_NAME_APPLY = "t4-apply-test"


@pytest.mark.docker
def test_apply_then_destroy_round_trips_resources_via_boto3():
    spec = EnvSpec(
        name_prefix="t4-apply-demo",
        # Two subnets in distinct AZs: an RDS DB subnet group requires >=2, so
        # any spec carrying a database must span them (see materialize.py's
        # fail-loud check and envspec._random_vpc).
        vpcs=[
            VpcSpec(
                cidr="10.40.0.0/16",
                subnets=[
                    SubnetSpec(cidr="10.40.0.0/24", az_suffix="a"),
                    SubnetSpec(cidr="10.40.1.0/24", az_suffix="b"),
                ],
            )
        ],
        instances=[InstanceSpec(subnet_index=0, instance_type="t3.micro", count=1)],
        databases=[DatabaseSpec(engine="postgres", instance_class="db.t3.micro", count=1)],
        buckets=[BucketSpec(name_hint="app-data", count=1)],
    )
    try:
        env = create_env(ENV_NAME_APPLY, spec, with_azure=False)
        port = env.ports["aws"]

        result = apply(env)
        assert result.ok, result.transcript_path.read_text()
        # 1 vpc + 2 subnets + 1 instance + 1 db + 1 db_subnet_group (emitted
        # because the spec has a database) + 1 bucket = 7 terraform resources.
        assert result.resources_applied == 7

        applied_env = state.load_state(ENV_NAME_APPLY)
        assert applied_env.children, "apply() must record every child it spawned"
        assert any(name.startswith("floci-rds-") for name in applied_env.children), (
            "RDS in the S-shaped spec must exercise the real-engine child path"
        )
        assert any(name.startswith("floci-ec2-") for name in applied_env.children)

        # The recorded children must be genuinely running right now, while applied.
        running = _running_floci_container_names()
        for child_name in applied_env.children:
            assert child_name in running, f"{child_name} should be running while applied"

        s3 = _aws_client("s3", port)
        ec2 = _aws_client("ec2", port)
        rds = _aws_client("rds", port)
        assert len(s3.list_buckets()["Buckets"]) == 1
        assert len(_non_terminated_instance_ids(ec2)) == 1
        assert len(rds.describe_db_instances()["DBInstances"]) == 1

        destroy_result = destroy(env)
        assert destroy_result.ok, destroy_result.transcript_path.read_text()

        assert s3.list_buckets()["Buckets"] == []
        assert _non_terminated_instance_ids(ec2) == []
        assert rds.describe_db_instances()["DBInstances"] == []
    finally:
        _force_cleanup(ENV_NAME_APPLY)


# --- scenario 2: the live recorded-reap proof -------------------------------

ENV_NAME_REAP = "t4-reap-live-test"


@pytest.mark.docker
def test_remove_env_reaps_a_still_running_recorded_child():
    spec = EnvSpec(
        name_prefix="t4-reap-demo",
        vpcs=[
            VpcSpec(cidr="10.41.0.0/16", subnets=[SubnetSpec(cidr="10.41.0.0/24", az_suffix="a")])
        ],
        instances=[InstanceSpec(subnet_index=0, instance_type="t3.micro", count=1)],
    )
    try:
        env = create_env(ENV_NAME_REAP, spec, with_azure=False)

        result = apply(env)
        assert result.ok, result.transcript_path.read_text()

        applied_env = state.load_state(ENV_NAME_REAP)
        assert applied_env.children, "apply() must record the spawned EC2 child"
        assert all(name.startswith("floci-ec2-") for name in applied_env.children)

        # Prove the child is genuinely alive right now - the "live" half of
        # this proof that the pure select_children_to_reap/remove_env
        # dispatch tests (test_engine_docker.py) cannot provide on their
        # own, since they only ever exercise a canned table.
        running_before = _running_floci_container_names()
        for child_name in applied_env.children:
            assert child_name in running_before

        # No destroy() here, deliberately: this reproduces the exact
        # scenario ownership-by-recording exists for - an environment whose
        # terraform was never (or not yet) cleanly destroyed, so its
        # real-engine children are still alive when removal runs.
        report = remove_env(ENV_NAME_REAP)

        assert set(report.reaped) == set(applied_env.children)
        assert report.spared == []
        assert report.unparsed == []

        running_after = _all_floci_container_names()
        for child_name in applied_env.children:
            assert child_name not in running_after, f"{child_name} should have been reaped"
    finally:
        _force_cleanup(ENV_NAME_REAP)
