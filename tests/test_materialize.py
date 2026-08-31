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


def _resource_block(tf_source: str, header: str) -> str:
    """The full text of one resource block, from `header` up to (but not
    including) the next top-level `resource "..."` declaration, or the end
    of the string if `header` is the last block. Robust to a block's own
    internal blank lines (e.g. the blank line before an `aws_instance`'s
    `tags {}` section) that a naive search for the next "\\n\\n" would
    mistake for the block's end.
    """
    start = tf_source.index(header)
    next_start = tf_source.find('\nresource "', start + 1)
    return tf_source[start:] if next_start == -1 else tf_source[start:next_start]


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
        'resource "aws_db_subnet_group" "foundry-test-dbsubnets" {',
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
    assert tf_source.count('resource "aws_db_subnet_group"') == 1
    assert tf_source.count('resource "aws_db_instance"') == 1
    assert tf_source.count('resource "aws_s3_bucket"') == 2
    assert tf_source.count('resource "aws_sqs_queue"') == 1
    assert tf_source.count('resource "aws_iam_role"') == 1


# --- the DB subnet group: RDS instances need >=2-AZ network placement ------


def test_emit_terraform_a_database_spec_emits_one_db_subnet_group_referenced_by_every_instance():
    # A live acceptance re-run failed at real `terraform apply`: "RDS:
    # InvalidVPCNetworkStateFault: No subnets available for DB subnet
    # group default" - aws_db_instance with no db_subnet_group_name falls
    # back to the account's "default" DB subnet group, which has no
    # subnets. This was never correct on real AWS either (a VPC DB
    # instance requires an explicit subnet group spanning >=2 AZs); floci
    # only tolerated it intermittently.
    spec = _two_instance_s_spec("db-demo")

    tf_source = emit_terraform(spec, _env())

    assert tf_source.count('resource "aws_db_subnet_group"') == 1
    group_block = _resource_block(tf_source, 'resource "aws_db_subnet_group" "db-demo-dbsubnets"')
    assert 'name = "db-demo-dbsubnets"' in group_block
    assert "aws_subnet.db-demo-subnet-0.id" in group_block
    assert "aws_subnet.db-demo-subnet-1.id" in group_block
    assert 'Application = "db-demo"' in group_block  # it's taggable too

    db_block = _resource_block(tf_source, 'resource "aws_db_instance" "db-demo-database-0"')
    assert "db_subnet_group_name = aws_db_subnet_group.db-demo-dbsubnets.name" in db_block


def test_emit_terraform_omits_the_db_subnet_group_when_the_spec_has_no_database():
    spec = EnvSpec(
        name_prefix="no-db-demo",
        vpcs=[VpcSpec(cidr="10.0.0.0/16", subnets=[SubnetSpec(cidr="10.0.0.0/24", az_suffix="a")])],
    )

    tf_source = emit_terraform(spec, _env())

    assert "aws_db_subnet_group" not in tf_source


def test_emit_terraform_db_subnet_group_uses_only_the_first_vpcs_subnets_in_a_multi_vpc_spec():
    # RDS requires every subnet in ONE DB subnet group to belong to the
    # SAME vpc - mixing subnets across vpcs is rejected by real AWS
    # outright. Every current size profile with >1 vpc (M, L, XL) also has
    # >=1 database group, so naively using "every subnet in the spec"
    # would repeat this exact bug at a bigger estate size. Scoped to
    # vpc-0 instead - a documented, deliberate simplification
    # (DatabaseSpec carries no vpc/subnet field of its own to place it
    # more precisely - see materialize._db_subnet_group_blocks's own
    # docstring).
    spec = EnvSpec(
        name_prefix="multi-vpc-db",
        vpcs=[
            VpcSpec(
                cidr="10.0.0.0/16",
                subnets=[
                    SubnetSpec(cidr="10.0.0.0/24", az_suffix="a"),
                    SubnetSpec(cidr="10.0.1.0/24", az_suffix="b"),
                ],
            ),
            VpcSpec(
                cidr="10.1.0.0/16",
                subnets=[SubnetSpec(cidr="10.1.0.0/24", az_suffix="a")],
            ),
        ],
        databases=[DatabaseSpec(engine="postgres", instance_class="db.t3.micro", count=1)],
    )

    tf_source = emit_terraform(spec, _env())

    group_block = _resource_block(
        tf_source, 'resource "aws_db_subnet_group" "multi-vpc-db-dbsubnets"'
    )
    assert "aws_subnet.multi-vpc-db-subnet-0.id" in group_block
    assert "aws_subnet.multi-vpc-db-subnet-1.id" in group_block
    # subnet-2 is vpc-1's own subnet - must NOT be pulled into vpc-0's group.
    assert "aws_subnet.multi-vpc-db-subnet-2.id" not in group_block


