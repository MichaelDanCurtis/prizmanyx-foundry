"""Tests for foundry.envspec: EnvSpec and the seeded random generator.

Written before src/foundry/envspec.py exists - the whole module import
fails until that file lands (the TDD red step for this task).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from foundry.envspec import (
    _NAME_PREFIX_PATTERN,
    BucketSpec,
    DatabaseSpec,
    EnvSpec,
    InstanceSpec,
    QueueSpec,
    RoleSpec,
    SpecLoadError,
    SubnetSpec,
    VpcSpec,
    load_spec,
    random_spec,
    total_resource_count,
)

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples" / "specs"

SIZES = ("S", "M", "L", "XL")


def _one_subnet_vpc() -> VpcSpec:
    return VpcSpec(cidr="10.0.0.0/16", subnets=[SubnetSpec(cidr="10.0.0.0/24", az_suffix="a")])


# --- random_spec: determinism ------------------------------------------------


def test_same_size_and_seed_produce_a_fully_equal_spec():
    first = random_spec("S", seed=42)
    second = random_spec("S", seed=42)
    assert first == second


@pytest.mark.parametrize("size", SIZES)
def test_same_size_and_seed_produce_equal_specs_for_every_size(size):
    assert random_spec(size, seed=7) == random_spec(size, seed=7)


def test_different_seeds_produce_different_specs():
    assert random_spec("S", seed=1) != random_spec("S", seed=2)


def test_different_sizes_produce_different_specs_for_the_same_seed():
    assert random_spec("S", seed=1) != random_spec("M", seed=1)


def test_random_spec_rejects_an_unknown_size():
    with pytest.raises(ValueError, match="unknown size"):
        random_spec("XXL", seed=1)


# --- random_spec: structural invariants --------------------------------------


@pytest.mark.parametrize("size", SIZES)
def test_random_spec_never_produces_a_negative_count(size):
    spec = random_spec(size, seed=13)
    counts = (
        [i.count for i in spec.instances]
        + [d.count for d in spec.databases]
        + [b.count for b in spec.buckets]
        + [q.count for q in spec.queues]
        + [r.count for r in spec.roles]
    )
    assert counts, "expected at least one group to assert non-negativity over"
    assert all(count >= 0 for count in counts)


# The EC2 instance types floci can actually APPLY, established by direct
# probe against the pinned floci image on 2026-08-31 (see docs/DECISIONS.md
# entry 003). Written out as a literal here, deliberately NOT imported from
# `envspec._INSTANCE_TYPES` - importing the generator's own vocabulary would
# make this test tautological, unable to catch exactly the regression it
# exists for (a plausible-looking AWS type added to that tuple that floci's
# DescribeInstanceTypes answers with zero rows, which the terraform AWS
# provider turns into a hard apply failure).
FLOCI_APPLIABLE_INSTANCE_TYPES = frozenset({"t3.micro", "t3.small", "t3.medium", "m5.large"})


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("seed", range(10))
def test_random_spec_only_ever_draws_instance_types_floci_can_apply(size, seed):
    # The generator's contract is that its estates APPLY against floci - a
    # spec that cannot be materialized is not a smaller estate, it is a
    # broken one. `m5.xlarge` was in this vocabulary and affected 12 of 30
    # S-seeds (40%), including seed 42, which is the plan's own acceptance
    # seed; the failure only ever surfaced at `terraform apply` time.
    spec = random_spec(size, seed=seed)
    assert spec.instances, "expected at least one instance group to assert over"
    drawn = {instance.instance_type for instance in spec.instances}
    assert drawn <= FLOCI_APPLIABLE_INSTANCE_TYPES, (
        f"{size}/seed {seed} drew {sorted(drawn - FLOCI_APPLIABLE_INSTANCE_TYPES)}, "
        "which floci's DescribeInstanceTypes answers with zero rows"
    )


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("seed", range(20))
def test_random_spec_vpc_subnets_always_span_distinct_azs(size, seed):
    # RDS DB subnet groups require their subnets to span >=2 distinct
    # availability zones - a real AWS constraint (InvalidVPCNetworkState
    # Fault otherwise), not a floci quirk - and materialize.py's
    # aws_db_subnet_group depends on this generator invariant to ever
    # produce an appliable spec whenever it has a database (see
    # docs/DECISIONS.md entry 005). Before this test existed, `_random_vpc`
    # drew each subnet's az_suffix INDEPENDENTLY via `rng.choice`, which
    # could - and empirically did, for 69/200 S-profile seeds checked by
    # hand while diagnosing a live acceptance failure - draw the SAME
    # az_suffix for two subnets in the same vpc.
    spec = random_spec(size, seed=seed)
    for vpc in spec.vpcs:
        az_suffixes = [subnet.az_suffix for subnet in vpc.subnets]
        assert len(set(az_suffixes)) == len(az_suffixes), (
            f"{size}/seed {seed}: a vpc's subnets {az_suffixes} collide on an AZ"
        )


@pytest.mark.parametrize("size", SIZES)
def test_random_spec_subnet_indices_are_always_in_range(size):
    spec = random_spec(size, seed=99)
    subnet_count = sum(len(vpc.subnets) for vpc in spec.vpcs)
    assert all(0 <= instance.subnet_index < subnet_count for instance in spec.instances)


@pytest.mark.parametrize("size", SIZES)
def test_random_spec_name_prefix_always_conforms_to_the_pattern(size):
    # A pathologically large (and a negative) seed stress the bound that
    # keeps name_prefix short and sign-free regardless of `seed`'s shape.
    for seed in (0, -1, 42, 10**18):
        spec = random_spec(size, seed=seed)
        assert re.fullmatch(_NAME_PREFIX_PATTERN, spec.name_prefix), spec.name_prefix


def test_random_spec_round_trips_through_yaml(tmp_path):
    spec = random_spec("S", seed=5)
    out_path = tmp_path / "roundtrip.yaml"
    out_path.write_text(yaml.safe_dump(spec.model_dump()))
    assert load_spec(out_path) == spec


# --- random_spec: the XL resource budget -------------------------------------


def test_xl_total_resource_count_lands_in_the_documented_budget():
    spec = random_spec("XL", seed=123)
    assert 8_000 <= total_resource_count(spec) <= 15_000


def test_xl_total_resource_count_does_not_depend_on_seed():
    # Quantities are fixed per size - only content (cidrs, instance types,
    # subnet assignment) is seed-dependent - so the budget check above
    # can't be a fluke of whichever seed a test happens to pick.
    totals = {total_resource_count(random_spec("XL", seed=s)) for s in (1, 2, 3, 999)}
    assert len(totals) == 1


# --- EnvSpec: count validation ------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        lambda: InstanceSpec(subnet_index=0, instance_type="t3.micro", count=-1),
        lambda: DatabaseSpec(engine="postgres", instance_class="db.t3.micro", count=-1),
        lambda: BucketSpec(name_hint="data", count=-1),
        lambda: QueueSpec(count=-1),
        lambda: RoleSpec(count=-1),
    ],
)
def test_negative_counts_are_rejected_on_every_group_type(build):
    with pytest.raises(ValidationError, match="count"):
        build()


def test_database_engine_is_restricted_to_the_supported_literals():
    with pytest.raises(ValidationError):
        DatabaseSpec(engine="oracle", instance_class="db.t3.micro", count=1)


def test_instance_spec_rejects_a_negative_subnet_index_on_its_own():
    # Standalone construction (no enclosing EnvSpec to run the cross-
    # reference check) must still reject a negative index on its own -
    # otherwise a caller that builds InstanceSpec directly gets no
    # protection at all until it happens to land inside an EnvSpec.
    with pytest.raises(ValidationError, match="subnet_index"):
        InstanceSpec(subnet_index=-1, instance_type="t3.micro", count=1)


# --- EnvSpec: name_prefix shape validation ------------------------------------
# Task 4 interpolates name_prefix into terraform resource names
# (`<name_prefix>-vpc-0`, ...), so it is shape-validated at this boundary
# rather than trusted through to the emitter (the plan's two-sided
# injection lesson: validate at the boundary AND fail loud at the
# emitter). Pattern sized for suffixes: 24 chars after the leading letter
# leaves room for `-vpc-NN`-style additions under common AWS name caps.


def test_name_prefix_rejects_uppercase_and_underscores():
    with pytest.raises(ValidationError, match=re.escape(_NAME_PREFIX_PATTERN)):
        EnvSpec(name_prefix="Bad_Prefix")


def test_name_prefix_rejects_the_empty_string():
    with pytest.raises(ValidationError, match=re.escape(_NAME_PREFIX_PATTERN)):
        EnvSpec(name_prefix="")


def test_name_prefix_rejects_26_or_more_characters():
    with pytest.raises(ValidationError, match=re.escape(_NAME_PREFIX_PATTERN)):
        EnvSpec(name_prefix="a" * 26)


def test_name_prefix_accepts_25_characters_at_the_boundary():
    spec = EnvSpec(name_prefix="a" * 25)
    assert spec.name_prefix == "a" * 25


# --- EnvSpec: subnet_index cross-reference validation ------------------------


def test_out_of_range_subnet_index_is_rejected_naming_the_index():
    with pytest.raises(ValidationError, match="subnet_index"):
        EnvSpec(
            name_prefix="test",
            vpcs=[_one_subnet_vpc()],
            instances=[InstanceSpec(subnet_index=1, instance_type="t3.micro", count=1)],
        )


def test_negative_subnet_index_is_rejected_too():
    with pytest.raises(ValidationError, match="subnet_index"):
        EnvSpec(
            name_prefix="test",
            vpcs=[_one_subnet_vpc()],
            instances=[InstanceSpec(subnet_index=-1, instance_type="t3.micro", count=1)],
        )


def test_in_range_subnet_index_is_accepted():
    spec = EnvSpec(
        name_prefix="test",
        vpcs=[_one_subnet_vpc()],
        instances=[InstanceSpec(subnet_index=0, instance_type="t3.micro", count=1)],
    )
    assert spec.instances[0].subnet_index == 0


# --- load_spec ----------------------------------------------------------------


def test_load_spec_loads_the_hand_written_small_example():
    spec = load_spec(EXAMPLES_DIR / "small.yaml")
    assert isinstance(spec, EnvSpec)
    assert spec.vpcs and spec.instances and spec.databases and spec.buckets


def test_load_spec_rejects_a_bad_subnet_index_naming_the_field(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name_prefix: bad-spec\n"
        "vpcs:\n"
        "  - cidr: 10.0.0.0/16\n"
        "    subnets:\n"
        "      - cidr: 10.0.0.0/24\n"
        "        az_suffix: a\n"
        "instances:\n"
        "  - subnet_index: 3\n"
        "    instance_type: t3.micro\n"
        "    count: 1\n"
    )
    with pytest.raises(SpecLoadError, match="subnet_index"):
        load_spec(bad)


def test_load_spec_rejects_a_negative_count_naming_the_field(tmp_path):
    bad = tmp_path / "bad_count.yaml"
    bad.write_text("name_prefix: bad-spec\nqueues:\n  - count: -1\n")
    with pytest.raises(SpecLoadError, match="count"):
        load_spec(bad)


def test_load_spec_names_the_file_path_on_failure(tmp_path):
    missing = tmp_path / "does-not-exist.yaml"
    with pytest.raises(SpecLoadError, match=r"does-not-exist\.yaml"):
        load_spec(missing)
