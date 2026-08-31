"""Tests for foundry.materialize: pure, no-I/O terraform emission.

Every test here is pure - no docker, no filesystem, no network. The
`@pytest.mark.docker` acceptance test that actually runs this emitted HCL
against a real floci container lives in tests/test_apply_docker.py.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from foundry.envspec import (
    BucketSpec,
    DatabaseSpec,
    EnvSpec,
    InstanceSpec,
    QueueSpec,
    RoleSpec,
    SubnetSpec,
    VpcSpec,
)
from foundry.materialize import emit_terraform
from foundry.state import EnvState


def _env(name: str = "demo", port: int = 15650) -> EnvState:
    return EnvState(
        name=name,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ports={"aws": port},
        spec_path=Path("/tmp/demo/spec.yaml"),
        status="running",
    )


def _two_instance_s_spec(name_prefix: str = "foundry-test") -> EnvSpec:
    """A hand-built spec with the same structural composition as
    `random_spec("S", ...)` / examples/specs/small.yaml: one vpc/two
    subnets, two one-instance groups, one database, two buckets, one
    queue, one role - "the 2-instance S spec" the brief's Step 1 names.
    """
    return EnvSpec(
        name_prefix=name_prefix,
        vpcs=[
            VpcSpec(
                cidr="10.0.0.0/16",
                subnets=[
                    SubnetSpec(cidr="10.0.0.0/24", az_suffix="a"),
                    SubnetSpec(cidr="10.0.1.0/24", az_suffix="b"),
                ],
            )
        ],
        instances=[
            InstanceSpec(subnet_index=0, instance_type="t3.micro", count=1),
            InstanceSpec(subnet_index=1, instance_type="t3.small", count=1),
        ],
        databases=[DatabaseSpec(engine="postgres", instance_class="db.t3.micro", count=1)],
        buckets=[
            BucketSpec(name_hint="app-data", count=1),
            BucketSpec(name_hint="app-logs", count=1),
        ],
        queues=[QueueSpec(count=1)],
        roles=[RoleSpec(count=1)],
    )


# --- determinism -------------------------------------------------------


def test_emit_terraform_is_deterministic_across_calls():
    spec = _two_instance_s_spec()
    env = _env()

    first = emit_terraform(spec, env)
    second = emit_terraform(spec, env)

    assert first == second


def test_emit_terraform_is_deterministic_across_fresh_equal_objects():
    # Not just "the same object twice" - two independently constructed but
    # equal (spec, env) pairs must also produce byte-identical output.
    first = emit_terraform(_two_instance_s_spec(), _env())
    second = emit_terraform(_two_instance_s_spec(), _env())

    assert first == second


# --- provider block + endpoints -----------------------------------------


def test_emit_terraform_provider_block_has_the_required_test_credentials_and_flags():
    tf_source = emit_terraform(_two_instance_s_spec(), _env())

    assert 'provider "aws" {' in tf_source
    assert 'access_key                  = "test"' in tf_source
    assert 'secret_key                  = "test"' in tf_source
    assert 'region                      = "us-east-1"' in tf_source
    assert "s3_use_path_style           = true" in tf_source
    assert "skip_credentials_validation = true" in tf_source
    assert "skip_metadata_api_check     = true" in tf_source
    assert "skip_requesting_account_id  = true" in tf_source


@pytest.mark.parametrize("service", ["ec2", "iam", "rds", "s3", "sqs"])
def test_emit_terraform_endpoints_block_lists_every_service_pointed_at_the_envs_port(service):
    tf_source = emit_terraform(_two_instance_s_spec(), _env(port=15777))

    assert f'{service} = "http://host.docker.internal:15777"' in tf_source


def test_emit_terraform_endpoints_use_this_specific_envs_port_not_another():
    tf_source = emit_terraform(_two_instance_s_spec(), _env(port=15601))

    assert "15601" in tf_source
    assert "15600" not in tf_source


# --- resource blocks for the 2-instance S spec ---------------------------


def test_emit_terraform_contains_exactly_the_expected_resource_blocks_for_the_s_spec():
    tf_source = emit_terraform(_two_instance_s_spec("foundry-test"), _env())

    expected_headers = [
        'resource "aws_vpc" "foundry-test-vpc-0" {',
        'resource "aws_subnet" "foundry-test-subnet-0" {',
        'resource "aws_subnet" "foundry-test-subnet-1" {',
        'resource "aws_instance" "foundry-test-instance-0" {',
        'resource "aws_instance" "foundry-test-instance-1" {',
        'resource "aws_db_instance" "foundry-test-database-0" {',
        'resource "aws_s3_bucket" "foundry-test-bucket-0" {',
        'resource "aws_s3_bucket" "foundry-test-bucket-1" {',
        'resource "aws_sqs_queue" "foundry-test-queue-0" {',
        'resource "aws_iam_role" "foundry-test-role-0" {',
    ]
    for header in expected_headers:
        assert header in tf_source, header

    # Nothing extra: exactly one block per spec list entry, no stray
    # duplicates from a copy-paste bug in one of the per-kind emitters.
    assert tf_source.count('resource "aws_vpc"') == 1
    assert tf_source.count('resource "aws_subnet"') == 2
    assert tf_source.count('resource "aws_instance"') == 2
    assert tf_source.count('resource "aws_db_instance"') == 1
    assert tf_source.count('resource "aws_s3_bucket"') == 2
    assert tf_source.count('resource "aws_sqs_queue"') == 1
    assert tf_source.count('resource "aws_iam_role"') == 1


def test_emit_terraform_instances_reference_their_own_subnet_by_subnet_index():
    tf_source = emit_terraform(_two_instance_s_spec("foundry-test"), _env())

    # instance-0 has subnet_index=0, instance-1 has subnet_index=1 - each
    # must reference ITS OWN subnet, not always subnet-0.
    instance_0_start = tf_source.index('resource "aws_instance" "foundry-test-instance-0"')
    instance_1_start = tf_source.index('resource "aws_instance" "foundry-test-instance-1"')
    instance_0_block = tf_source[instance_0_start : instance_0_start + 400]
    instance_1_block = tf_source[instance_1_start : instance_1_start + 400]

    assert "aws_subnet.foundry-test-subnet-0.id" in instance_0_block
    assert "aws_subnet.foundry-test-subnet-1.id" in instance_1_block


def test_emit_terraform_flattens_subnets_across_multiple_vpcs_in_vpc_then_subnet_order():
    spec = EnvSpec(
        name_prefix="multi-vpc",
        vpcs=[
            VpcSpec(cidr="10.0.0.0/16", subnets=[SubnetSpec(cidr="10.0.0.0/24", az_suffix="a")]),
            VpcSpec(
                cidr="10.1.0.0/16",
                subnets=[
                    SubnetSpec(cidr="10.1.0.0/24", az_suffix="a"),
                    SubnetSpec(cidr="10.1.1.0/24", az_suffix="b"),
                ],
            ),
        ],
        # subnet_index=2 is the SECOND vpc's SECOND subnet, in the
        # flattened (vpc-order, then subnet-order) numbering.
        instances=[InstanceSpec(subnet_index=2, instance_type="t3.micro", count=1)],
    )

    tf_source = emit_terraform(spec, _env())

    assert 'resource "aws_subnet" "multi-vpc-subnet-0" {' in tf_source
    assert 'resource "aws_subnet" "multi-vpc-subnet-1" {' in tf_source
    assert 'resource "aws_subnet" "multi-vpc-subnet-2" {' in tf_source
    # subnet-0 belongs to vpc-0; subnet-1 and subnet-2 belong to vpc-1.
    subnet_2_start = tf_source.index('resource "aws_subnet" "multi-vpc-subnet-2"')
    subnet_2_block = tf_source[subnet_2_start : subnet_2_start + 200]
    assert "aws_vpc.multi-vpc-vpc-1.id" in subnet_2_block
    # The instance addressing subnet_index=2 must reference subnet-2.
    assert "aws_subnet.multi-vpc-subnet-2.id" in tf_source


def test_emit_terraform_with_an_empty_spec_still_produces_a_valid_provider_only_file():
    spec = EnvSpec(name_prefix="empty-demo")

    tf_source = emit_terraform(spec, _env())

    assert 'provider "aws" {' in tf_source
    assert "resource " not in tf_source


# --- name_prefix shape: enforced at the EnvSpec boundary, honored verbatim here ---


def test_name_prefix_shape_is_enforced_before_it_can_ever_reach_the_emitter():
    # emit_terraform never re-validates name_prefix itself - EnvSpec's own
    # pydantic pattern (`_NAME_PREFIX_PATTERN`) is the single source of
    # truth, and it fires before an invalid spec object can even exist.
    with pytest.raises(Exception, match="name_prefix"):
        EnvSpec(name_prefix="Bad-Prefix!")


def test_emit_terraform_uses_the_validated_name_prefix_verbatim_in_every_label():
    spec = EnvSpec(name_prefix="a" * 25)  # the longest shape _NAME_PREFIX_PATTERN allows
    spec = spec.model_copy(update={"vpcs": [VpcSpec(cidr="10.0.0.0/16")]})

    tf_source = emit_terraform(spec, _env())

    assert f'resource "aws_vpc" "{"a" * 25}-vpc-0" {{' in tf_source


# --- emitter-side injection defense (EnvSpec does not constrain these fields) ---


def test_emit_terraform_escapes_a_hostile_instance_type_without_breaking_the_hcl_structure():
    hostile = 'x"\n}\nresource "aws_iam_role" "evil" {\n  name = "pwned'
    spec = EnvSpec(
        name_prefix="hostile-demo",
        vpcs=[VpcSpec(cidr="10.0.0.0/16", subnets=[SubnetSpec(cidr="10.0.0.0/24", az_suffix="a")])],
        instances=[InstanceSpec(subnet_index=0, instance_type=hostile, count=1)],
    )

    tf_source = emit_terraform(spec, _env())

    # The hostile payload must appear only inside its own escaped string
    # literal - never as a live break-out that opens a second, attacker
    # controlled resource block. The word "resource" itself survives
    # json.dumps escaping unchanged (only quotes/newlines are escaped), so
    # the count checks for `resource "aws_` specifically - unescaped -
    # never for the bare word, which the hostile payload's ESCAPED form
    # still legitimately contains (as `resource \"aws_...`, backslash and
    # all, which is not a live resource block).
    assert 'resource "aws_iam_role" "evil"' not in tf_source
    assert tf_source.count('resource "aws_instance"') == 1
    # Exactly the vpc + subnet + instance this spec legitimately declares -
    # the escaped payload contributes nothing to this count.
    assert tf_source.count('resource "aws_') == 3


def test_emit_terraform_slugifies_a_hostile_bucket_name_hint():
    hostile = 'App Data" } resource "aws_s3_bucket" "evil" { bucket = "pwned'
    spec = EnvSpec(name_prefix="hostile-bucket", buckets=[BucketSpec(name_hint=hostile, count=1)])

    tf_source = emit_terraform(spec, _env())

    assert 'resource "aws_s3_bucket" "evil"' not in tf_source
    assert tf_source.count('resource "aws_s3_bucket"') == 1
    # The slug keeps only [a-z0-9-] - no quote can survive into the bucket
    # name attribute's own string literal.
    match = re.search(r'bucket = "([^"]*)"', tf_source)
    assert match is not None
    bucket_name_value = match.group(1)
    assert re.fullmatch(r"[a-z0-9${}.-]+", bucket_name_value)
