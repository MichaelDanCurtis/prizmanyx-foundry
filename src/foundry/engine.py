"""The environment engine: docker lifecycle + on-disk state for one
Floci-backed AWS environment.

Every docker-touching function goes through `run_docker` - the one seam
between this module and the real `docker` binary (subprocess) - so tests
can monkeypatch a single call instead of shelling out for real. The one
genuinely pure piece, `select_children_to_reap`, does not even need that
seam: it takes already-parsed rows and decides, with no I/O of its own.

`list_envs` here returns state only - nobody has asked docker anything.
Handoff note for whichever task builds `foundry ls`: get live status per
environment by calling `env_status(name)` for each name `list_envs()`
returns, one docker round trip per environment - there is no bulk
"status of everything" call, and `list_envs()`'s own `.status` field is
whatever was last persisted, not necessarily current.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import yaml

from foundry import state
from foundry.envspec import EnvSpec
from foundry.state import CHILD_NAME_PREFIXES, EnvState

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

# run_docker's default timeout: fits ps/inspect/rm, local daemon calls with
# no network I/O. `docker run` gets its own, much larger, timeout - see
# DOCKER_RUN_TIMEOUT_SECONDS - because it blocks on pulling the image on a
# cold machine, which a 30s timeout would fail spuriously.
DOCKER_QUICK_TIMEOUT_SECONDS = 30.0
DOCKER_RUN_TIMEOUT_SECONDS = 300.0

# `docker rm -f <missing container>` exits 0 (NOT an error) while writing
# this to stderr - verified directly against a real daemon (docker
# 29.6.2), which is what `remove_env`'s primary reap path keys off of to
# tell "genuinely removed" apart from "was already gone" (see its own
# docstring). Checking `returncode` alone cannot distinguish the two for
# `-f`: it is 0 either way.
_NO_SUCH_CONTAINER_MARKER = "No such container"


class EngineError(Exception):
    """Base class for environment-engine failures."""


class EnvAlreadyExistsError(EngineError):
    """Raised by `create_env` when an environment with this name is already tracked."""


class EnvNotFoundError(EngineError):
    """Raised when an operation names an environment with no tracked state."""


class EnvStartError(EngineError):
    """Raised when a newly started container never reports healthy in time."""


def run_docker(
    args: list[str], *, timeout: float | None = DOCKER_QUICK_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    """Run the `docker` CLI with `args`, capturing stdout/stderr as text.

    The one seam between this module and the real docker binary: every
    docker invocation anywhere in this file goes through here, so tests can
    monkeypatch this single function instead of shelling out for real.
    Never raises on a non-zero exit (`check=False`) - callers decide what a
    failure means for them (a missing container is a normal outcome for
    some callers, a fatal one for others).

    The default timeout (`DOCKER_QUICK_TIMEOUT_SECONDS`) fits ps/inspect/rm.
    `docker run` passes its own, much larger, `DOCKER_RUN_TIMEOUT_SECONDS`
    explicitly - see that constant for why.
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
    fails before touching anything - this also rejects a name that would
    collide with CHILD_NAME_PREFIXES; see `state.validate_env_name`), check
    no environment of this name is already tracked, allocate a port, start
    the container, wait for it to report healthy, and only once all of
    that has succeeded, write the spec copy and state.json. A failure at
    any docker step removes the container it just started before raising -
    nothing left running, and nothing for `list_envs`/`remove_env` to find,
    so a retry under the same name starts clean rather than tripping over a
    failed attempt's leftovers.

    `seed` is recorded on EnvState verbatim when the caller generated
    `spec` via `random_spec` and wants that traceable; hand-written or
    from-repo specs have none.

    NOTE on `created_at`: it is stamped here, after `_wait_healthy`
    returns - not at the moment `docker run` was issued. See EnvState's
    own docstring for why that gap matters to anything comparing it
    against a `docker ps` timestamp.
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
        ],
        timeout=DOCKER_RUN_TIMEOUT_SECONDS,
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
    """Every environment currently tracked (state-only; does not probe docker).

    See this module's own docstring for the `foundry ls` handoff note on
    getting live status per environment.
    """
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


@dataclass(frozen=True)
class ReapSelection:
    """The verdict of one child-reap sweep: which floci-child-prefixed rows
    to reap, and which to spare (see `select_children_to_reap`)."""

    reap: list[ContainerRow]
    spare: list[ContainerRow]


