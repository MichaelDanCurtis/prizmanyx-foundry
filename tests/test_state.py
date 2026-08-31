"""Tests for foundry.state: EnvState persistence and environment-name validation.

Written before src/foundry/state.py exists - the whole module import fails
until that file lands (the TDD red step for this task).
"""

from __future__ import annotations

import json
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from foundry import state
from foundry.state import (
    CHILD_NAME_PREFIXES,
    ENV_NAME_PATTERN,
    EnvState,
    InvalidEnvNameError,
    delete_env_dir,
    list_envs,
    load_state,
    save_state,
    validate_env_name,
)


@pytest.fixture(autouse=True)
def _isolated_envs_dir(tmp_path, monkeypatch):
    # Every test in this file gets its own throwaway envs directory - never
    # the real ~/.prizmanyx-foundry on the machine running the suite.
    monkeypatch.setattr(state, "ENVS_DIR", tmp_path / "envs")


def _sample_state(name: str = "demo", **overrides: object) -> EnvState:
    fields: dict[str, object] = {
        "name": name,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "ports": {"aws": 15600},
        "spec_path": Path("/tmp/demo/spec.yaml"),
        "seed": 42,
        "status": "running",
    }
    fields.update(overrides)
    return EnvState(**fields)


# --- EnvState persistence -----------------------------------------------


def test_save_then_load_round_trips_exactly():
    original = _sample_state()
    save_state(original)
    assert load_state("demo") == original


def test_save_then_load_round_trips_a_state_carrying_an_az_port():
    # Slice 4: an Azure-enabled environment records the floci-az host port
    # under `ports["az"]` (the key state.py's docstring reserved) alongside
    # `ports["aws"]`. The whole state must persist and reload byte-for-byte
    # - `az` is not special-cased anywhere in the schema, it is just another
    # entry in the open `ports` map, and this pins that it survives the
    # round trip.
    original = _sample_state(name="azure-env", ports={"aws": 15600, "az": 14577})
    save_state(original)

    loaded = load_state("azure-env")

    assert loaded == original
    assert loaded.ports == {"aws": 15600, "az": 14577}


def test_save_state_creates_a_0700_directory():
    saved_path = save_state(_sample_state())
    mode = stat.S_IMODE(saved_path.parent.stat().st_mode)
    assert mode == 0o700


def test_load_state_raises_file_not_found_for_an_untracked_env():
    with pytest.raises(FileNotFoundError):
        load_state("never-created")


def test_load_state_defaults_children_to_none_for_pre_migration_json_without_the_key():
    # Finding 4: a state.json written before EnvState gained `children`
    # (the Task 3 era) has no such key in it at all - `load_state` must
    # still parse it, with `children` defaulting to None. That None is
    # exactly `engine.remove_env`'s signal to use the created_at heuristic
    # FALLBACK rather than the recorded-children PRIMARY path for these
    # pre-existing environments - so this is a load-bearing default, not
    # incidental pydantic convenience.
    directory = state.ensure_env_dir("legacy")
    pre_migration_json = json.dumps(
        {
            "name": "legacy",
            "created_at": "2026-01-01T00:00:00+00:00",
            "ports": {"aws": 15600},
            "spec_path": "/tmp/legacy/spec.yaml",
            "seed": None,
            "status": "running",
            # deliberately no "children" key at all
        }
    )
    (directory / "state.json").write_text(pre_migration_json)

    loaded = load_state("legacy")

    assert loaded.children is None


def test_list_envs_is_empty_when_the_envs_directory_does_not_exist_yet():
    assert list_envs() == []


def test_list_envs_returns_every_tracked_env_sorted_by_name():
    save_state(_sample_state(name="zebra"))
    save_state(_sample_state(name="alpha"))
    assert [env.name for env in list_envs()] == ["alpha", "zebra"]


def test_delete_env_dir_removes_the_whole_directory():
    saved_path = save_state(_sample_state())
    assert saved_path.parent.is_dir()
    delete_env_dir("demo")
    assert not saved_path.parent.exists()


def test_delete_env_dir_is_a_noop_when_nothing_was_ever_created():
    delete_env_dir("never-created")  # must not raise


# --- env name validation --------------------------------------------------


@pytest.mark.parametrize("name", ["a", "demo", "demo-env-1", "a" * 41])
def test_validate_env_name_accepts_conforming_names(name):
    validate_env_name(name)  # must not raise


@pytest.mark.parametrize(
    "name",
    [
        "Foo!",  # uppercase + punctuation
        "Demo",  # uppercase start
        "-demo",  # leading hyphen
        "1demo",  # leading digit
        "",  # empty
        "a" * 42,  # one over the length cap
    ],
)
def test_validate_env_name_rejects_bad_names_naming_the_rule(name):
    with pytest.raises(InvalidEnvNameError, match=re.escape(ENV_NAME_PATTERN)):
        validate_env_name(name)


def test_env_dir_validates_the_name_too():
    with pytest.raises(InvalidEnvNameError):
        state.env_dir("Bad!")


# --- env name / child-prefix collision rejection --------------------------
# A fix-round review reproduced a live bug: an env named e.g. "rds-x" gets
# container "floci-rds-x", which starts with "floci-rds-" (a
# CHILD_NAME_PREFIXES entry) purely by coincidence of the `floci-<name>`
# join - engine.py's reap sweep then cannot tell that live parent container
# apart from a real emulated-RDS child by name alone. Reject the name
# shape at the boundary so this collision can never be created.


@pytest.mark.parametrize("stem", [prefix.removeprefix("floci-") for prefix in CHILD_NAME_PREFIXES])
def test_validate_env_name_rejects_names_colliding_with_child_prefixes(stem):
    colliding_name = f"{stem}x"
    with pytest.raises(InvalidEnvNameError, match=re.escape(stem)):
        validate_env_name(colliding_name)


def test_validate_env_name_accepts_a_name_that_merely_contains_a_stem_mid_string():
    # The rule is "starts with", not "contains" - a stem appearing after
    # the first character is not a collision (floci-my-rds-clone does not
    # start with floci-rds-).
    validate_env_name("my-rds-clone")  # must not raise


# --- env name / az-prefix collision rejection -----------------------------
# CRITICAL create-time collision: az_container_name("foo") == "floci-az-foo"
# == container_name("az-foo"). An env named "az-foo" gets AWS parent
# container "floci-az-foo", byte-identical to env "foo"'s AZURE container -
# so create_env's stale-container clear (`docker rm -f floci-az-foo`) would
# DELETE a different, live env's healthy Azure container. Reject the "az-"
# name prefix at the boundary, before any docker call can fire.


@pytest.mark.parametrize("name", ["az-foo", "az-anything", "az-", "az-x", "az-eastus"])
def test_validate_env_name_rejects_the_az_prefix_colliding_with_floci_az_containers(name):
    with pytest.raises(InvalidEnvNameError, match="az-"):
        validate_env_name(name)


@pytest.mark.parametrize("name", ["azure-demo", "azfoo", "az", "azalea", "azure", "azov"])
def test_validate_env_name_accepts_az_names_that_do_not_start_with_the_az_hyphen_prefix(name):
    # Precision: ONLY the "az-" prefix (the one that produces the floci-az-
    # collision) is forbidden - not any name merely containing or starting
    # with "az". "azure-demo", "azfoo", and a bare "az" are all safe: none
    # yields a container name of the form container_name("az-<something>").
    validate_env_name(name)  # must not raise
