"""Tests for foundry.engine: docker lifecycle, port allocation, and the
child-reap selection logic.

Pure tests (name-validation propagation, port allocation, child-reap
selection) always run - no docker daemon involved, nothing here shells out.
The single @pytest.mark.docker test at the bottom exercises the real
lifecycle against a real container and is skipped cleanly (conftest.py)
when no docker daemon is reachable.

Written before src/foundry/engine.py exists - the whole module import fails
until that file lands (the TDD red step for this task).
"""

from __future__ import annotations

import socket
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from foundry.engine import (
    PORT_RANGE_END,
    PORT_RANGE_START,
    ContainerRow,
    allocate_port,
    container_name,
    create_env,
    env_status,
    remove_env,
    select_children_to_reap,
)
from foundry.envspec import EnvSpec
from foundry.state import EnvState, InvalidEnvNameError


def _env(name: str, created_at: datetime) -> EnvState:
    return EnvState(
        name=name,
        created_at=created_at,
        ports={"aws": 15600},
        spec_path=Path("/tmp/spec.yaml"),
        status="running",
    )


def _row(name: str, created_at: datetime) -> ContainerRow:
    return ContainerRow(name=name, created_at=created_at)


# --- create_env: name validation happens before docker is ever touched -----


def test_create_env_rejects_a_bad_name_without_calling_docker(monkeypatch):
    def _must_not_be_called(args: list[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"run_docker should not be called, got {args!r}")

    monkeypatch.setattr("foundry.engine.run_docker", _must_not_be_called)

    with pytest.raises(InvalidEnvNameError):
        create_env("Foo!", EnvSpec(name_prefix="demo"))


# --- allocate_port -----------------------------------------------------------


def test_allocate_port_skips_a_port_that_is_already_bound():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("", PORT_RANGE_START))
        blocker.listen(1)

        port = allocate_port()

    assert port != PORT_RANGE_START
    assert PORT_RANGE_START <= port < PORT_RANGE_END


# --- select_children_to_reap -------------------------------------------------


def test_select_children_to_reap_reaps_orphans_spares_others_ignores_unrelated():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    target = _env("target", t0 + timedelta(minutes=5))
    other = _env("other", t0 + timedelta(minutes=10))

    candidates = [
        _row("floci-ec2-i-orphan1", t0 + timedelta(minutes=6)),  # target's own -> reap
        _row("floci-rds-db-orphan2-abcd", t0 + timedelta(minutes=7)),  # target's own -> reap
        _row("floci-ec2-i-belongs-to-other", t0 + timedelta(minutes=12)),  # other's -> spare
        _row("prizmanyx-postgres", t0 + timedelta(minutes=6)),  # unrelated name -> ignore
        _row("floci-target", t0 + timedelta(minutes=1)),  # the parent itself -> ignore
    ]

    reaped = {row.name for row in select_children_to_reap(candidates, target, [target, other])}

    assert reaped == {"floci-ec2-i-orphan1", "floci-rds-db-orphan2-abcd"}


def test_select_children_to_reap_excludes_children_created_at_or_before_target():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    target = _env("target", t0)
    candidates = [
        _row("floci-ec2-i-same-instant", t0),
        _row("floci-ec2-i-before", t0 - timedelta(seconds=1)),
    ]

    assert select_children_to_reap(candidates, target, [target]) == []


def test_select_children_to_reap_treats_a_child_at_the_other_envs_exact_creation_as_owned():
    # Boundary case for rule 3: the row's created_at is exactly equal to
    # the other env's created_at, not strictly after it - still "owned by
    # the other env" (the <= in the ownership window), because the other
    # env already existed at that instant and could have spawned it.
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    target = _env("target", t0)
    other = _env("other", t0 + timedelta(minutes=1))
    candidates = [_row("floci-ec2-i-exact", other.created_at)]

    assert select_children_to_reap(candidates, target, [target, other]) == []


def test_select_children_to_reap_never_matches_a_name_outside_the_child_prefixes():
    # Lambda is one of floci's real-engine services per the README, but the
    # plan's CHILD_NAME_PREFIXES deliberately names only rds/ec2/elasticache
    # - a plausible-looking "floci-lambda-..." name must still be ignored.
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    target = _env("target", t0)
    candidates = [_row("floci-lambda-fn-abc", t0 + timedelta(minutes=1))]

    assert select_children_to_reap(candidates, target, [target]) == []


def test_select_children_to_reap_ignores_target_appearing_in_other_live_envs():
    # remove_env passes state.list_envs() straight through, which still
    # includes target's own (not-yet-deleted) state.json - the function
    # must exclude target from "other" on its own, not rely on the caller.
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    target = _env("target", t0)
    candidates = [_row("floci-ec2-i-orphan", t0 + timedelta(minutes=1))]

    reaped = select_children_to_reap(candidates, target, [target])

    assert [row.name for row in reaped] == ["floci-ec2-i-orphan"]


# --- docker integration -------------------------------------------------

ENV_NAME = "t3-engine-test"


def _docker_ps_names(filter_name: str) -> list[str]:
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={filter_name}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


@pytest.mark.docker
def test_create_status_remove_round_trip_leaves_no_containers_behind():
    spec = EnvSpec(name_prefix="t3-engine-test")
    try:
        created = create_env(ENV_NAME, spec)
        assert created.name == ENV_NAME
        assert PORT_RANGE_START <= created.ports["aws"] < PORT_RANGE_END

        status = env_status(ENV_NAME)
        assert status.status == "healthy"

        remove_env(ENV_NAME)
    finally:
        # Belt and suspenders: if create_env or an assertion above raised
        # partway through, fall back to a raw docker rm so this test never
        # leaks a container regardless of what foundry code did or didn't
        # get to run. Never touches the untouchable prizmanyx-* stack -
        # this is a single exact container name in the 15600-15999 range.
        subprocess.run(
            ["docker", "rm", "-f", container_name(ENV_NAME)], capture_output=True, check=False
        )

    assert _docker_ps_names(container_name(ENV_NAME)) == []
