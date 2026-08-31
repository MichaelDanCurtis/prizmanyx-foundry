"""Tests for foundry.envspec: EnvSpec and the seeded random generator.

Written before src/foundry/envspec.py exists - the whole module import
fails until that file lands (the TDD red step for this task).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from foundry.envspec import (
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


@pytest.mark.parametrize("size", SIZES)
def test_random_spec_subnet_indices_are_always_in_range(size):
    spec = random_spec(size, seed=99)
    subnet_count = sum(len(vpc.subnets) for vpc in spec.vpcs)
    assert all(0 <= instance.subnet_index < subnet_count for instance in spec.instances)


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
