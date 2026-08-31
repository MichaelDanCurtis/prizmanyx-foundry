"""Spec -> Terraform materializer: turns one EnvSpec into a single,
deterministic ``main.tf`` that applies against ITS OWN floci endpoint.

Every resource this module can emit is named deterministically from
``spec.name_prefix`` (already boundary-validated at the EnvSpec edge - see
envspec.py's own comment on ``_NAME_PREFIX_PATTERN``) plus a fixed resource
kind and its list position - never from free-form spec content (name_hint,
instance_type, cidr, az_suffix), none of which EnvSpec itself constrains.
That content still reaches emitted HCL, but only ever inside a quoted
string VALUE, run through `_hcl_string` (json.dumps-based escaping) first -
the emitter-side half of the plan's two-sided injection lesson (EnvSpec's
own boundary validation, where it exists, is the other half). A resource
LABEL built only from already-safe parts can never be used to break out of
one resource block into a hand-crafted next one, regardless of what a
hand-edited spec.yaml puts in a free-form field.
"""

from __future__ import annotations

import json

from foundry.envspec import EnvSpec
from foundry.state import EnvState

# Every backend the generator can currently emit resources for. Always
# listed in the provider's `endpoints {}` block, regardless of whether this
# particular spec happens to use all of them - deterministic output must
# not vary with spec content, only with name_prefix/ports.
_ENDPOINT_SERVICES = ("ec2", "iam", "rds", "s3", "sqs")

# floci does not validate AMI existence (confirmed by the 2026-08-30 probe
# in docs/DECISIONS.md, which seeded instances with this same id) - any
# syntactically AMI-shaped string would do, but reusing the probe's own
# value keeps this materializer's output consistent with prior art.
_DEFAULT_AMI = "ami-0c55b159cbfafe1f0"

_DEFAULT_ENGINE_VERSION: dict[str, str] = {"postgres": "15", "mysql": "8.0"}
_DEFAULT_DB_USERNAME = "foundry"
_DEFAULT_DB_PASSWORD = "foundry-test-password"  # floci's fake RDS, not a real credential.


def _hcl_string(value: str) -> str:
    """A double-quoted HCL string literal for `value`, safely escaped.

    Two independent passes, because they close two different gaps:

    1. `json.dumps` for backslash/double-quote/control-character escaping.
       JSON's string-escaping rules are a compatible match for HCL's own
       for exactly these characters, so this pass alone stops a hostile
       value from closing the string literal early and injecting new HCL
       syntax after it (an earlier version of this docstring called this
       a "superset" match and stopped there - true as far as it went, but
       incomplete: see pass 2).
    2. Neutralizing HCL's OWN template syntax within the string's
       content - `${...}` (interpolation) and `%{...}` (a directive, e.g.
       `%{if ...}...%{endif}`) are special to HCL even INSIDE a properly
       quoted, properly escaped string literal. `json.dumps` has no idea
       these exist, since they are not JSON syntax at all - a value
       containing a literal `${aws_iam_role.x.arn}` would have terraform
       evaluate that expression for real once applied, a live
       cross-resource reference, not inert text, which is a materially
       different (and worse) injection than an early string close. Each
       occurrence is escaped to HCL's own documented literal form -
       `$${` for a literal dollar-brace, `%%{` for a literal
       percent-brace - exactly as an operator writing raw HCL by hand
       would to keep a stray `$` or `%` next to a brace inert.

    This is the emitter-side defense for every free-form spec field
    (name_hint, instance_type, cidr, az_suffix) EnvSpec does not itself
    boundary-validate. Never used to build a resource LABEL - see this
    module's own docstring for why labels stay clear of this content
    entirely instead of merely escaping it.
    """
    escaped = json.dumps(value)
    return escaped.replace("${", "$${").replace("%{", "%%{")


def _application_tag_line(spec: EnvSpec) -> str:
    """The `Application = "<name_prefix>"` tag line every taggable
    resource's own `tags` block includes.

    Not cosmetic: `prizmanyx_discovery.migration_units.GROUP_TAG_KEYS`
    (Application/application/App/app/Workload/workload/Service/service)
    is what seeds Prizmanyx's DETERMINISTIC migration-unit inference - a
    graph node whose extracted `properties["tags"]` carries none of those
    keys with a non-empty value joins NO migration unit at all
    (`_group_signal` in that module returns `None`). A materialized
    estate with no such tag on any resource yields zero units, and the
    loop's exit gate can never pass (`units_approved` has nothing to
    approve) - this is exactly what a live Task 8 acceptance run hit: 19
    graph nodes, 0 units. Every taggable resource this generator emits
    must carry this tag for a materialized estate to actually be
    migratable, not merely extractable.

    Every resource gets the SAME value - `spec.name_prefix` - on purpose:
    the inference groups candidate nodes by tag VALUE, so one shared
    value joins every resource in this spec into ONE coherent migration
    unit (which also pulls in hard-dependency neighbors via the
    inference's own edge-walking) - the realistic "one small estate, one
    application" shape this generator's specs already model. Modeling
    more than one application per estate is deliberately out of scope for
    slice 1-3; `name_prefix` is already boundary-validated
    (`_NAME_PREFIX_PATTERN`), so it reaches this tag value safely.
    """
    return f"    Application = {_hcl_string(spec.name_prefix)}"