@dataclass(frozen=True)
class RemovalReport:
    """What `remove_env` actually did to `name`'s containers, by name.

    `reaped` - floci-child containers `remove_env` confirmed it actually
    removed. `spared` - floci-child-prefixed containers it saw and
    deliberately left alone: they might belong to another live
    environment, they predate `target`, or one literally IS another
    tracked environment's own parent container (see
    `select_children_to_reap`'s docstring for exactly which) - FALLBACK
    path only, always `[]` on the PRIMARY path (nothing is ambiguous when
    the exact list is already recorded). `unparsed` - `docker ps` rows
    whose CreatedAt this engine could not parse at all; left untouched
    rather than guessed at (see `_parse_docker_ps_output`) - also FALLBACK
    path only. `already_gone` - PRIMARY path only: recorded children whose
    `docker rm -f` reported the container already didn't exist (a prior
    `destroy()`, or a human, already removed it) - kept distinct from
    `reaped` rather than silently folded into it, so a caller can see this
    happened ("visible truth, not silence") instead of `reaped` echoing
    `target.children` regardless of what `docker rm -f` actually did.
    Always `[]` on the FALLBACK path, which never checks individual
    removal outcomes. Every list here holds container names, not
    environment names.
    """

    reaped: list[str]
    spared: list[str]
    unparsed: list[str]
    already_gone: list[str] = field(default_factory=list)


def _is_floci_child(name: str) -> bool:
    return name.startswith(CHILD_NAME_PREFIXES)


def select_children_to_reap(
    candidates: Iterable[ContainerRow],
    target: EnvState,
    other_live_envs: Iterable[EnvState],
) -> ReapSelection:
    """Classify every floci-child-prefixed row in `candidates` as `target`'s
    to reap, or one to spare. Rows that don't match a child prefix at all
    (the untouchable compose stack, unrelated containers) are ignored
    entirely - neither reaped nor reported as spared.

    A prefix-matching row is REAPED only if ALL of:

    1. Its name is not literally another tracked environment's own parent
       container (`floci-<other.name>` for some `other` in
       `other_live_envs`) - checked first, unconditionally, regardless of
       any timestamp. An environment whose NAME happens to start with a
       child stem (e.g. an env named `rds-x`, container `floci-rds-x`)
       collides with CHILD_NAME_PREFIXES by construction of the
       `floci-<name>` join. `state.validate_env_name` now refuses to
       create such a name, but this check is the second, clock-independent
       line of defense: it protects a live peer's own parent container
       even if one exists anyway (a legacy environment from before this
       validation existed, for instance) - and it is NOT redundant with
       rule 3 below: `EnvState.created_at` is stamped after that
       environment's own healthcheck passes (see EnvState's docstring),
       which can be LATER than that same environment's container's own
       `docker ps` CreatedAt, so a timing comparison against a peer's own
       row can come out backwards. Only this unconditional name check
       catches that case.
    2. It was created after `target.created_at` - a child can't belong to
       an environment that didn't exist yet.
    3. NO other live environment's `created_at` is earlier than the row's.

    Rule 3 is deliberately NOT "no other env came into existence between
    target and the row" - an earlier version of this function got exactly
    that wrong: it only excluded LATER-created environments from
    consideration, so an OLDER live environment was never treated as a
    candidate parent, and a peer's `remove_env` could reap that older
    environment's own still-being-spawned child right out from under it.
    The corrected rule's honest cost: once two or more environments are
    alive, a row created after `target` is spared the instant ANY other
    live environment predates it - in normal use, where environments are
    created one at a time, that is every other live environment, so
    nothing auto-reaps while 2+ environments are alive and any of them is
    older than the row. That conservatism is the point: there is no docker
    label tying a child back to the specific floci parent that spawned it
    (checked directly against the spike's own `docker inspect`
    transcripts - floci sets none), so reaping a live environment's real
    resource - not leaving an orphan a human (or a future prune command)
    can clean up later - is the failure this function exists to prevent.
    Spared rows are never deleted automatically, here or anywhere else;
    `remove_env` reports them (`RemovalReport.spared`) rather than acting
    on them.
    """
    other_live = [env for env in other_live_envs if env.name != target.name]
    other_parent_names = {container_name(env.name) for env in other_live}

    reap: list[ContainerRow] = []
    spare: list[ContainerRow] = []
    for row in candidates:
        if not _is_floci_child(row.name):
            continue
        if row.name in other_parent_names:
            spare.append(row)
            continue
        if row.created_at <= target.created_at:
            spare.append(row)
            continue
        if any(other.created_at < row.created_at for other in other_live):
            spare.append(row)
            continue
        reap.append(row)
    return ReapSelection(reap=reap, spare=spare)


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
    """Parse one `docker ps --format '{{.CreatedAt}}'` value into an aware datetime.

    Raises ValueError if `raw` does not match the expected layout;
    `_parse_docker_ps_output` catches this per-line rather than letting one
    malformed row abort parsing every other one.
    """
    match = _DOCKER_CREATED_AT_RE.match(raw.strip())
    if not match:
        raise ValueError(f"unparseable docker ps CreatedAt: {raw!r}")
    return datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H:%M:%S %z")


