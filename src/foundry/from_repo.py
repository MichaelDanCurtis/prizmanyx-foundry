"""From-repo materializer: clone anyone's terraform, apply it at THIS
environment's own floci endpoint.

`materialize_from_repo` is the from-repo counterpart to `tf_runner.apply`:
instead of rendering `main.tf` from an `EnvSpec` via
`materialize.emit_terraform`, it shallow-clones an arbitrary git
repository, copies one of its subtrees into the environment's terraform
workdir verbatim, and injects an `override.tf` carrying the SAME
provider/endpoints block `materialize._provider_block` already emits for a
spec-based environment - reused by direct import, not reimplemented, so a
from-repo environment's floci wiring can never drift from a spec-based
one's.

Two reuse decisions, both structural rather than promised in prose:

- The provider block is `materialize._provider_block`'s own output
  (`render_override_tf`), never a second copy of the flag list.
- The apply is `tf_runner.apply_in_dir` - the exact function
  `tf_runner.apply` itself delegates to once it has written its own
  main.tf. This module deliberately does NOT call `tf_runner.apply(env)`:
  that function's first action is `_write_main_tf`, which loads
  `env.spec_path` and re-renders `main.tf` from the spec - overwriting
  whatever was just copied out of the cloned repo, since both write to the
  same path in the same env directory.

Known limitation, honestly stated: terraform's override semantics merge
`override.tf` into the DEFAULT `provider "aws"` configuration. A cloned
repo that declares additional aliased aws providers
(`provider "aws" { alias = "west" }`) keeps those pointed wherever the repo
pointed them; only the default one is redirected at this environment's
floci. A repo carrying its own top-level override file is refused outright
rather than silently clobbered - see `_reject_conflicting_override_files`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from foundry import state, tf_runner
from foundry.envspec import EnvSpec
from foundry.materialize import _provider_block
from foundry.state import EnvState
from foundry.tf_runner import ApplyResult

_OVERRIDE_TF_FILENAME = "override.tf"

# Terraform reads `override.tf` AND every `*_override.tf` in the working
# directory as override files, merged in filename order with the last one
# winning. Either shape, shipped by a cloned repo, could clobber or
# out-precedence foundry's own endpoint injection - see
# `_reject_conflicting_override_files`.
_OVERRIDE_TF_SUFFIX = "_override.tf"

# The transient clone lives under this prefix inside the environment's own
# 0o700 state directory, and is removed before terraform ever runs - the
# same discipline Prizmanyx's own clone seam applies ("Clones are
# transient; only the proposal, once stored, persists" -
# `convention_ingest.clone_scan_parse_terraform`). Keeping it out of the
# final workdir also keeps the directory bind-mounted into every
# `docker run ... hashicorp/terraform` invocation down to the files
# terraform is actually meant to read.
_CLONE_DIR_PREFIX = ".clone-"

# A shallow clone of a small repo over a real network should finish in
# seconds; generous enough to survive a slow connection without hanging
# the whole `foundry create` call indefinitely.
_GIT_CLONE_TIMEOUT_SECONDS = 120.0


class FromRepoError(Exception):
    """Base class for from-repo materialization failures."""


class InvalidRepoURLError(FromRepoError):
    """Raised when a candidate repo URL is not a usable https git URL."""


class RepoCloneError(FromRepoError):
    """Raised when the shallow clone of a repo URL fails."""


def _validate_https_url(url: str) -> None:
    """Reject any URL that is not a credential-free https git URL.

    Both halves mirror Prizmanyx's own clone-seam rule verbatim
    (`packages/analysis-harness/prizmanyx_analysis_harness/clone.py`'s
    `CloneRequest.__post_init__` - the check
    `packages/api/prizmanyx_api/convention_ingest.py`'s
    `clone_scan_parse_terraform` relies on for its own repo source):

    1. Scheme must be https - message text "Only https git URLs are
       supported", not merely the same idea in different words, so an
       operator who already knows Prizmanyx's rule recognizes this one on
       sight.
    2. No credentials embedded in the URL. Prizmanyx refuses these because
       they would land in argv and un-redacted errors; that reason applies
       here identically and literally - this URL becomes `git clone <url>`
       argv, readable by every process on the machine, and would otherwise
       be echoed back inside `RepoCloneError`'s own message. The message
       differs from Prizmanyx's only in its remedy clause: this tool has no
       credential registry to point at, it relies on the operator's own git
       credential helper.

    The offending URL is never included in either message - a rejected
    credential must not be re-emitted into a log line by the very check
    that rejected it.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise InvalidRepoURLError("Only https git URLs are supported")
    if parsed.username or parsed.password:
        raise InvalidRepoURLError(
            "URL must not embed credentials; let git's own credential helper supply them"
        )


