"""Tests for foundry.engine: docker lifecycle, port allocation, and the
child-reap selection logic.

Pure tests (name-validation propagation, port allocation, child-reap
selection, transcript parsing) always run - no docker daemon involved,
nothing here shells out. The single @pytest.mark.docker test at the bottom
exercises the real lifecycle against a real container and is skipped
cleanly (conftest.py) when no docker daemon is reachable.

The "case A2" and "case B" tests below reproduce two real bugs a fix-round
review found in an earlier version of `select_children_to_reap`:

  A2: rule 3 only excluded LATER-created environments from being a
      candidate parent, so an OLDER live environment's own child was
      wrongly reaped by a younger peer's `remove_env`.
  B:  an environment named e.g. "rds-x" gets container "floci-rds-x",
      which collides with CHILD_NAME_PREFIXES by coincidence of the
      `floci-<name>` join; combined with EnvState.created_at being
      stamped AFTER that environment's own healthcheck (later than the
      same container's real `docker ps` CreatedAt), a peer's removal
      could reap that live environment's own parent container outright.

Both are asserted here as "spared, not reaped" - the corrected behavior.

Task 4 added ownership-by-recording (`EnvState.children`, populated by
`tf_runner.apply`'s docker-ps-delta): `remove_env` now has a PRIMARY reap
path (recorded children, exact) and a FALLBACK path (this file's own
`select_children_to_reap` heuristic, for environments with no recording).
The "remove_env: recorded-children primary path vs heuristic fallback"
section below exercises `remove_env`'s REAL dispatch between the two -
via a fake `run_docker` and an isolated on-disk state dir, not a real
docker daemon - so the branch choice itself is proven, not just each
path's own already-tested internals. The first LIVE proof that recorded
children actually get removed from a real, still-running container is
`tests/test_apply_docker.py`'s `@pytest.mark.docker` acceptance test, not
here.
"""

from __future__ import annotations