def _slug(text: str) -> str:
    """Lowercase `text` down to `[a-z0-9-]`, collapsing everything else to
    a single hyphen - used only for the human-readable half of an S3
    bucket name (`name_hint`), never for a resource label. AWS bucket
    names are themselves restricted to lowercase alphanumerics and
    hyphens, so this doubles as the AWS-shape fix and the HCL-safety fix
    in one pass. Falls back to a fixed literal if `text` slugifies to
    nothing at all (e.g. it was empty, or entirely punctuation).
    """
    slugged = "".join(char if char.isascii() and char.isalnum() else "-" for char in text.lower())
    while "--" in slugged:
        slugged = slugged.replace("--", "-")
    slugged = slugged.strip("-")
    return slugged or "x"


def _provider_block(port: int) -> str:
    endpoint_lines = "\n".join(
        f'    {service} = "http://host.docker.internal:{port}"' for service in _ENDPOINT_SERVICES
    )
    return (
        "terraform {\n"
        "  required_providers {\n"
        "    aws = {\n"
        '      source  = "hashicorp/aws"\n'
        '      version = "~> 5.0"\n'
        "    }\n"
        "  }\n"
        "}\n"
        "\n"
        'provider "aws" {\n'
        '  access_key                  = "test"\n'
        '  secret_key                  = "test"\n'
        '  region                      = "us-east-1"\n'
        "  s3_use_path_style           = true\n"
        "  skip_credentials_validation = true\n"
        "  skip_metadata_api_check     = true\n"
        "  skip_requesting_account_id  = true\n"
        "\n"
        "  endpoints {\n"
        f"{endpoint_lines}\n"
        "  }\n"
        "}"
    )


def _vpc_blocks(spec: EnvSpec) -> list[str]:
    blocks = []
    for index, vpc in enumerate(spec.vpcs):
        label = f"{spec.name_prefix}-vpc-{index}"
        blocks.append(
            f'resource "aws_vpc" "{label}" {{\n'
            f"  cidr_block = {_hcl_string(vpc.cidr)}\n"
            "\n"
            "  tags = {\n"
            f"{_application_tag_line(spec)}\n"
            "  }\n"
            "}"
        )
    return blocks


def _subnet_blocks(spec: EnvSpec) -> list[str]:
    """One block per subnet, flattened across every vpc in vpc-then-subnet
    order - the exact same flattening `InstanceSpec.subnet_index` already
    assumes (see its own docstring), so `instance.subnet_index` can address
    a subnet label directly with no re-derivation.
    """
    blocks = []
    flat_index = 0
    for vpc_index, vpc in enumerate(spec.vpcs):
        vpc_label = f"{spec.name_prefix}-vpc-{vpc_index}"
        for subnet in vpc.subnets:
            label = f"{spec.name_prefix}-subnet-{flat_index}"
            availability_zone = f"us-east-1{subnet.az_suffix}"
            blocks.append(
                f'resource "aws_subnet" "{label}" {{\n'
                f"  vpc_id            = aws_vpc.{vpc_label}.id\n"
                f"  cidr_block        = {_hcl_string(subnet.cidr)}\n"
                f"  availability_zone = {_hcl_string(availability_zone)}\n"
                "\n"
                "  tags = {\n"
                f"{_application_tag_line(spec)}\n"
                "  }\n"
                "}"
            )
            flat_index += 1
    return blocks


def _instance_blocks(spec: EnvSpec) -> list[str]:
    blocks = []
    for index, instance in enumerate(spec.instances):
        label = f"{spec.name_prefix}-instance-{index}"
        subnet_label = f"{spec.name_prefix}-subnet-{instance.subnet_index}"
        name_tag = f"{spec.name_prefix}-instance-{index}-${{count.index}}"
        blocks.append(
            f'resource "aws_instance" "{label}" {{\n'
            f"  count         = {instance.count}\n"
            f"  ami           = {_hcl_string(_DEFAULT_AMI)}\n"
            f"  instance_type = {_hcl_string(instance.instance_type)}\n"
            f"  subnet_id     = aws_subnet.{subnet_label}.id\n"
            "\n"
            "  tags = {\n"
            f'    Name = "{name_tag}"\n'
            f"{_application_tag_line(spec)}\n"
            "  }\n"
            "}"
        )
    return blocks