def _parse_docker_ps_output(stdout: str) -> tuple[list[ContainerRow], list[str]]:
    r"""Parse `docker ps --format '{{.Names}}\t{{.CreatedAt}}'` stdout into
    rows, tolerating unparseable lines instead of raising.

    A line missing the tab separator, or whose CreatedAt half doesn't match
    docker's own layout, is skipped, and its best-effort name (the text
    before the first tab, or the whole line if there is no tab at all) goes
    into the second return value instead of raising - one malformed row
    must never block `remove_env` from reaping every other one, or from
    completing at all.
    """
    rows: list[ContainerRow] = []
    unparsed: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        raw_name, separator, raw_created = line.partition("\t")
        if not separator:
            unparsed.append(line.strip())
            continue
        try:
            created_at = _parse_docker_created_at(raw_created)
        except ValueError:
            unparsed.append(raw_name.strip() or line.strip())
            continue
        rows.append(ContainerRow(name=raw_name.strip(), created_at=created_at))
    return rows, unparsed


def _list_floci_containers() -> tuple[list[ContainerRow], list[str]]:
    """`docker ps -a` rows for every container whose name starts with
    `floci-` - a superset of both env parents and their children;
    `select_children_to_reap` does the precise filtering. Second element:
    unparsed row names - see `_parse_docker_ps_output`."""
    result = run_docker(["ps", "-a", "--filter", "name=floci-", "--format", _DOCKER_PS_FORMAT])
    return _parse_docker_ps_output(result.stdout)


def remove_env(name: str) -> RemovalReport:
    """Tear down environment `name`: its parent container, every floci-
    child container this engine can safely attribute to it, and its
    on-disk state - and report exactly what happened.

    Order: remove the parent first (fast, always safe, best-effort - an
    already-gone parent is not fatal), THEN reap children (recorded or
    swept, see below - the spike found children only matter once the
    parent's gone), THEN delete the state directory last. That ordering
    means a failure partway through leaves `list_envs`/`env_status` still
    able to see this environment and a retry able to pick up whatever this
    call did not finish, rather than an environment that vanished from
    tracking while containers of its own were still running.

    Two reap strategies, chosen by whether `target.children` was ever
    recorded (see `EnvState.children`'s own docstring for exactly what
    `None` versus `[]` means):

    - PRIMARY (`target.children is not None`): attempt `docker rm -f` on
      exactly that list, no more, no less, and report each one HONESTLY
      by what actually happened - `reaped` if it was genuinely removed,
      `already_gone` if it was not there to remove (a prior `destroy()`,
      or a human, already removed it). This does NOT mean "returncode
      nonzero" - verified directly against a real daemon (docker 29.6.2):
      `docker rm -f <missing>` exits 0 (not an error) while still writing
      "No such container" to stderr, whereas a real removal exits 0 with
      the name on stdout and empty stderr - so the classification below
      keys off stderr content, never returncode, which cannot tell the
      two apart for `-f`. Nothing is spared and nothing is unparsed on
      this path - there is nothing ambiguous left to guess once the exact
      list is already known.
    - FALLBACK (`target.children is None`, i.e. never recorded - an
      environment from before this field existed, or one nothing has ever
      been applied to): the original `created_at`-based heuristic sweep,
      `select_children_to_reap`. This does NOT promise "reaps every orphan
      and nothing else" - that was an earlier, false claim this docstring
      made, and a fix-round review reproduced two ways it broke (an older
      live environment's child reaped by a younger peer's removal; a live
      peer's own parent container reaped outright when its name happened
      to collide with CHILD_NAME_PREFIXES). The true, narrower guarantee
      `select_children_to_reap` actually provides: this never reaps a
      container it cannot confidently attribute to `name` alone. Ambiguous
      rows survive as `RemovalReport.spared` for a human (or a future prune
      command) to act on, rather than being guessed at and possibly torn
      out from under a still-live peer environment. Rows this engine could
      not even parse survive as `RemovalReport.unparsed` for the same
      reason - never touched, never guessed at.
    """
    try:
        target = state.load_state(name)
    except FileNotFoundError as exc:
        raise EnvNotFoundError(f"no tracked environment named {name!r}") from exc

    run_docker(["rm", "-f", container_name(name)])  # best-effort: already-gone is fine.

    if target.children is not None:
        reaped: list[str] = []
        already_gone: list[str] = []
        for child_name in target.children:
            result = run_docker(["rm", "-f", child_name])
            if _NO_SUCH_CONTAINER_MARKER in result.stderr:
                already_gone.append(child_name)
            else:
                reaped.append(child_name)
        state.delete_env_dir(name)
        return RemovalReport(reaped=reaped, spared=[], unparsed=[], already_gone=already_gone)

    # Includes `target` itself (its state.json is not deleted yet) -
    # select_children_to_reap excludes it from "other" on its own.
    other_live = state.list_envs()
    candidates, unparsed = _list_floci_containers()
    selection = select_children_to_reap(candidates, target, other_live)
    for row in selection.reap:
        run_docker(["rm", "-f", row.name])

    state.delete_env_dir(name)

    return RemovalReport(
        reaped=[row.name for row in selection.reap],
        spared=[row.name for row in selection.spare],
        unparsed=unparsed,
        already_gone=[],  # a PRIMARY-path-only concept; this path never checks individual outcomes.
    )
