"""Terraform apply/destroy runner: drives a real `terraform` binary, inside
a throwaway container, against ONE environment's own floci endpoint.

Every terraform invocation goes through `engine.run_docker` - the same seam
`engine.py` uses for the `docker` binary itself - so neither `apply` nor
`destroy` ever shells out directly, and tests can monkeypatch that one
call. `materialize.emit_terraform` does the actual HCL generation; this
module's job is running it and recording what happened.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from foundry import state
from foundry.engine import DOCKER_RUN_TIMEOUT_SECONDS, run_docker
from foundry.envspec import load_spec
from foundry.materialize import emit_terraform
from foundry.state import EnvState

# Pinned minor version, matching the plan's Global Constraints - never
# `hashicorp/terraform:latest`.
TERRAFORM_IMAGE = "hashicorp/terraform:1.9"

_MAIN_TF_FILENAME = "main.tf"
_APPLY_TRANSCRIPT_FILENAME = "terraform-apply.log"
_DESTROY_TRANSCRIPT_FILENAME = "terraform-destroy.log"

_FLOCI_PS_FILTER = "name=floci-"

# Matches terraform's own one-line summary from either `apply` or
# `destroy` - a single regex so both callers can share one parser rather
# than each recomputing a count from the spec (which would reflect what
# the spec ASKED for, not what terraform actually did).
_RESOURCE_SUMMARY_RE = re.compile(
    r"Apply complete! Resources: (\d+) added|Destroy complete! Resources: (\d+) destroyed"
)


@dataclass(frozen=True)
class ApplyResult:
    """The outcome of one `apply()` or `destroy()` call.

    `resources_applied` is parsed straight from terraform's own summary
    line in the captured transcript - "Apply complete! Resources: N added"
    or "Destroy complete! Resources: N destroyed" - never recomputed from
    the spec, so it reflects what terraform actually did, not what the
    spec asked for. It is 0 whenever that line never appeared: a failed
    `init` (skipped the action entirely) or a failed `apply`/`destroy`
    with nothing completed.
    """

    ok: bool
    resources_applied: int
    transcript_path: Path


def _run_terraform(env_dir: Path, *args: str) -> tuple[bool, str]:
    """Run one `terraform` subcommand against `env_dir`, mounted at /wk.

    Uses `DOCKER_RUN_TIMEOUT_SECONDS` - the same ceiling `engine.create_env`
    gives a cold floci image pull - because `terraform init` can just as
    plausibly block on a cold provider-plugin download, and a real `apply`
    against an RDS instance has been observed (2026-08-30 probe) taking
    upwards of a minute and a half on its own.
    """
    result = run_docker(
        ["run", "--rm", "-v", f"{env_dir}:/wk", "-w", "/wk", TERRAFORM_IMAGE, *args],
        timeout=DOCKER_RUN_TIMEOUT_SECONDS,
    )
    return result.returncode == 0, result.stdout + result.stderr


def _running_floci_container_names() -> set[str]:
    """Every currently-RUNNING container whose name starts with `floci-` -
    both env parents and their real-engine children. Deliberately not
    `-a`: only a container alive right now can plausibly be a child THIS
    apply just spawned, and floci's own real-engine children stay running
    for as long as the emulated resource exists (spike-confirmed) - an
    already-exited row from an unrelated past run must never be swept in.
    """
    result = run_docker(["ps", "--filter", _FLOCI_PS_FILTER, "--format", "{{.Names}}"])
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _parse_resources_summary(transcript: str) -> int:
    match = _RESOURCE_SUMMARY_RE.search(transcript)
    if not match:
        return 0
    added, destroyed = match.groups()
    return int(added or destroyed)


def _write_main_tf(env: EnvState) -> Path:
    spec = load_spec(env.spec_path)
    env_dir = state.env_dir(env.name)
    tf_source = emit_terraform(spec, env)
    main_tf_path = env_dir / _MAIN_TF_FILENAME
    main_tf_path.write_text(tf_source)
    return env_dir


def apply(env: EnvState) -> ApplyResult:
    """Materialize `env`'s spec as Terraform and apply it against its own
    floci endpoint.

    Ownership-by-recording: `docker ps` (running, `floci-`-prefixed) is
    snapshotted immediately before and immediately after the real
    `terraform apply` runs, and the names that only appear in the second
    snapshot - this apply's own floci real-engine children (RDS/EC2/
    ElastiCache backing containers; see `state.CHILD_NAME_PREFIXES`) - are
    unioned into `env.children` and persisted via `state.save_state`
    (union, not overwrite, so a second `apply()` on the same environment
    never loses track of a first apply's own children). This happens
    regardless of whether `apply` itself ultimately succeeds: a partially
    applied run can still spawn real children, and skipping the recording
    on failure would leave `engine.remove_env` unable to attribute them to
    anything. `engine.remove_env` treats a non-None `children` as the
    authoritative reap list for this environment - see its own docstring.

    Assumes no OTHER environment's own `apply`/`destroy` runs concurrently
    with this one - a concurrent peer spawning its own child in the same
    before/after window would be misattributed to this one. An accepted
    limitation of a single-operator local tool, the same trade-off
    `select_children_to_reap`'s own created_at heuristic already lives
    with, for a related but distinct reason.
    """
    env_dir = _write_main_tf(env)

    before = _running_floci_container_names()
    init_ok, init_output = _run_terraform(env_dir, "init")
    if init_ok:
        apply_ok, apply_output = _run_terraform(env_dir, "apply", "-auto-approve")
    else:
        apply_ok, apply_output = False, "(skipped: terraform init failed)"
    after = _running_floci_container_names()

    transcript_path = env_dir / _APPLY_TRANSCRIPT_FILENAME
    transcript_path.write_text(
        f"$ terraform init\n{init_output}\n\n$ terraform apply -auto-approve\n{apply_output}\n"
    )

    new_children = after - before
    combined_children = sorted(set(env.children or []) | new_children)
    state.save_state(env.model_copy(update={"children": combined_children}))

    ok = init_ok and apply_ok
    resources_applied = _parse_resources_summary(apply_output) if ok else 0
    return ApplyResult(ok=ok, resources_applied=resources_applied, transcript_path=transcript_path)


def destroy(env: EnvState) -> ApplyResult:
    """Tear down `env`'s terraform-managed resources via `terraform destroy`.

    Mirrors `apply`: re-materializes main.tf (deterministic emission makes
    this a no-op rewrite when the spec hasn't changed) and runs `init` then
    `destroy` through the same seam. Deliberately does NOT touch
    `env.children` - `engine.remove_env`'s recorded-reap list is the
    ownership record of what THIS environment's applies have ever spawned,
    not a live-container cache, so it stays valid whether `destroy` ran
    first, was skipped, or failed partway (floci itself tears down a
    real-engine child's backing container when the matching AWS resource is
    properly deleted, so a clean `destroy` typically leaves nothing left
    for a later `remove_env` to reap - that is expected, not a bug in
    either function).
    """
    env_dir = _write_main_tf(env)

    init_ok, init_output = _run_terraform(env_dir, "init")
    if init_ok:
        destroy_ok, destroy_output = _run_terraform(env_dir, "destroy", "-auto-approve")
    else:
        destroy_ok, destroy_output = False, "(skipped: terraform init failed)"

    transcript_path = env_dir / _DESTROY_TRANSCRIPT_FILENAME
    transcript_path.write_text(
        f"$ terraform init\n{init_output}\n\n$ terraform destroy -auto-approve\n{destroy_output}\n"
    )

    ok = init_ok and destroy_ok
    resources_applied = _parse_resources_summary(destroy_output) if ok else 0
    return ApplyResult(ok=ok, resources_applied=resources_applied, transcript_path=transcript_path)