def _database_blocks(spec: EnvSpec) -> list[str]:
    blocks = []
    for index, database in enumerate(spec.databases):
        label = f"{spec.name_prefix}-database-{index}"
        identifier = f"{spec.name_prefix}-database-{index}-${{count.index}}"
        engine_version = _DEFAULT_ENGINE_VERSION[database.engine]
        blocks.append(
            f'resource "aws_db_instance" "{label}" {{\n'
            f"  count               = {database.count}\n"
            f'  identifier          = "{identifier}"\n'
            f"  engine              = {_hcl_string(database.engine)}\n"
            f"  engine_version      = {_hcl_string(engine_version)}\n"
            f"  instance_class      = {_hcl_string(database.instance_class)}\n"
            "  allocated_storage   = 20\n"
            f"  username            = {_hcl_string(_DEFAULT_DB_USERNAME)}\n"
            f"  password            = {_hcl_string(_DEFAULT_DB_PASSWORD)}\n"
            "  skip_final_snapshot = true\n"
            "\n"
            "  tags = {\n"
            f"{_application_tag_line(spec)}\n"
            "  }\n"
            "}"
        )
    return blocks


def _bucket_blocks(spec: EnvSpec) -> list[str]:
    blocks = []
    for index, bucket in enumerate(spec.buckets):
        label = f"{spec.name_prefix}-bucket-{index}"
        # `_slug` already restricts itself to `[a-z0-9-]` - safe to splice
        # straight into the f-string with no further escaping.
        bucket_name = f"{spec.name_prefix}-{_slug(bucket.name_hint)}-{index}-${{count.index}}"
        blocks.append(
            f'resource "aws_s3_bucket" "{label}" {{\n'
            f"  count  = {bucket.count}\n"
            f'  bucket = "{bucket_name}"\n'
            "\n"
            "  tags = {\n"
            f"{_application_tag_line(spec)}\n"
            "  }\n"
            "}"
        )
    return blocks


def _queue_blocks(spec: EnvSpec) -> list[str]:
    blocks = []
    for index, queue in enumerate(spec.queues):
        label = f"{spec.name_prefix}-queue-{index}"
        queue_name = f"{spec.name_prefix}-queue-{index}-${{count.index}}"
        blocks.append(
            f'resource "aws_sqs_queue" "{label}" {{\n'
            f"  count = {queue.count}\n"
            f'  name  = "{queue_name}"\n'
            "\n"
            "  tags = {\n"
            f"{_application_tag_line(spec)}\n"
            "  }\n"
            "}"
        )
    return blocks


def _role_blocks(spec: EnvSpec) -> list[str]:
    blocks = []
    for index, role in enumerate(spec.roles):
        label = f"{spec.name_prefix}-role-{index}"
        role_name = f"{spec.name_prefix}-role-{index}-${{count.index}}"
        blocks.append(
            f'resource "aws_iam_role" "{label}" {{\n'
            f"  count = {role.count}\n"
            f'  name  = "{role_name}"\n'
            "\n"
            "  assume_role_policy = jsonencode({\n"
            '    Version = "2012-10-17"\n'
            "    Statement = [{\n"
            '      Action    = "sts:AssumeRole"\n'
            '      Effect    = "Allow"\n'
            '      Principal = { Service = "ec2.amazonaws.com" }\n'
            "    }]\n"
            "  })\n"
            "\n"
            "  tags = {\n"
            f"{_application_tag_line(spec)}\n"
            "  }\n"
            "}"
        )
    return blocks


def emit_terraform(spec: EnvSpec, env: EnvState) -> str:
    """Render `spec` as one main.tf, pointed at `env`'s own floci endpoint.

    Deterministic: the identical (spec, env) pair always produces the
    identical string, in any process, forever - every resource-generating
    helper above builds purely from `spec`'s own already-ordered lists and
    `env.ports`, with no randomness, no wall-clock, no filesystem or
    environment-variable reads. Callers (the pure emission tests, a loop
    driver's idempotent re-apply) depend on this, not just convenience.

    Every terraform resource type this module knows how to emit
    (aws_vpc, aws_subnet, aws_instance, aws_db_instance, aws_s3_bucket,
    aws_sqs_queue, aws_iam_role) gets one block per spec list entry,
    using terraform's own `count` meta-argument for that entry's `count`
    field rather than emitting `count` separate blocks - keeping main.tf
    sized to the number of spec GROUPS, not the number of individual AWS
    resources they expand to (relevant once `random_spec("XL", ...)`
    groups reach counts in the tens or hundreds).
    """
    port = env.ports["aws"]
    sections = [
        _provider_block(port),
        *_vpc_blocks(spec),
        *_subnet_blocks(spec),
        *_instance_blocks(spec),
        *_database_blocks(spec),
        *_bucket_blocks(spec),
        *_queue_blocks(spec),
        *_role_blocks(spec),
    ]
    return "\n\n".join(sections) + "\n"
