"""EnvSpec: the shape of a Floci-backed AWS estate, described before it exists.

EnvSpec is the external boundary between a human (or `random_spec`) and
everything downstream that builds an estate from it: the environment engine,
the terraform materializer, and the loop driver's report (later tasks).
Field names here are a contract those tasks consume verbatim.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator


class SpecLoadError(Exception):
    """Raised when a YAML file cannot be read, parsed, or validated as an EnvSpec.

    The message is always the file path followed by the underlying error's
    own text, verbatim - this class adds path context, nothing else. A
    pydantic ValidationError already names the offending field(s);
    rewriting or summarizing that message here would only make it less
    useful.
    """


class SubnetSpec(BaseModel):
    """One subnet within a VpcSpec."""

    cidr: str
    az_suffix: str


class VpcSpec(BaseModel):
    """One VPC and its subnets."""

    cidr: str
    subnets: list[SubnetSpec] = Field(default_factory=list)


class InstanceSpec(BaseModel):
    """A group of like-shaped EC2 instances in one subnet.

    `subnet_index` indexes into EnvSpec's flattened subnet list (every
    vpc's subnets, in vpc order then subnet order) - not a per-vpc index.
    The `ge=0` below rejects a negative index even on a standalone
    InstanceSpec built outside an EnvSpec; the *upper* bound (< the actual
    subnet count) still needs EnvSpec's own validator, since this model
    can't see the other vpcs on its own.
    """

    subnet_index: int = Field(ge=0)
    instance_type: str
    count: int = Field(ge=0)


class DatabaseSpec(BaseModel):
    """A group of like-shaped RDS instances."""

    engine: Literal["postgres", "mysql"]
    instance_class: str
    count: int = Field(ge=0)


class BucketSpec(BaseModel):
    """A group of like-shaped S3 buckets."""

    name_hint: str
    count: int = Field(ge=0)


class QueueSpec(BaseModel):
    """A group of like-shaped SQS queues."""

    count: int = Field(ge=0)


class RoleSpec(BaseModel):
    """A group of like-shaped IAM roles."""

    count: int = Field(ge=0)


# Task 4 interpolates name_prefix into terraform resource names
# (`<name_prefix>-vpc-0`, ...), so it is shape-validated here, at the
# boundary where an untrusted YAML file (or a caller) first supplies it -
# the plan's two-sided injection lesson: validate at the boundary AND fail
# loud at the emitter. Lowercase-start, lowercase/digit/hyphen body, same
# family as the env-name pattern but sized for suffixes: 24 characters
# after the leading letter leaves room for `-vpc-NN`-style additions under
# common AWS resource-name length caps.
_NAME_PREFIX_PATTERN = r"^[a-z][a-z0-9-]{0,24}$"


class EnvSpec(BaseModel):
    """The full description of a Floci-backed AWS estate.

    Every `count` field across instances/databases/buckets/queues/roles
    must be >=0 (enforced per-field on each group's own model above). Every
    `instances[i].subnet_index` must be >=0 (also enforced on InstanceSpec
    itself) and land within the flattened list of every vpc's subnets
    (enforced below - only EnvSpec can see all vpcs at once). `name_prefix`
    must match `_NAME_PREFIX_PATTERN` (see its comment for why).
    """

    name_prefix: str = Field(pattern=_NAME_PREFIX_PATTERN)
    vpcs: list[VpcSpec] = Field(default_factory=list)
    instances: list[InstanceSpec] = Field(default_factory=list)
    databases: list[DatabaseSpec] = Field(default_factory=list)
    buckets: list[BucketSpec] = Field(default_factory=list)
    queues: list[QueueSpec] = Field(default_factory=list)
    roles: list[RoleSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _subnet_indices_are_in_range(self) -> EnvSpec:
        subnet_count = sum(len(vpc.subnets) for vpc in self.vpcs)
        for position, instance in enumerate(self.instances):
            if not 0 <= instance.subnet_index < subnet_count:
                raise ValueError(
                    f"instances[{position}].subnet_index={instance.subnet_index} is out "
                    f"of range for {subnet_count} subnet(s) across all vpcs"
                )
        return self


def load_spec(path: str | Path) -> EnvSpec:
    """Load and validate an EnvSpec from a YAML file.

    Any failure - the file missing or unreadable, invalid YAML, or a spec
    that fails EnvSpec's own validation - raises SpecLoadError. Validation
    errors are pydantic's own, verbatim; this function only prefixes the
    file path onto them.
    """
    spec_path = Path(path)
    try:
        raw = yaml.safe_load(spec_path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise SpecLoadError(f"{spec_path}: {exc}") from exc

    try:
        return EnvSpec.model_validate(raw)
    except ValidationError as exc:
        raise SpecLoadError(f"{spec_path}: {exc}") from exc


def total_resource_count(spec: EnvSpec) -> int:
    """Count resources the way the terraform materializer will count them:
    one per vpc, one per subnet (flattened across every vpc), plus the
    summed `count` field of every instance/database/bucket/queue/role
    group.

    Shared here, rather than duplicated in a test, so the materializer and
    the XL budget check below can never drift apart on what "one resource"
    means.
    """
    subnet_total = sum(len(vpc.subnets) for vpc in spec.vpcs)
    return (
        len(spec.vpcs)
        + subnet_total
        + sum(group.count for group in spec.instances)
        + sum(group.count for group in spec.databases)
        + sum(group.count for group in spec.buckets)
        + sum(group.count for group in spec.queues)
        + sum(group.count for group in spec.roles)
    )


_AZ_SUFFIXES = ("a", "b", "c")

# Vocabulary pinned to the EC2 instance types VALIDATED against the pinned
# floci image digest (`engine.DEFAULT_FLOCI_IMAGE`) by direct probe on
# 2026-08-31 - not to "plausible AWS types". floci's DescribeInstanceTypes
# answers each of these with one row; it answered `m5.xlarge` (previously
# listed here) with ZERO rows, and the terraform AWS provider turns that
# empty result into a hard apply failure - "reading EC2 Instance Type
# (m5.xlarge): empty result" - so any (size, seed) pair that drew it
# produced a spec that could not be materialized at all. Measured before
# removal: 12 of 30 S-seeds affected, seed 42 among them, which is the
# foundry plan's own acceptance seed.
#
# A generated estate that cannot APPLY is not a smaller estate, it is a
# broken one; that is why this list is emulator-validated rather than
# aspirational. REVISIT ON ANY FLOCI IMAGE UPGRADE: probe
# `describe_instance_types` against the new digest before widening this,
# and update docs/DECISIONS.md entry 003 with the new transcript date.
# tests/test_envspec.py carries its own independent copy of the validated
# set, on purpose - a test importing this tuple could not catch a bad
# addition to it.
_INSTANCE_TYPES = ("t3.micro", "t3.small", "t3.medium", "m5.large")

_DB_ENGINES = ("postgres", "mysql")
_DB_INSTANCE_CLASSES = ("db.t3.micro", "db.t3.small", "db.r5.large")
_BUCKET_WORDS = ("data", "logs", "backups", "assets", "artifacts", "exports")

# `hash()` on `str` is salted per-process by PYTHONHASHSEED, so hashing
# (size, seed) directly would make random_spec reproducible only within a
# single process. Map size to a fixed ordinal first so only ints reach
# hash() below - int hashes are stable across processes and Python
# versions.
_SIZE_ORDINAL: dict[str, int] = {"S": 0, "M": 1, "L": 2, "XL": 3}


@dataclass(frozen=True)
class _SizeProfile:
    """Structural resource quantities for one `random_spec` size tier.

    Every quantity here is a fixed constant for a given size - only content
    (CIDRs, AZ letters, instance types, subnet assignment, bucket name
    hints) is drawn from the seeded RNG. That keeps a size's total resource
    count exactly reproducible independent of seed, which is what lets the
    XL budget test assert a fixed range unconditionally.
    """

    n_vpcs: int
    subnets_per_vpc: int
    n_instance_groups: int
    instances_per_group: int
    n_database_groups: int
    databases_per_group: int
    n_bucket_groups: int
    buckets_per_group: int
    n_queue_groups: int
    queues_per_group: int
    n_role_groups: int
    roles_per_group: int


_SIZE_PROFILES: dict[str, _SizeProfile] = {
    "S": _SizeProfile(
        n_vpcs=1,
        subnets_per_vpc=2,
        n_instance_groups=2,
        instances_per_group=1,
        n_database_groups=1,
        databases_per_group=1,
        n_bucket_groups=2,
        buckets_per_group=1,
        n_queue_groups=1,
        queues_per_group=1,
        n_role_groups=1,
        roles_per_group=1,
    ),
    "M": _SizeProfile(
        n_vpcs=5,
        subnets_per_vpc=2,
        n_instance_groups=10,
        instances_per_group=1,
        n_database_groups=5,
        databases_per_group=1,
        n_bucket_groups=10,
        buckets_per_group=1,
        n_queue_groups=5,
        queues_per_group=1,
        n_role_groups=5,
        roles_per_group=1,
    ),
    "L": _SizeProfile(
        n_vpcs=25,
        subnets_per_vpc=2,
        n_instance_groups=50,
        instances_per_group=1,
        n_database_groups=25,
        databases_per_group=1,
        n_bucket_groups=50,
        buckets_per_group=1,
        n_queue_groups=25,
        queues_per_group=1,
        n_role_groups=25,
        roles_per_group=1,
    ),
    # XL breaks the S xN pattern on purpose: a realistic 10k-resource
    # estate keeps networking constructs (vpcs, subnets) small while
    # compute/storage/messaging resources dominate via `count` - which is
    # exactly what `count` is for: representing many like-shaped resources
    # without needing thousands of list entries. Total = 20+60+2400+300+
    # 2000+2400+2400 = 9,580, comfortably inside the documented
    # [8_000, 15_000] budget.
    "XL": _SizeProfile(
        n_vpcs=20,
        subnets_per_vpc=3,
        n_instance_groups=40,
        instances_per_group=60,
        n_database_groups=20,
        databases_per_group=15,
        n_bucket_groups=40,
        buckets_per_group=50,
        n_queue_groups=30,
        queues_per_group=80,
        n_role_groups=30,
        roles_per_group=80,
    ),
}


def random_spec(size: Literal["S", "M", "L", "XL"], seed: int) -> EnvSpec:
    """Generate a deterministic, seeded EnvSpec at one of four size tiers.

    Determinism is the contract: the same (size, seed) always produces a
    fully equal EnvSpec, in any process, forever. Seeded from
    ``hash((_SIZE_ORDINAL[size], seed))``, not ``hash((size, seed))``
    directly - see the comment on `_SIZE_ORDINAL` for why.

    Resource *quantities* are fixed per size (`_SIZE_PROFILES`); only
    *content* (CIDRs, AZ letters, instance types, subnet assignment, bucket
    name hints) is drawn from the seed. Approximate composition - vpcs /
    subnets / instances / databases / buckets / queues / roles (total):
    S 1/2/2/1/2/1/1 (10); M 5/10/10/5/10/5/5 (50, 5x S);
    L 25/50/50/25/50/25/25 (250, 25x S); XL 20/60/2400/300/2000/2400/2400
    (9,580 - see `_SIZE_PROFILES` for why XL breaks the xN pattern).

    Exact counts are the generator's business and may change; determinism
    and the documented XL budget ([8_000, 15_000] total, see
    `total_resource_count`) are the contract.
    """
    if size not in _SIZE_PROFILES:
        raise ValueError(f"unknown size {size!r}; expected one of {sorted(_SIZE_PROFILES)}")

    profile = _SIZE_PROFILES[size]
    rng = random.Random(hash((_SIZE_ORDINAL[size], seed)))

    vpcs = [_random_vpc(rng, profile.subnets_per_vpc) for _ in range(profile.n_vpcs)]
    subnet_count = sum(len(vpc.subnets) for vpc in vpcs)

    instances = [
        InstanceSpec(
            subnet_index=rng.randrange(subnet_count),
            instance_type=rng.choice(_INSTANCE_TYPES),
            count=profile.instances_per_group,
        )
        for _ in range(profile.n_instance_groups)
    ]
    databases = [
        DatabaseSpec(
            engine=rng.choice(_DB_ENGINES),
            instance_class=rng.choice(_DB_INSTANCE_CLASSES),
            count=profile.databases_per_group,
        )
        for _ in range(profile.n_database_groups)
    ]
    buckets = [
        BucketSpec(
            name_hint=f"{rng.choice(_BUCKET_WORDS)}-{i}",
            count=profile.buckets_per_group,
        )
        for i in range(profile.n_bucket_groups)
    ]
    queues = [QueueSpec(count=profile.queues_per_group) for _ in range(profile.n_queue_groups)]
    roles = [RoleSpec(count=profile.roles_per_group) for _ in range(profile.n_role_groups)]

    # `seed` is an unbounded int (any sign, any number of digits); `%` with
    # a positive modulus always yields a non-negative result in Python, so
    # this stays within _NAME_PREFIX_PATTERN's shape and length no matter
    # how large or negative the caller's seed is.
    name_suffix = seed % 100_000
    return EnvSpec(
        name_prefix=f"foundry-{size.lower()}-{name_suffix}",
        vpcs=vpcs,
        instances=instances,
        databases=databases,
        buckets=buckets,
        queues=queues,
        roles=roles,
    )


def _random_vpc(rng: random.Random, subnet_count: int) -> VpcSpec:
    """One vpc with `subnet_count` subnets, each in its OWN availability zone.

    `az_suffix` is assigned by subnet POSITION (`_AZ_SUFFIXES[i]`), not
    drawn independently per subnet via `rng.choice` as an earlier version
    of this function did - that could, and empirically did for 69/200
    S-profile seeds checked while diagnosing a live acceptance failure,
    draw the SAME az_suffix for two subnets in the same vpc. RDS DB
    subnet groups require their subnets to span >=2 distinct AZs (a real
    AWS constraint - `InvalidVPCNetworkStateFault` otherwise, not a floci
    quirk), and `materialize._db_subnet_group_blocks` depends on this
    invariant to ever emit an appliable spec whenever it has a database
    (see docs/DECISIONS.md entry 005, which also records this as an
    accepted determinism-content change per entry 003's own precedent).

    Every current `_SIZE_PROFILES` entry uses `subnets_per_vpc` of 2 or 3,
    never more than `len(_AZ_SUFFIXES)` (3) - so this never needs to
    repeat an AZ letter today. A future profile needing more subnets per
    vpc than `_AZ_SUFFIXES` has entries would need to widen that tuple
    first; this function does not silently wrap around and repeat one.
    """
    octet = rng.randint(0, 255)
    subnets = [
        SubnetSpec(cidr=f"10.{octet}.{subnet_idx}.0/24", az_suffix=_AZ_SUFFIXES[subnet_idx])
        for subnet_idx in range(subnet_count)
    ]
    return VpcSpec(cidr=f"10.{octet}.0.0/16", subnets=subnets)
