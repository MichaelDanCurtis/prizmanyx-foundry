"""EnvState: what the engine knows about one environment, persisted to disk.

Persisted at ``~/.prizmanyx-foundry/envs/<name>/state.json`` inside a 0o700
directory that also holds a copy of the spec that created the environment
(and, in later tasks, its terraform workdir). This module owns
environment-name identity: a name that becomes a container name
(`floci-<name>`) and a filesystem path is exactly the kind of string the
Destination Architect injection lesson applies to from birth (see the
foundry plan's Global Constraints) - so its shape is validated in the one
function every path-building call in this module goes through, not merely
trusted in from a caller that remembered to check first.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

# Env NAMES are a distinct, engine-level rule from EnvSpec's name_prefix
# pattern (envspec.py's _NAME_PREFIX_PATTERN caps at 25 characters and feeds
# terraform interpolation) - this one feeds container names and filesystem
# paths, sized per the foundry plan's Global Constraints: up to 41
# characters total, lowercase-start, lowercase/digit/hyphen body.
ENV_NAME_PATTERN = r"^[a-z][a-z0-9-]{0,40}$"
_ENV_NAME_RE = re.compile(ENV_NAME_PATTERN)

# Floci's real-engine services (RDS, EC2, ElastiCache) back their emulated
# resources with their own containers, named after the resource's own
# identifier, that survive `docker rm` of the floci parent (spike-proven,
# 2026-08-30: `floci-rds-db-*` and `floci-ec2-i-*` both survived teardown as
# running orphans - see engine.py's `select_children_to_reap`, the actual
# consumer of this list for its reap sweep).
#
# Defined HERE rather than in engine.py because this module's own name
# validation must know it too: an environment named e.g. `rds-x` would get
# a parent container `floci-rds-x` that collides with `floci-rds-` by pure
# coincidence of the `floci-<name>` join, and engine.py's reap sweep would
# then have no way to tell that live parent container apart from a real
# emulated-RDS child by name alone (this is exactly how a real bug shipped:
# a fix-round review reproduced a peer environment's own parent container
# being reaped because of this collision). One list, so the reap patterns
# and the naming rule that avoids colliding with them can never drift
# apart.
CHILD_NAME_PREFIXES: tuple[str, ...] = ("floci-rds-", "floci-ec2-", "floci-elasticache-")

# Every entry above is "floci-" + a stem (e.g. "rds-"). Derived, not
# hand-duplicated, so this and CHILD_NAME_PREFIXES can never drift apart.
_CHILD_PREFIX_STEMS: tuple[str, ...] = tuple(
    prefix.removeprefix("floci-") for prefix in CHILD_NAME_PREFIXES
)

# The floci-az (Azure control plane) container prefix - `engine.az_container_name`
# joins it as `floci-az-<name>`. Defined HERE, and imported by engine.py, for
# the SAME reason CHILD_NAME_PREFIXES is: this module's name validation must
# forbid the `az-` env-name stem. An env named e.g. `az-foo` gets its AWS
# parent container `floci-<name>` = `floci-az-foo` - BYTE-IDENTICAL to env
# `foo`'s Azure parent `floci-az-foo`. That collision is catastrophic at
# CREATE time, not just at reap: `create_env`'s stale-container clear
# (`docker rm -f floci-az-foo`) would delete a DIFFERENT, live env's healthy
# Azure container. Reject the name shape at the boundary before any docker
# call. One source of truth, so the container-naming prefix and the naming
# rule that avoids colliding with it can never drift apart.
AZ_CONTAINER_PREFIX = "floci-az-"

# The full set of env-name stems that are forbidden because `floci-<name>`
# would collide with a RESERVED floci container namespace: the child-container
# prefixes (rds-/ec2-/elasticache-) AND the az parent prefix (`az-`). Derived
# from the prefixes themselves so it cannot drift from them.
_FORBIDDEN_NAME_STEMS: tuple[str, ...] = (
    *_CHILD_PREFIX_STEMS,
    AZ_CONTAINER_PREFIX.removeprefix("floci-"),
)

FOUNDRY_HOME = Path.home() / ".prizmanyx-foundry"
ENVS_DIR = FOUNDRY_HOME / "envs"


class InvalidEnvNameError(ValueError):
    """Raised when a candidate environment name does not match ENV_NAME_PATTERN
    or would collide with a reserved floci container-name prefix (a child
    prefix like `rds-`, or the `az-` Azure-parent prefix)."""


def validate_env_name(name: str) -> None:
    """Raise InvalidEnvNameError if `name` is not a safe environment name.

    Two independent checks: `name` must match ENV_NAME_PATTERN, AND `name`
    must not start with any `_FORBIDDEN_NAME_STEMS` entry - a child-container
    stem (e.g. `rds-`) OR the `az-` Azure-parent stem - because in either
    case `floci-<name>` (this environment's own AWS parent container) would
    collide by name with a RESERVED floci container namespace:

    - a child stem (`rds-`/`ec2-`/`elasticache-`): `floci-<name>` looks
      like one of floci's emulated-resource backing containers, and
      engine.py's child-reap sweep could mistake this live parent container
      for an orphan (see CHILD_NAME_PREFIXES's comment and
      `engine.select_children_to_reap`).
    - the `az-` stem: `floci-az-<rest>` is byte-identical to some OTHER
      env's Azure parent container (`engine.az_container_name`). This one
      bites at CREATE time - `create_env`'s stale-container clear would
      `docker rm -f` a different, live env's healthy floci-az container.
      This unconditional boundary refusal is the fix for that collision.

    Called from `engine.create_env` before anything else happens (fail
    fast, before a port is allocated or a container started) and again
    here, by every function in this module that turns a name into a
    filesystem path - belt and suspenders against a name ever reaching a
    shell command or a path join unchecked. Note "starts with", not
    "contains": `azure-demo`, `azfoo`, and a bare `az` are all safe - only
    the `az-` PREFIX yields the `floci-az-` collision.
    """
    if not _ENV_NAME_RE.fullmatch(name):
        raise InvalidEnvNameError(
            f"invalid environment name {name!r}: must match {ENV_NAME_PATTERN}"
        )
    if name.startswith(_FORBIDDEN_NAME_STEMS):
        raise InvalidEnvNameError(
            f"invalid environment name {name!r}: must not start with any of "
            f"{_FORBIDDEN_NAME_STEMS!r} (floci-{name} would collide with a reserved "
            "floci container-name prefix - a child container - CHILD_NAME_PREFIXES - "
            "or the floci-az- Azure-parent prefix, and could delete or mis-reap "
            "another environment's container)"
        )


def env_dir(name: str) -> Path:
    """The state directory for environment `name` (validates `name` first)."""
    validate_env_name(name)
    return ENVS_DIR / name


def state_file(name: str) -> Path:
    """The state.json path for environment `name` (validates `name`, via env_dir)."""
    return env_dir(name) / "state.json"


class EnvState(BaseModel):
    """Everything the engine persists about one environment.

    `ports` maps service name to host port. An AWS-only environment carries
    just `{"aws": 15600}`; an Azure-enabled one also carries `{"az": 14577}`
    (the fixed floci-az port - see `engine.floci_az_port`). The key space
    stays open; `az` is not special-cased in the schema, just another entry.
    `spec_path` points at the copy of the
    EnvSpec `create_env` wrote into this environment's own directory, not
    the caller's original file, so the environment's record stays
    self-contained even if the caller's original spec file later moves or
    changes.

    `created_at` is stamped by `create_env` AFTER the container reports
    healthy, not at the moment `docker run` was issued - it can trail the
    container's own `docker ps`/`docker inspect` creation timestamp by
    however long the healthcheck took (seconds to tens of seconds).
    Anything that compares this field against a `docker ps` CreatedAt value
    (engine.py's reap sweep does exactly that) must not assume the two
    clocks agree to the second - a fix-round review reproduced a live
    environment's own parent container being reaped precisely because a
    timing comparison assumed they did. Treat the two as "this
    environment's own recorded existence" versus "this container's own
    recorded existence", never as interchangeable.

    `children` is the ownership-by-recording list: `tf_runner.apply`
    snapshots `docker ps` (running, `floci-`-prefixed) immediately before
    and immediately after each real `terraform apply`, and the names that
    only appear in the second snapshot - this apply's own floci real-engine
    children (RDS/EC2/ElastiCache backing containers, see
    `CHILD_NAME_PREFIXES`) - are unioned in here and persisted. `None`
    means this environment has never had a recorded apply - either it
    predates this field, or `create_env` made it but nothing has been
    applied to it yet - and `engine.remove_env` falls back to the
    `created_at`-based heuristic (`select_children_to_reap`) for those. An
    empty list is a different, authoritative statement: an apply WAS
    recorded and it spawned zero children. `engine.remove_env` treats
    non-None as the primary, exact reap list - see its own docstring.
    """

    name: str
    created_at: datetime
    ports: dict[str, int]
    spec_path: Path
    seed: int | None = None
    status: str = "running"
    children: list[str] | None = None


def ensure_env_dir(name: str) -> Path:
    """Create environment `name`'s state directory (0o700) if it does not
    exist yet, and return its path either way."""
    directory = env_dir(name)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)  # mkdir's mode is umask-masked; chmod makes 0o700 exact.
    return directory


def save_state(env_state: EnvState) -> Path:
    """Write `env_state` to its state.json, creating the 0o700 directory first.

    Returns the path written.
    """
    ensure_env_dir(env_state.name)
    path = state_file(env_state.name)
    path.write_text(env_state.model_dump_json(indent=2))
    return path


def load_state(name: str) -> EnvState:
    """Read and parse the state.json for environment `name`.

    Raises FileNotFoundError if the environment is not tracked, and
    pydantic's own ValidationError if a tracked state.json is malformed -
    neither is caught here, both are informative on their own.
    """
    return EnvState.model_validate_json(state_file(name).read_text())


def list_envs() -> list[EnvState]:
    """Every environment currently tracked, sorted by name.

    "Tracked" means the environment's state directory contains a
    state.json file - the same definition `engine.remove_env`'s
    child-ownership check relies on for "still live". A corrupt state.json
    raises (via `load_state`) rather than being silently skipped.
    """
    if not ENVS_DIR.is_dir():
        return []
    names = sorted(
        p.name for p in ENVS_DIR.iterdir() if p.is_dir() and (p / "state.json").is_file()
    )
    return [load_state(name) for name in names]


def delete_env_dir(name: str) -> None:
    """Remove environment `name`'s entire state directory, if present.

    A no-op if the directory does not exist - callers that already know an
    environment may be only partially set up (or that removal is being
    retried) should not have to check first.
    """
    directory = env_dir(name)
    if directory.is_dir():
        shutil.rmtree(directory)