def run_git(
    args: list[str], *, timeout: float | None = _GIT_CLONE_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    """Run the `git` CLI, capturing stdout/stderr as text.

    Mirrors `engine.run_docker`'s role for the docker binary: a single seam
    between this module and a real git process. `GIT_TERMINAL_PROMPT=0` is
    forced regardless of the ambient environment - a private repo with no
    usable credential helper must fail immediately and readably, not block
    on a username prompt nobody can see until the clone timeout fires.

    Pure tests monkeypatch `_clone_repo` above this seam (see its own
    docstring for why); this function's real subprocess call is exercised
    by the `@pytest.mark.docker` acceptance test, against the foundry's own
    pushed GitHub repo.
    """
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _clone_repo(url: str, dest: Path) -> None:
    """Shallow-clone `url` into `dest` (`git clone --depth 1`).

    `dest` is expected to be empty or absent - `materialize_from_repo`
    always hands it a freshly-made temporary directory, which `git clone`
    accepts.

    Pure tests monkeypatch this whole function (not `run_git` underneath
    it) so they can simulate "the repo is now at dest" by writing real
    files straight to a tmp_path - no git process, no network, involved at
    all.
    """
    # The explicit "--" marks the end of options: defense-in-depth against a
    # hostile url starting with "-" ever reaching git as a flag rather than
    # the repository argument it is meant to be. `_validate_https_url`
    # already rejects anything that is not a genuine https:// URL before
    # this runs, so no url shaped like a flag can arrive here today - this
    # is a second, independent line of defense, not a fix for a reachable bug.
    result = run_git(["clone", "--depth", "1", "--", url, str(dest)])
    if result.returncode != 0:
        raise RepoCloneError(f"git clone failed: {result.stderr.strip()}")


def _resolve_subtree(clone_dir: Path, path: str | None) -> Path:
    """The directory `path` selects inside `clone_dir`, or `clone_dir`
    itself when `path` is None.

    Resolved and boundary-checked against `clone_dir` before use - a
    hostile `--path ../../etc` (or an absolute path, which plain
    `Path.__truediv__` would otherwise let silently replace `clone_dir`
    entirely) can never walk the later copy step outside the clone it came
    from. Mirrors this codebase's existing boundary-validation habit for
    the one free-form string this module accepts from a human
    (`state.validate_env_name`'s role for environment names,
    `envspec._NAME_PREFIX_PATTERN`'s for name_prefix).
    """
    resolved_clone_dir = clone_dir.resolve()
    source = (resolved_clone_dir / path).resolve() if path else resolved_clone_dir
    if not source.is_relative_to(resolved_clone_dir):
        raise FromRepoError(f"path {path!r} escapes the cloned repository")
    if not source.exists():
        raise FromRepoError(f"path {path!r} does not exist in the cloned repository")
    if not source.is_dir():
        raise FromRepoError(f"path {path!r} is not a directory in the cloned repository")
    return source


def _reject_conflicting_override_files(source: Path) -> None:
    """Refuse a subtree that already carries a terraform override file.

    Terraform treats `override.tf` and every `*_override.tf` in the working
    directory as override files, merging them in filename order with the
    last one winning. A repo shipping `override.tf` would have foundry's
    own write clobber it; one shipping e.g. `zzz_override.tf` would
    out-precedence foundry's endpoint injection instead - and the apply
    would then quietly be pointed somewhere other than this environment's
    own floci. Both are refused loudly, before anything is copied, rather
    than discovered in a confusing terraform error (or not at all).

    Only the top level is checked, because only the top level is what
    terraform reads: an override file inside a subdirectory of the copied
    tree cannot affect this apply.
    """
    for entry in sorted(source.iterdir()):
        if not entry.is_file():
            continue
        if entry.name == _OVERRIDE_TF_FILENAME or entry.name.endswith(_OVERRIDE_TF_SUFFIX):
            raise FromRepoError(
                f"the selected subtree already contains {entry.name!r}: foundry injects its own "
                f"{_OVERRIDE_TF_FILENAME} for this environment's floci endpoints, and terraform "
                "would merge or overwrite the two unpredictably - remove it, or select a subtree "
                "that does not carry one"
            )


def render_override_tf(port: int) -> str:
    """The exact `override.tf` content foundry injects for a floci
    endpoint on `port`.

    Reuses `materialize._provider_block` verbatim - the same
    `terraform { required_providers { aws = ... } }` block, the same
    `test`/`test`-credentialed, `us-east-1`-forced, `skip_*`-flagged
    `provider "aws" {}` block, and the same five-service `endpoints {}`
    block a spec-based environment's own `main.tf` carries (see
    `materialize.emit_terraform`) - imported directly rather than
    reimplemented, so a from-repo environment's floci wiring can never
    drift from a spec-based one's. Terraform's own override-file semantics
    apply this over whatever `provider "aws" {}` block, if any, the cloned
    repo's own `.tf` files declare - adding it fresh if the repo declares
    none at all, which is exactly the shape `examples/aws-mini/main.tf`
    uses (see `docs/DECISIONS.md` entry 002 for this design's context).
    """
    return _provider_block(port) + "\n"


def _write_override_tf(env: EnvState, workdir: Path) -> Path:
    override_path = workdir / _OVERRIDE_TF_FILENAME
    override_path.write_text(render_override_tf(env.ports["aws"]))
    return override_path


def materialize_from_repo(env: EnvState, url: str, path: str | None = None) -> ApplyResult:
    """Clone `url`'s terraform and apply it at `env`'s own floci endpoint.

    Steps: validate `url` (`_validate_https_url`, checked before anything
    else is touched); shallow-clone it into a transient directory inside
    `env`'s own 0o700 state directory; select the `path` subtree (the whole
    clone, minus `.git`, when `path` is None) and refuse it if it carries
    its own terraform override file; copy it into the env dir itself - the
    same directory `tf_runner` treats as the terraform workdir, mounted at
    `/wk` for every `docker run ... hashicorp/terraform` invocation; write
    `override.tf` carrying this env's own provider/endpoints block; then
    apply through `tf_runner.apply_in_dir`, which records children exactly
    as a spec-based apply does. The clone is removed on the way out,
    success or failure.

    `env` must already exist (`engine.create_env` already ran) - this
    mirrors `tf_runner.apply`'s own assumption. A from-repo environment
    still needs SOME `EnvSpec` to satisfy `create_env`'s signature and
    `EnvState.spec_path`, even though it is never rendered here: see
    `placeholder_spec`.
    """
    _validate_https_url(url)
    workdir = state.ensure_env_dir(env.name)
    clone_dir = Path(tempfile.mkdtemp(prefix=_CLONE_DIR_PREFIX, dir=workdir))
    try:
        _clone_repo(url, clone_dir)
        source = _resolve_subtree(clone_dir, path)
        _reject_conflicting_override_files(source)
        shutil.copytree(source, workdir, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)

    _write_override_tf(env, workdir)
    return tf_runner.apply_in_dir(env, workdir)


def placeholder_spec(name: str) -> EnvSpec:
    """An `EnvSpec` with no resources of its own, for a from-repo environment.

    `engine.create_env` always needs an `EnvSpec` to satisfy its own
    signature and to write `spec.yaml` - but a from-repo environment's real
    resources come entirely from the cloned repo's own terraform, never
    from `materialize.emit_terraform`; `materialize_from_repo` never calls
    `tf_runner.apply` (see this module's own docstring for why), so this
    spec is never rendered. Its only real job is satisfying
    `EnvSpec.name_prefix`'s shape (`^[a-z][a-z0-9-]{0,24}$`, up to 25
    characters) from an environment name that may be up to 41 characters
    (`state.ENV_NAME_PATTERN`) - truncated safely, since name_prefix's
    content is otherwise inert here.
    """
    return EnvSpec(name_prefix=name[:25])
