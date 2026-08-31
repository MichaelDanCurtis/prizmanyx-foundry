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

FOUNDRY_HOME = Path.home() / ".prizmanyx-foundry"
ENVS_DIR = FOUNDRY_HOME / "envs"


class InvalidEnvNameError(ValueError):
    """Raised when a candidate environment name does not match ENV_NAME_PATTERN."""


def validate_env_name(name: str) -> None:
    """Raise InvalidEnvNameError if `name` does not match ENV_NAME_PATTERN.

    Called from `engine.create_env` before anything else happens (fail
    fast, before a port is allocated or a container started) and again
    here, by every function in this module that turns a name into a
    filesystem path - belt and suspenders against a name ever reaching a
    shell command or a path join unchecked.
    """
    if not _ENV_NAME_RE.fullmatch(name):
        raise InvalidEnvNameError(
            f"invalid environment name {name!r}: must match {ENV_NAME_PATTERN}"
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

    `ports` maps service name to allocated host port (`{"aws": 15600}`
    today; the key space is deliberately open for a future `"az"` entry
    once floci-az joins the engine). `spec_path` points at the copy of the
    EnvSpec `create_env` wrote into this environment's own directory, not
    the caller's original file, so the environment's record stays
    self-contained even if the caller's original spec file later moves or
    changes.
    """

    name: str
    created_at: datetime
    ports: dict[str, int]
    spec_path: Path
    seed: int | None = None
    status: str = "running"


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
