"""Shared pytest configuration for the foundry test suite.

Skips every test carrying `@pytest.mark.docker`, cleanly, when no docker
daemon is reachable from this machine: `shutil.which` first (is the binary
even on PATH), then `docker info` (is the daemon actually up, not just the
CLI installed). The marker itself is registered in pyproject.toml.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@pytest.fixture(scope="session")
def docker_available() -> bool:
    """True if a docker daemon is reachable from this machine right now."""
    return _docker_available()


# `config` is unused but must keep this exact name: pluggy injects pytest
# hook arguments by name against the hookspec, not positionally, so
# renaming or dropping it would break hook registration.
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:  # noqa: ARG001
    if _docker_available():
        return
    skip_docker = pytest.mark.skip(reason="docker is not available in this environment")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip_docker)
