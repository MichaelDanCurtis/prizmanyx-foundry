"""The environment engine: docker lifecycle + on-disk state for one
Floci-backed AWS environment.

Every docker-touching function goes through `run_docker` - the one seam
between this module and the real `docker` binary (subprocess) - so tests
can monkeypatch a single call instead of shelling out for real. The one
genuinely pure piece, `select_children_to_reap`, does not even need that
seam: it takes already-parsed rows and decides, with no I/O of its own.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

import yaml

from foundry import state
from foundry.envspec import EnvSpec
from foundry.state import EnvState

# Global Constraints: the spike-validated digest, overridable only through
# this named escape hatch - never trust an unpinned `floci/floci:latest`.
DEFAULT_FLOCI_IMAGE = (
    "floci/floci@sha256:bec9b9f749322444a57fb0f00d3e007ffd49015162361aab84ccecd9b4f5f8ed"
)
FLOCI_IMAGE_ENV_VAR = "FOUNDRY_FLOCI_IMAGE"

PORT_RANGE_START = 15600
PORT_RANGE_END = 16000  # exclusive; the allocatable range is [15600, 15999]

HEALTHY_TIMEOUT_SECONDS = 60.0
HEALTH_POLL_INTERVAL_SECONDS = 1.0

# Floci's real-engine services back RDS/EC2/ElastiCache with their own
# containers that outlive `docker rm` of the floci parent - spike-proven,
# 2026-08-30: `floci-rds-db-*` and `floci-ec2-i-*` both survived teardown as
# running orphans. Prefix-matched, not exact: floci names each child after
# the emulated resource's own identifier, which this list can't predict.
CHILD_NAME_PREFIXES: tuple[str, ...] = ("floci-rds-", "floci-ec2-", "floci-elasticache-")


class EngineError(Exception):
    """Base class for environment-engine failures."""


class EnvAlreadyExistsError(EngineError):
    """Raised by `create_env` when an environment with this name is already tracked."""


class EnvNotFoundError(EngineError):
    """Raised when an operation names an environment with no tracked state."""


class EnvStartError(EngineError):
    """Raised when a newly started container never reports healthy in time."""


def run_docker(
    args: list[str], *, timeout: float | None = 300.0
) -> subprocess.CompletedProcess[str]:
    """Run the `docker` CLI with `args`, capturing stdout/stderr as text.

    The one seam between this module and the real docker binary: every
    docker invocation anywhere in this file goes through here, so tests can
    monkeypatch this single function instead of shelling out for real.
    Never raises on a non-zero exit (`check=False`) - callers decide what a
    failure means for them (a missing container is a normal outcome for
    some callers, a fatal one for others).

    The default timeout is generous (5 minutes), not snappy, because
    `docker run` on a cold machine blocks on pulling the image first - a
    tight timeout here would fail a plain first-ever `create_env` on a slow
    connection. It still exists to catch a genuinely wedged daemon rather
    than hanging forever.
    """
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False, timeout=timeout
    )


def floci_image() -> str:
    """The floci image reference to run: the digest pin, unless the
    operator has deliberately overridden it via FOUNDRY_FLOCI_IMAGE."""
    return os.environ.get(FLOCI_IMAGE_ENV_VAR, DEFAULT_FLOCI_IMAGE)


def container_name(name: str) -> str:
    """The docker container name for environment `name`."""
    return f"floci-{name}"


def allocate_port(start: int = PORT_RANGE_START, end: int = PORT_RANGE_END) -> int:
    """Find a free host port in `[start, end)` by actually probing binds.

    A real `socket.bind` attempt against each candidate in turn, not merely
    "not currently tracked by a foundry env" - so a port some other process
    already holds is correctly skipped too. This is inherently a
    check-then-use race (nothing stops another process from grabbing the
    port between this call returning and `docker run -p` using it);
    acceptable for a local, single-operator dev tool.
    """
    for candidate in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("", candidate))
            except OSError:
                continue
            return candidate
    raise RuntimeError(f"no free port available in [{start}, {end})")


def _container_health(container: str) -> str:
    """Docker's live health verdict for `container`.

    "healthy" / "unhealthy" / "starting" mirror floci's own image
    HEALTHCHECK (confirmed present via the 2026-08-30 spike and Task 1's
    probe - `docker ps` showed `(healthy)` once floci finished booting).
    "not_found" covers a container that does not exist (never created, or
    already removed).
    """
    result = run_docker(["inspect", "--format", "{{.State.Health.Status}}", container])
    if result.returncode != 0:
        return "not_found"
    return result.stdout.strip()


def _wait_healthy(container: str, timeout: float = HEALTHY_TIMEOUT_SECONDS) -> None:
    """Poll `container` until it reports healthy, or raise EnvStartError.

    Only the control-plane container's own boot is being waited on here -
    floci's real-engine services (an RDS instance's backing postgres, etc.)
    start on demand later and are not part of this wait.
    """
    deadline = time.monotonic() + timeout
    last_status = "unknown"
    while time.monotonic() < deadline:
        last_status = _container_health(container)
        if last_status == "healthy":
            return
        if last_status == "not_found":
            raise EnvStartError(f"container {container!r} disappeared while starting")
        time.sleep(HEALTH_POLL_INTERVAL_SECONDS)
    raise EnvStartError(
        f"container {container!r} did not become healthy within {timeout}s "
        f"(last status: {last_status!r})"
    )


def create_env(name: str, spec: EnvSpec, *, seed: int | None = None) -> EnvState:
    """Bring up a new Floci-backed environment and record it.

    Order matters for cleanliness on failure: validate the name (cheapest,
    fails before touching anything), check no environment of this name is
    already tracked, allocate a port, start the container, wait for it to
    report healthy, and only once all of that has succeeded, write the spec
    copy and state.json. A failure at any docker step removes the container
    it just started before raising - nothing left running, and nothing for
    `list_envs`/`remove_env` to find, so a retry under the same name starts
    clean rather than tripping over a failed attempt's leftovers.

    `seed` is recorded on EnvState verbatim when the caller generated
    `spec` via `random_spec` and wants that traceable; hand-written or
    from-repo specs have none.
    """
    state.validate_env_name(name)
    if state.state_file(name).is_file():
        raise EnvAlreadyExistsError(f"environment {name!r} already exists")

    port = allocate_port()
    container = container_name(name)
    # Best-effort: clear any stale container left by a prior attempt that
    # crashed between starting docker and writing state.json (state.json
    # missing is exactly the "not yet tracked" signal the check above
    # trusts) - without this, retrying under the same name would fail with
    # docker's own "name already in use" before ever reaching this env's
    # own healthcheck.
    run_docker(["rm", "-f", container])

    run_result = run_docker(
        [
            "run",
            "-d",
            "--name",
            container,
            "-p",
            f"{port}:4566",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            floci_image(),
        ]
    )
    if run_result.returncode != 0:
        run_docker(["rm", "-f", container])  # best-effort: clear any partial container object.
        raise EnvStartError(f"docker run failed for {container!r}: {run_result.stderr.strip()}")

    try:
        _wait_healthy(container)
    except (EnvStartError, subprocess.TimeoutExpired):
        # TimeoutExpired can surface from `run_docker` itself (a wedged
        # daemon mid-poll), not just EnvStartError's own "never went
        # healthy" verdict - both leave a container that must not survive
        # this failed attempt.
        run_docker(["rm", "-f", container])
        raise

    directory = state.ensure_env_dir(name)
    spec_path = directory / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(spec.model_dump()))

    env_state = EnvState(
        name=name,
        created_at=datetime.now(UTC),
        ports={"aws": port},
        spec_path=spec_path,
        seed=seed,
        status="running",
    )
    state.save_state(env_state)
    return env_state


def env_status(name: str) -> EnvState:
    """`name`'s persisted state, with `.status` refreshed from docker right now.

    Everything else (created_at, ports, spec_path, seed) is the immutable
    record `create_env` wrote; `status` alone reflects docker's live answer
    - "healthy" / "unhealthy" / "starting" / "not_found" - not
    last-persisted knowledge. Read-only: does not rewrite state.json.
    """
    try:
        persisted = state.load_state(name)
    except FileNotFoundError as exc:
        raise EnvNotFoundError(f"no tracked environment named {name!r}") from exc
    return persisted.model_copy(update={"status": _container_health(container_name(name))})


def list_envs() -> list[EnvState]:
    """Every environment currently tracked (state-only; does not probe docker)."""
    return state.list_envs()


@dataclass(frozen=True)
class ContainerRow:
    """One row of `docker ps -a` output: just enough to decide reaping.

    Kept separate from EnvState (rather than reusing it) so
    `select_children_to_reap` can be unit-tested against a canned table -
    no real docker process, no real environment, involved at all.
    """

    name: str
    created_at: datetime


def _is_floci_child(name: str) -> bool:
    return name.startswith(CHILD_NAME_PREFIXES)


def select_children_to_reap(
    candidates: Iterable[ContainerRow],
    target: EnvState,
    other_live_envs: Iterable[EnvState],
) -> list[ContainerRow]:
    """Which rows in `candidates` are `target`'s orphaned floci children.

    A row is reaped only if all three hold:

    1. Its name matches a floci child-name prefix (`CHILD_NAME_PREFIXES`) -
       `docker ps -a` lists every container on the host, including the
       untouchable compose stack, so this is the first gate.
    2. It was created after `target.created_at` - a child can't belong to
       an environment that didn't exist yet.
    3. No OTHER live environment came into existence in the window between
       `target`'s creation and the row's. If one did, that environment (not
       `target`) is the more recently created candidate parent, and
       reaping the row would reach into a still-running environment's real
       resource. `other_live_envs` may include `target` itself (callers are
       not required to filter it out first) - it is always excluded here.

    There is no docker label tying a child back to the specific floci
    parent that spawned it (checked directly against the spike's own
    `docker inspect` transcripts - floci sets none), so "created after the
    nearest still-live environment before it" is the best attribution
    available from `docker ps` output alone. It is exact whenever
    environments are created and removed one at a time, which is this
    tool's normal usage pattern; the failure mode when environments overlap
    in unusual ways is to under-reap (leave an orphan behind) rather than
    to ever delete a live environment's resource.
    """
    other_live = [env for env in other_live_envs if env.name != target.name]
    reap = []
    for row in candidates:
        if not _is_floci_child(row.name):
            continue
        if row.created_at <= target.created_at:
            continue
        if any(target.created_at < other.created_at <= row.created_at for other in other_live):
            continue
        reap.append(row)
    return reap


_DOCKER_PS_FORMAT = "{{.Names}}\t{{.CreatedAt}}"
# Docker's `ps` CreatedAt column has a fixed layout across versions -
# "2021-09-27 13:05:07 -0700 PDT" - but the trailing zone abbreviation is
# not reliably parseable by strptime's %Z (Python only recognizes
# abbreviations for the process's own locale, which need not match the
# container host's). The numeric UTC offset earlier in the string already
# carries the same information unambiguously, so only that prefix is
# captured; the abbreviation is discarded.
_DOCKER_CREATED_AT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ([+-]\d{4})")


def _parse_docker_created_at(raw: str) -> datetime:
    """Parse one `docker ps --format '{{.CreatedAt}}'` value into an aware datetime."""
    match = _DOCKER_CREATED_AT_RE.match(raw.strip())
    if not match:
        raise ValueError(f"unparseable docker ps CreatedAt: {raw!r}")
    return datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H:%M:%S %z")


def _list_floci_containers() -> list[ContainerRow]:
    """`docker ps -a` rows for every container whose name starts with
    `floci-` - a superset of both env parents and their children;
    `select_children_to_reap` does the precise filtering."""
    result = run_docker(["ps", "-a", "--filter", "name=floci-", "--format", _DOCKER_PS_FORMAT])
    rows = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        raw_name, _, raw_created = line.partition("\t")
        rows.append(ContainerRow(name=raw_name, created_at=_parse_docker_created_at(raw_created)))
    return rows


def remove_env(name: str) -> None:
    """Tear down environment `name` completely: parent container, every
    orphaned child it spawned, and its on-disk state.

    Order: remove the parent first (fast, always safe, best-effort - an
    already-gone parent is not fatal), THEN sweep for surviving children
    (which the spike found only matter once the parent's gone), THEN delete
    the state directory last. That ordering means a failure partway through
    leaves `list_envs`/`env_status` still able to see this environment and
    a retry able to pick up whatever this call did not finish, rather than
    an environment that vanished from tracking while containers of its own
    were still running.
    """
    try:
        target = state.load_state(name)
    except FileNotFoundError as exc:
        raise EnvNotFoundError(f"no tracked environment named {name!r}") from exc

    run_docker(["rm", "-f", container_name(name)])  # best-effort: already-gone is fine.

    # Includes `target` itself (its state.json is not deleted yet) -
    # select_children_to_reap excludes it from "other" on its own.
    other_live = state.list_envs()
    for row in select_children_to_reap(_list_floci_containers(), target, other_live):
        run_docker(["rm", "-f", row.name])

    state.delete_env_dir(name)