def test_emit_terraform_raises_if_the_first_vpc_cannot_offer_two_distinct_azs():
    # The emitter's own second line of defense - not just trusting the
    # generator - see _db_subnet_group_blocks's docstring. Two subnets,
    # same az_suffix: exactly the shape `_random_vpc` could produce before
    # it was fixed to guarantee distinctness (docs/DECISIONS.md entry 005).
    spec = EnvSpec(
        name_prefix="same-az-demo",
        vpcs=[
            VpcSpec(
                cidr="10.0.0.0/16",
                subnets=[
                    SubnetSpec(cidr="10.0.0.0/24", az_suffix="a"),
                    SubnetSpec(cidr="10.0.1.0/24", az_suffix="a"),
                ],
            )
        ],
        databases=[DatabaseSpec(engine="postgres", instance_class="db.t3.micro", count=1)],
    )

    with pytest.raises(ValueError, match="distinct availability zone"):
        emit_terraform(spec, _env())


def test_emit_terraform_db_subnet_group_determinism_preserved():
    spec = _two_instance_s_spec("db-determinism")
    env = _env()

    first = emit_terraform(spec, env)
    second = emit_terraform(spec, env)

    assert first == second


# --- the Application tag: the deterministic migration-unit inference seed ---


def test_emit_terraform_tags_every_taggable_resource_with_application_equal_to_name_prefix():
    # Prizmanyx's migration-unit inference is tag-seeded
    # (packages/discovery/prizmanyx_discovery/migration_units.py,
    # GROUP_TAG_KEYS: Application/application/App/app/Workload/workload/
    # Service/service) - a graph node whose properties["tags"] carries
    # none of those keys with a non-empty value joins NO migration unit at
    # all. A materialized estate with no such tag anywhere yields zero
    # units and can never pass the loop's exit gate (units_approved has
    # nothing to approve) - confirmed live during Task 8 acceptance:
    # 19 graph nodes, 0 units. Every taggable resource this generator
    # emits (aws_vpc, aws_subnet, aws_instance, aws_db_instance,
    # aws_s3_bucket, aws_sqs_queue, aws_iam_role) must carry
    # Application=<name_prefix> for a materialized estate to actually be
    # migratable, not merely extractable.
    spec = _two_instance_s_spec("tag-demo")

    tf_source = emit_terraform(spec, _env())

    taggable_resource_headers = [
        'resource "aws_vpc" "tag-demo-vpc-0"',
        'resource "aws_subnet" "tag-demo-subnet-0"',
        'resource "aws_subnet" "tag-demo-subnet-1"',
        'resource "aws_instance" "tag-demo-instance-0"',
        'resource "aws_instance" "tag-demo-instance-1"',
        'resource "aws_db_subnet_group" "tag-demo-dbsubnets"',
        'resource "aws_db_instance" "tag-demo-database-0"',
        'resource "aws_s3_bucket" "tag-demo-bucket-0"',
        'resource "aws_s3_bucket" "tag-demo-bucket-1"',
        'resource "aws_sqs_queue" "tag-demo-queue-0"',
        'resource "aws_iam_role" "tag-demo-role-0"',
    ]
    for header in taggable_resource_headers:
        assert 'Application = "tag-demo"' in _resource_block(tf_source, header), header

    # Exactly one Application tag per taggable resource - not accidentally
    # duplicated within a block, not missing from one.
    assert tf_source.count('Application = "tag-demo"') == len(taggable_resource_headers)


def test_emit_terraform_instance_tags_block_carries_both_name_and_application():
    # aws_instance already had its own `tags { Name = ... }` block before
    # this tag existed - proving the two coexist in ONE tags block, not
    # that Application silently replaced Name or landed in a second one.
    spec = _two_instance_s_spec("merge-demo")

    tf_source = emit_terraform(spec, _env())

    block = _resource_block(tf_source, 'resource "aws_instance" "merge-demo-instance-0"')
    assert block.count("tags = {") == 1
    assert 'Name = "merge-demo-instance-0-${count.index}"' in block
    assert 'Application = "merge-demo"' in block