import socket
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from foundry import state
from foundry.engine import (
    PORT_RANGE_END,
    PORT_RANGE_START,
    ContainerRow,
    RemovalReport,
    _parse_docker_ps_output,
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


def test_create_env_rejects_a_child_prefix_colliding_name_without_calling_docker(monkeypatch):
    def _must_not_be_called(args: list[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"run_docker should not be called, got {args!r}")

    monkeypatch.setattr("foundry.engine.run_docker", _must_not_be_called)

    with pytest.raises(InvalidEnvNameError):
        create_env("rds-evil", EnvSpec(name_prefix="demo"))


# --- allocate_port -----------------------------------------------------------


def test_allocate_port_skips_a_port_that_is_already_bound():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("", PORT_RANGE_START))
        blocker.listen(1)

        port = allocate_port()

    assert port != PORT_RANGE_START
    assert PORT_RANGE_START <= port < PORT_RANGE_END


# --- select_children_to_reap: baseline behavior ------------------------------


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

    selection = select_children_to_reap(candidates, target, [target, other])

    assert {row.name for row in selection.reap} == {
        "floci-ec2-i-orphan1",
        "floci-rds-db-orphan2-abcd",
    }
    assert {row.name for row in selection.spare} == {"floci-ec2-i-belongs-to-other"}


def test_select_children_to_reap_spares_children_created_at_or_before_target():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    target = _env("target", t0)
    candidates = [
        _row("floci-ec2-i-same-instant", t0),
        _row("floci-ec2-i-before", t0 - timedelta(seconds=1)),
    ]

    selection = select_children_to_reap(candidates, target, [target])

    assert selection.reap == []
    assert {row.name for row in selection.spare} == {
        "floci-ec2-i-same-instant",
        "floci-ec2-i-before",
    }


def test_select_children_to_reap_spares_a_child_created_a_moment_after_another_live_env():
    # The mandated rule is a strict "<": `other.created_at < row.created_at`.
    # One microsecond after `other` came into existence is enough for
    # `other` to be an equally plausible parent - spared.
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    target = _env("target", t0)
    other = _env("other", t0 + timedelta(minutes=1))
    candidates = [_row("floci-ec2-i-just-after", other.created_at + timedelta(microseconds=1))]

    selection = select_children_to_reap(candidates, target, [target, other])

    assert selection.reap == []
    assert [row.name for row in selection.spare] == ["floci-ec2-i-just-after"]


def test_select_children_to_reap_reaps_a_child_at_the_exact_same_instant_as_another_env():
    # The flip side of the strict "<": a row created at the EXACT same
    # instant as `other` does not satisfy `other.created_at <
    # row.created_at` (equal is not less), so rule 3 alone does not spare
    # it - documented explicitly here rather than left as an untested
    # boundary, per the mandated formula verbatim. In real usage this is a
    # vanishingly unlikely tie (docker's own CreatedAt has only
    # whole-second resolution, EnvState.created_at has microsecond
    # resolution), not a live risk.
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    target = _env("target", t0)
    other = _env("other", t0 + timedelta(minutes=1))
    candidates = [_row("floci-ec2-i-exact-tie", other.created_at)]

    selection = select_children_to_reap(candidates, target, [target, other])

    assert [row.name for row in selection.reap] == ["floci-ec2-i-exact-tie"]
    assert selection.spare == []


def test_select_children_to_reap_never_matches_a_name_outside_the_child_prefixes():
    # Lambda is one of floci's real-engine services per the README, but the
    # plan's CHILD_NAME_PREFIXES deliberately names only rds/ec2/elasticache
    # - a plausible-looking "floci-lambda-..." name must still be ignored
    # entirely (neither reaped nor reported as spared).
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    target = _env("target", t0)
    candidates = [_row("floci-lambda-fn-abc", t0 + timedelta(minutes=1))]

    selection = select_children_to_reap(candidates, target, [target])

    assert selection.reap == []
    assert selection.spare == []


def test_select_children_to_reap_ignores_target_appearing_in_other_live_envs():
    # remove_env passes state.list_envs() straight through, which still
    # includes target's own (not-yet-deleted) state.json - the function
    # must exclude target from "other" on its own, not rely on the caller.
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    target = _env("target", t0)
    candidates = [_row("floci-ec2-i-orphan", t0 + timedelta(minutes=1))]

    selection = select_children_to_reap(candidates, target, [target])

    assert [row.name for row in selection.reap] == ["floci-ec2-i-orphan"]
    assert selection.spare == []


# --- select_children_to_reap: fix-round bug reproductions -------------------


def test_select_children_to_reap_spares_an_older_live_envs_child_case_a2():
    # Case A2: env "a" is OLDER than target and still alive. A child
    # spawned by "a" AFTER target was created (a is still receiving
    # traffic well after a younger peer exists) must never be reaped just
    # because removing the younger peer ("target") is what triggered the
    # sweep. The buggy rule (`target.created_at < other.created_at <=
    # row.created_at`) required the "other" env to be created AFTER
    # target, so an older "a" was never even considered a candidate parent
    # and this row was wrongly reaped.
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    older_live_env = _env("a", t0)
    target = _env("target", t0 + timedelta(minutes=5))
    a_child = _row("floci-ec2-i-belongs-to-a", t0 + timedelta(minutes=10))

    selection = select_children_to_reap([a_child], target, [target, older_live_env])

    assert selection.reap == [], "an older live env's child must never be reaped"
    assert [row.name for row in selection.spare] == ["floci-ec2-i-belongs-to-a"]


def test_select_children_to_reap_spares_a_peer_whose_name_collides_with_child_prefix_case_b():
    # Case B: env "rds-x" is a live peer whose OWN parent container is
    # named "floci-rds-x" - which starts with "floci-rds-" and therefore
    # matches CHILD_NAME_PREFIXES purely by coincidence of the
    # `floci-<name>` join. Its EnvState.created_at is stamped AFTER its
    # own healthcheck passed (see EnvState's docstring) - LATER than that
    # same container's real `docker ps` CreatedAt - so a timing-only rule
    # can come out backwards comparing an env against its own container.
    # Only the unconditional "is this literally another tracked env's
    # parent container" check (rule 1) catches this; it must never depend
    # on clocks agreeing.
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    target = _env("target", t0)
    rds_x_real_docker_created_at = t0 + timedelta(seconds=10)
    rds_x_recorded_created_at = t0 + timedelta(seconds=40)  # +30s health-wait gap
    rds_x = _env("rds-x", rds_x_recorded_created_at)
    rds_x_own_parent_row = _row(container_name("rds-x"), rds_x_real_docker_created_at)
    assert rds_x_own_parent_row.name.startswith("floci-rds-"), "the collision this test exercises"

    selection = select_children_to_reap([rds_x_own_parent_row], target, [target, rds_x])

    assert selection.reap == [], "a live peer's own parent container must never be reaped"
    assert [row.name for row in selection.spare] == ["floci-rds-x"]


def test_select_children_to_reap_auto_reaps_nothing_once_two_environments_are_alive():
    # The honest cost of the case-A2 fix, made explicit: with target plus
    # one OTHER live environment (regardless of which is older), every row
    # created after target is spared rather than reaped, because the other
    # env is an equally plausible parent and there is no label to tell
    # them apart.
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    target = _env("target", t0 + timedelta(minutes=5))
    older_peer = _env("older-peer", t0)
    younger_peer = _env("younger-peer", t0 + timedelta(minutes=6))
    candidates = [_row("floci-ec2-i-ambiguous", t0 + timedelta(minutes=7))]

    with_older = select_children_to_reap(candidates, target, [target, older_peer])
    with_younger = select_children_to_reap(candidates, target, [target, younger_peer])

    assert with_older.reap == []
    assert with_younger.reap == []


# --- remove_env: recorded-children PRIMARY path vs heuristic FALLBACK ------


class _FakeDocker:
    """Records every `run_docker` call `remove_env` makes and answers a
    `ps -a --filter name=floci-` call with a canned table; every other call
    (`rm -f`, ...) succeeds trivially. Lets `remove_env`'s REAL dispatch
    logic run end-to-end - not a reimplementation of it - against
    controlled `docker ps` output with no real docker daemon involved.
    """

    def __init__(self, ps_stdout: str = ""):
        self.ps_stdout = ps_stdout
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        # remove_env's own call sites never pass `timeout=` explicitly
        # (they all rely on run_docker's own default), so this fake never
        # needs to accept or honor one - matching this file's existing
        # `_must_not_be_called` double, just above.
        self.calls.append(args)
        if args[:2] == ["ps", "-a"]:
            return subprocess.CompletedProcess(args, 0, stdout=self.ps_stdout, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _save_env_in_isolated_dir(tmp_path, monkeypatch, env_state: EnvState) -> None:
    monkeypatch.setattr(state, "ENVS_DIR", tmp_path / "envs")
    state.save_state(env_state)


def test_remove_env_reaps_exactly_the_recorded_children_when_present(tmp_path, monkeypatch):
    target = _env("target", datetime(2026, 1, 1, tzinfo=UTC)).model_copy(
        update={"children": ["floci-ec2-i-abc", "floci-rds-db-xyz"]}
    )
    _save_env_in_isolated_dir(tmp_path, monkeypatch, target)
    fake_docker = _FakeDocker()
    monkeypatch.setattr("foundry.engine.run_docker", fake_docker)

    report = remove_env("target")

    assert sorted(report.reaped) == ["floci-ec2-i-abc", "floci-rds-db-xyz"]
    assert report.spared == []
    assert report.unparsed == []
    # The primary path never needs a `docker ps` sweep at all - it already
    # knows exactly what to remove, so it must never even ask.
    assert not any(call[:2] == ["ps", "-a"] for call in fake_docker.calls)
    assert not state.state_file("target").exists()


def test_remove_env_reaps_nothing_when_a_recorded_apply_spawned_zero_children(
    tmp_path, monkeypatch
):
    # `children == []` is NOT "never recorded" (that's `None`, the
    # fallback trigger) - it is the authoritative "an apply happened and
    # spawned nothing" - still the primary path, no heuristic sweep.
    target = _env("target", datetime(2026, 1, 1, tzinfo=UTC)).model_copy(update={"children": []})
    _save_env_in_isolated_dir(tmp_path, monkeypatch, target)
    fake_docker = _FakeDocker()
    monkeypatch.setattr("foundry.engine.run_docker", fake_docker)

    report = remove_env("target")

    assert report == RemovalReport(reaped=[], spared=[], unparsed=[])
    assert not any(call[:2] == ["ps", "-a"] for call in fake_docker.calls)


def test_remove_env_falls_back_to_the_heuristic_when_children_was_never_recorded(
    tmp_path, monkeypatch
):
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    target = _env("target", t0)  # children defaults to None: never recorded
    assert target.children is None
    _save_env_in_isolated_dir(tmp_path, monkeypatch, target)
    ps_stdout = "floci-ec2-i-orphan\t2026-01-01 00:01:00 +0000 UTC\n"
    fake_docker = _FakeDocker(ps_stdout=ps_stdout)
    monkeypatch.setattr("foundry.engine.run_docker", fake_docker)

    report = remove_env("target")

    assert report.reaped == ["floci-ec2-i-orphan"]
    assert report.spared == []
    assert report.unparsed == []
    # The fallback path DOES need the docker ps sweep - that's the point.
    assert any(call[:2] == ["ps", "-a"] for call in fake_docker.calls)


def test_remove_env_fallback_still_spares_what_it_cannot_attribute(tmp_path, monkeypatch):
    # A minimal end-to-end proof that the fallback path, reached through
    # remove_env's real dispatch (not select_children_to_reap called
    # directly, which the tests above this section already cover
    # exhaustively), still honors the same conservatism: a row created
    # before target is spared, not reaped.
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    target = _env("target", t0)
    _save_env_in_isolated_dir(tmp_path, monkeypatch, target)
    ps_stdout = "floci-ec2-i-too-old\t2025-12-31 23:59:00 +0000 UTC\n"
    fake_docker = _FakeDocker(ps_stdout=ps_stdout)
    monkeypatch.setattr("foundry.engine.run_docker", fake_docker)

    report = remove_env("target")

    assert report.reaped == []
    assert report.spared == ["floci-ec2-i-too-old"]


# --- docker ps transcript parsing (real STDOUT shape, tolerant of junk) -----


def test_parse_docker_ps_output_parses_well_formed_rows():
    stdout = (
        "floci-ec2-i-abc\t2026-01-01 00:05:00 -0700 PDT\n"
        "floci-rds-db-xyz-1234\t2026-01-01 00:06:00 +0000 UTC\n"
    )

    rows, unparsed = _parse_docker_ps_output(stdout)

    assert [row.name for row in rows] == ["floci-ec2-i-abc", "floci-rds-db-xyz-1234"]
    assert unparsed == []


def test_parse_docker_ps_output_skips_unparseable_rows_without_raising():
    stdout = (
        "floci-ec2-i-good\t2026-01-01 00:05:00 -0700 PDT\n"
        "floci-ec2-i-garbled\tnot-a-timestamp-at-all\n"
        "floci-ec2-i-blank-created-at\t\n"
        "no-tab-in-this-line-at-all\n"
        "\n"
        "floci-rds-db-good\t2026-01-01 00:06:00 +0000 UTC\n"
    )

    rows, unparsed = _parse_docker_ps_output(stdout)

    assert [row.name for row in rows] == ["floci-ec2-i-good", "floci-rds-db-good"]
    assert unparsed == [
        "floci-ec2-i-garbled",
        "floci-ec2-i-blank-created-at",
        "no-tab-in-this-line-at-all",
    ]


def test_parse_docker_ps_output_handles_empty_stdout():
    rows, unparsed = _parse_docker_ps_output("")
    assert rows == []
    assert unparsed == []


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
    # Deliberately uses the REAL ~/.prizmanyx-foundry (not a monkeypatched
    # ENVS_DIR): this is the one test that exercises real cross-process
    # visibility semantics (an env's own state must be visible to any
    # OTHER real foundry process's "other live envs" check), which an
    # isolated tmp_path would silently hide. Full cleanup in `finally`
    # instead - both the container AND the state directory, so a failed
    # run can never wedge a later run under the same name.
    spec = EnvSpec(name_prefix="t3-engine-test")
    try:
        created = create_env(ENV_NAME, spec)
        assert created.name == ENV_NAME
        assert PORT_RANGE_START <= created.ports["aws"] < PORT_RANGE_END

        status = env_status(ENV_NAME)
        assert status.status == "healthy"

        report = remove_env(ENV_NAME)
        assert report.reaped == []  # no terraform apply in this task - nothing to have spawned
    finally:
        # Belt and suspenders: if create_env or an assertion above raised
        # partway through, fall back to raw docker + filesystem cleanup so
        # this test can never leak a container OR wedge a later run with a
        # stale state.json, regardless of what foundry code did or didn't
        # get to run. Never touches the untouchable prizmanyx-* stack -
        # this is a single exact container name in the 15600-15999 range.
        subprocess.run(
            ["docker", "rm", "-f", container_name(ENV_NAME)], capture_output=True, check=False
        )
        state.delete_env_dir(ENV_NAME)

    assert _docker_ps_names(container_name(ENV_NAME)) == []
