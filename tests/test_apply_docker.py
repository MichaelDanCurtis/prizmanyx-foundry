"""Docker-marked acceptance tests for foundry.tf_runner: real `terraform`
apply/destroy against a real floci environment, verified host-side via
boto3.

Two scenarios, because they are mutually exclusive within one environment's
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

import boto3
import pytest

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
from foundry.tf_runner import apply, destroy


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
        vpcs=[
            VpcSpec(cidr="10.40.0.0/16", subnets=[SubnetSpec(cidr="10.40.0.0/24", az_suffix="a")])
        ],
        instances=[InstanceSpec(subnet_index=0, instance_type="t3.micro", count=1)],
        databases=[DatabaseSpec(engine="postgres", instance_class="db.t3.micro", count=1)],
        buckets=[BucketSpec(name_hint="app-data", count=1)],
    )
    try:
        env = create_env(ENV_NAME_APPLY, spec)
        port = env.ports["aws"]

        result = apply(env)
        assert result.ok, result.transcript_path.read_text()
        # 1 vpc + 1 subnet + 1 instance + 1 db + 1 bucket = 5, matching
        # envspec.total_resource_count's own counting rule for this spec.
        assert result.resources_applied == 5

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
        env = create_env(ENV_NAME_REAP, spec)

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
