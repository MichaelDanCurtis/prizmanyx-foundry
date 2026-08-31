"""Tests for foundry.state: EnvState persistence and environment-name validation.

Written before src/foundry/state.py exists - the whole module import fails
until that file lands (the TDD red step for this task).
"""

from __future__ import annotations

import re
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from foundry import state
from foundry.state import (
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


def test_save_state_creates_a_0700_directory():
    saved_path = save_state(_sample_state())
    mode = stat.S_IMODE(saved_path.parent.stat().st_mode)
    assert mode == 0o700


def test_load_state_raises_file_not_found_for_an_untracked_env():
    with pytest.raises(FileNotFoundError):
        load_state("never-created")


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