def test_emit_terraform_application_tag_determinism_and_name_prefix_scoping():
    spec = _two_instance_s_spec("determinism-demo")
    env = _env()

    first = emit_terraform(spec, env)
    second = emit_terraform(spec, env)

    assert first == second
    # One shared value across every resource, by design: the inference
    # groups candidate nodes by tag VALUE, so a single Application value
    # joins them all into one coherent migration unit. 11, not 10: the
    # "2-instance S spec" fixture has a database, so aws_db_subnet_group
    # (itself taggable) is now part of the taggable surface too.
    assert first.count('Application = "determinism-demo"') == 11
    assert "Application = " not in first.replace('Application = "determinism-demo"', "")


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


# --- the count meta-argument mechanism (what the XL profile actually relies on) ---


def test_emit_terraform_uses_the_count_meta_argument_for_a_group_with_count_greater_than_one():
    # This is the ACTUAL mechanism random_spec("XL", ...) depends on: one
    # resource BLOCK per spec group, terraform's own `count` meta-argument
    # fanning it out to many real resources - never N separate blocks (see
    # emit_terraform's own docstring). A group of `count=1` (every other
    # test in this file) can't distinguish "count is wired up" from "count
    # is just always 1" - this one uses count=3 specifically to prove it.
    spec = EnvSpec(
        name_prefix="xl-demo",
        vpcs=[VpcSpec(cidr="10.0.0.0/16", subnets=[SubnetSpec(cidr="10.0.0.0/24", az_suffix="a")])],
        instances=[InstanceSpec(subnet_index=0, instance_type="t3.micro", count=3)],
        buckets=[BucketSpec(name_hint="data", count=3)],
    )

    tf_source = emit_terraform(spec, _env())

    # Exactly one block per group - not three - with terraform's own count
    # meta-argument set to 3, and ${count.index} in the per-item name so
    # the 3 real resources it expands to don't collide with each other.
    assert tf_source.count('resource "aws_instance" "xl-demo-instance-0"') == 1
    assert "count         = 3" in tf_source
    assert 'Name = "xl-demo-instance-0-${count.index}"' in tf_source

    assert tf_source.count('resource "aws_s3_bucket" "xl-demo-bucket-0"') == 1
    assert "count  = 3" in tf_source
    assert 'bucket = "xl-demo-data-0-${count.index}"' in tf_source


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


def test_emit_terraform_neutralizes_hcl_interpolation_syntax_in_a_hostile_az_suffix():
    # Finding 6: a second, distinct injection class from the quote-breakout
    # tests above. HCL scans a string literal's own CONTENT for `${...}`
    # (interpolation) and `%{...}` (a directive) even AFTER proper
    # backslash/quote escaping - json.dumps has no idea these exist, since
    # they are not JSON syntax at all. A hostile az_suffix containing a
    # literal `${aws_iam_role.evil.arn}` would, if left unescaped, have
    # terraform evaluate that expression for REAL once applied - a live
    # cross-resource reference, not inert text - which is a materially
    # worse outcome than merely breaking the string open. `az_suffix` goes
    # straight through `_hcl_string` (unlike name_hint, which is slugged
    # to [a-z0-9-] first and so never reaches this gap at all).
    hostile_az_suffix = "a${aws_iam_role.evil.arn}"
    spec = EnvSpec(
        name_prefix="interp-demo",
        vpcs=[
            VpcSpec(
                cidr="10.0.0.0/16",
                subnets=[SubnetSpec(cidr="10.0.0.0/24", az_suffix=hostile_az_suffix)],
            )
        ],
    )

    tf_source = emit_terraform(spec, _env())

    # HCL's own documented escape for a literal dollar-brace is `$${` -
    # that neutralized form must be what actually appears in the emitted
    # availability_zone string.
    assert "$${aws_iam_role.evil.arn}" in tf_source
    # The live, unescaped form - which terraform would actually evaluate -
    # must never appear.
    assert "us-east-1a${aws_iam_role.evil.arn}" not in tf_source


def test_emit_terraform_neutralizes_a_hostile_percent_brace_directive_in_instance_type():
    # The `%{...}` half of the same gap (an HCL template directive, e.g.
    # `%{if ...}...%{endif}`) - HCL's own documented escape is `%%{`.
    hostile_instance_type = "t3.micro%{if true}pwned%{endif}"
    spec = EnvSpec(
        name_prefix="interp-demo-2",
        vpcs=[VpcSpec(cidr="10.0.0.0/16", subnets=[SubnetSpec(cidr="10.0.0.0/24", az_suffix="a")])],
        instances=[InstanceSpec(subnet_index=0, instance_type=hostile_instance_type, count=1)],
    )

    tf_source = emit_terraform(spec, _env())

    assert "%%{if true}" in tf_source
    assert "t3.micro%{if true}pwned%{endif}" not in tf_source
