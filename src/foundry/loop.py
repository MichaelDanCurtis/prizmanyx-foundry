"""The loop driver: walks one environment through Prizmanyx's pipeline as a
scripted operator, and returns a full PASS/FAIL report - never an exception.

`run_loop(env, client)` calls `PrizmanyxClient` methods in the same order an
operator clicking through the UI would: sign in, create a fresh program,
point it at this environment's floci endpoint, extract, wait for discovery,
accept the destination catalog's defaults, approve every migration unit,
disposition every unit `rehost` (the only disposition that lets translate
proceed - `retain`/`retire` block it, per `decide_disposition`'s own
docstring), sign every required role, wait for the exit gate, translate
every unit, wait for each translation to materialize, export the program
pack, trigger a preview deploy per translation, materialize each
translation's EXPECTED estate into this environment's own floci-az
(`deploy_apply`, via `arm_materialize.apply_expected_resources`), and ask
Prizmanyx to re-extract that estate over ARM and diff it against expected
(`verify`, via `PrizmanyxClient.verify_translation`) - the full round trip
this slice completes. See `report.FIDELITY_LINE`, which every written
report states verbatim, for exactly what each of `deploy_preview`,
`deploy_apply`, and `verify` does and does not prove.

Every step is recorded - name, PASSED/FAILED/UNRUN, timing - in
`LoopReport.steps`, in `STEP_ORDER` regardless of outcome: a step this run
never reached is still present, explicitly `UNRUN`, never simply absent.
The first `LoopStepError` any step raises ends the walk right there; every
later step in `STEP_ORDER` stays `UNRUN`. This function itself never raises
`LoopStepError` - a failing step becomes part of the returned report, not an
exception the caller must catch. (An exception of any OTHER type is a real
bug and is left to propagate - `run_loop` only ever narrows `LoopStepError`,
per this project's "catch what you mean" rule.)

Every gate the foundry walks on its own - accepting catalog defaults,
approving a unit, choosing `rehost`, signing a role - is a decision a human
reviewer would make in a real migration. `LoopReport.decisions` records each
one verbatim as it happens (`{step, choice, why: "foundry default"}`, plus a
`detail` naming which unit/role): the foundry is a scripted operator, not a
reviewer, and this list is the honesty surface that says so.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, TypeVar

import httpx
from pydantic import BaseModel, Field

from foundry import state
from foundry.arm_materialize import ArmApplyResult, apply_expected_resources
from foundry.engine import run_docker
from foundry.prizmanyx_client import JSONList, JSONObject, LoopStepError
from foundry.state import EnvState, ensure_env_dir

_T = TypeVar("_T")

# The injectable materializer seam `deploy_apply` calls through - defaults to
# the real `arm_materialize.apply_expected_resources`, overridable (as
# `run_loop`'s `materialize` parameter) so `tests/test_loop.py` can drive
# `deploy_apply` with a hand-written fake, exactly like `PipelineClient`
# lets tests drive every OTHER step with no network/docker at all. Typed as
# a plain 4-positional-argument `Callable` (not `arm_materialize`'s own
# richer signature, which also accepts `arm_client`/poll-budget keywords
# with defaults) because that is the exact shape `deploy_apply` calls it
# with - a fake only needs to satisfy this narrower shape.
ArmMaterializeFn = Callable[[str, str, str, JSONList], ArmApplyResult]

# The only account/region floci-backed environments ever present (see
# `PrizmanyxClient.register_env_connection`'s own docstring: every
# floci-backed environment forces "us-east-1", and this repo never mints
# real AWS credentials - "000000000000" is localstack's own placeholder
# account id). `start_extraction` targets exactly what was just registered.
_ACCOUNT_ID = "000000000000"
_REGION = "us-east-1"

# Extraction runs as a Temporal ACTIVITY inside the prizmanyx-temporal-
# worker CONTAINER, not in the host `uvicorn` process that merely receives
# this module's `register_env_connection` call (Task 8 live finding,
# 2026-08-31: Prizmanyx's own preflight refused a `localhost` endpoint as
# "unreachable from the discovery worker ... The worker runs in a
# container, so 'localhost' there is the container itself" - confirmed live
# with `docker exec prizmanyx-temporal-worker curl
# http://host.docker.internal:15600/` -> HTTP 200, `host.docker.internal`
# resolving to the host machine from inside that container). This is the
# SAME address form `materialize.py`'s own emitted terraform provider
# already targets, for the identical reason - one environment, one
# worker-reachable address, no matter which container asks for it.
# Overridable (see `worker_reachable_endpoint`, `run_loop`'s `worker_host`,
# and `foundry run --worker-host`) for a non-Docker-Desktop worker: a Linux
# host would use its own host-gateway address, and a worker joined to this
# environment's own compose network could use a service name instead.
DEFAULT_WORKER_HOST = "host.docker.internal"

# app.py's sign_off_store.SIGN_OFF_ROLES required subset for the
# `client_report` deliverable (see prizmanyx_client.sign_off's own
# docstring) - all four are mandatory for poll_exit_gate's
# `sign_offs_complete` condition to ever become true.
_SIGN_OFF_ROLES: tuple[str, ...] = ("engineer", "program_mgmt", "delivery_md", "customer")

_EXPORT_FORMAT = "csv"
_EXPORT_FILENAME = "export.csv"

# The placeholder Azure subscription every `deploy_apply`/`verify` call uses -
# there is no real Azure subscription anywhere in a bare foundry checkout,
# and floci-az itself does not validate the subscription segment of an ARM
# path, so one fixed placeholder is correct for every environment/run.
_AZ_PLACEHOLDER_SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000000"

# arm_materialize._RESOURCE_GROUP_TOKEN, duplicated rather than imported -
# this module reaches into `arm_materialize` for its public
# `apply_expected_resources`/`ArmApplyResult` surface only, never a private
# symbol across that boundary (the same discipline `arm_materialize` itself
# applies to `tf_runner`'s private `_running_floci_container_names`, per its
# own module docstring).
_RESOURCE_GROUP_TOKEN = "azure_native.resources.ResourceGroup"

# tf_runner._FLOCI_PS_FILTER / arm_materialize._FLOCI_PS_FILTER, duplicated
# for the same "no private cross-module reach" reason above - this module's
# OWN before/after `docker ps` snapshot around the whole `deploy_apply` call
# (see `_floci_children_snapshot`), independent of whatever
# `ArmApplyResult.spawned_children` the materializer itself manages to
# compute and return.
_FLOCI_PS_FILTER = "name=floci-"

# `list_translations` has no client-side poll helper (unlike
# poll_pipeline_run/poll_exit_gate) because the translation row is created
# by the translate workflow, not the translate_unit route itself - see that
# method's own docstring. Budget mirrors poll_exit_gate's own reasoning
# (waiting on a workflow-driven write becoming visible), scaled up because a
# translation workflow does real codegen work, unlike a synchronous DB
# write.
_TRANSLATION_POLL_MAX_ATTEMPTS = 30
_TRANSLATION_POLL_INTERVAL_SECONDS = 2.0

# The ordered pipeline this loop always walks, PASS or FAIL - the definitive
# list of `LoopReport.steps` names/positions. A step that groups several API
# calls (e.g. "approve", one `approve_unit` call per migration unit) keeps
# ONE name here; per-call detail lives in `LoopReport.decisions`, not as
# extra STEP_ORDER entries, so "FAIL(translate)" always names the step this
# list says it should.
STEP_ORDER: tuple[str, ...] = (
    "login",
    "create_program",
    "register_env_connection",
    "start_extraction",
    "poll_pipeline_run",
    "accept_catalog_defaults",
    "list_units",
    "approve",
    "disposition",
    "sign_off",
    "poll_exit_gate",
    "translate",
    "poll_translations",
    "export",
    "deploy_preview",
    "deploy_apply",
    "verify",
)

StepStatus = Literal["PASSED", "FAILED", "UNRUN"]
Verdict = Literal["PASS", "FAIL"]


class StepResult(BaseModel):
    """One `STEP_ORDER` entry's outcome: PASSED/FAILED with real timing, or
    UNRUN (a step the run never reached - `duration_seconds` stays `None`,
    never a fabricated `0.0`, so "never ran" and "ran instantly" can never
    be confused in the report)."""

    name: str
    status: StepStatus
    duration_seconds: float | None = None
    detail: str = ""


class Decision(BaseModel):
    """One auto-walked gate the foundry decided on its own, verbatim.

    `why` is always `"foundry default"` - there is no other reason this
    tool ever picks a disposition, approves a unit, or signs a role; it is
    a fixed field, not merely a common one. `detail` names the unit or role
    the decision applied to, when `choice` alone would not (every "approve"
    decision would otherwise be indistinguishable from every other one).
    """

    step: str
    choice: str
    why: str = "foundry default"
    detail: str = ""


class DeploymentResult(BaseModel):
    """One translation's `arm_materialize.apply_expected_resources` outcome,
    reduced to what a report needs - see `ArmApplyResult`'s own docstring for
    exactly what each field means (`ok` true iff every MAPPED resource
    reached a terminal success; `skipped_unmapped` is a coverage gap, not a
    failure). `resource_group` is the EXACT name this translation's expected
    estate was materialized under - the ResourceGroup row's own
    `azure_logical_name` from `list_translation_resources`, never a
    foundry-invented name (see `_expected_resource_group_name`) - and is the
    same name the matching `VerificationResult` for this `translation_id`
    carries, by construction."""

    translation_id: int
    resource_group: str
    ok: bool
    applied: int
    skipped_unmapped: list[str] = Field(default_factory=list)
    failures: list[dict[str, Any]] = Field(default_factory=list)
    provisioning_timeouts: list[str] = Field(default_factory=list)


class VerificationResult(BaseModel):
    """One translation's `PrizmanyxClient.verify_translation` outcome,
    recorded verbatim - Prizmanyx's own `VerifyResponse` counts and
    `all_matched` verdict, never reworded or re-derived by this module.
    `resource_group` is the same name `DeploymentResult.resource_group`
    carries for this `translation_id` - the one `verify_translation` was
    actually asked to diff."""

    translation_id: int
    resource_group: str
    verification_run_id: int | None = None
    all_matched: bool
    matched_count: int
    mismatch_count: int
    missing_count: int
    extra_count: int


class LoopReport(BaseModel):
    """The complete, honest record of one `run_loop` call.

    `steps` always has exactly `len(STEP_ORDER)` entries, in that order,
    regardless of verdict. `failed_step`/`failure_detail` are set together
    and only on `FAIL` - `failure_detail` is the failing `LoopStepError`'s
    own `detail_verbatim`, never reworded. `decisions`, `unit_ids`, and
    `translation_ids` all reflect real, already-happened progress even on a
    `FAIL`: a batch that fails partway through (an approval, a translate, a
    translation-id poll) leaves every entry it genuinely produced before the
    failure in the report - none of these ever collapse to empty just
    because the batch as a whole did not finish.

    `worker_endpoint_url` is the exact value this run sent to
    `register_env_connection` (see `worker_reachable_endpoint`) - the
    address Prizmanyx's extraction WORKER container reaches this
    environment at, deliberately distinct from the `localhost` address the
    foundry's own host-side boto3 verification calls correctly use instead.
    Never treat the two as interchangeable just because both name the same
    environment.
    """

    env_name: str
    verdict: Verdict
    failed_step: str | None = None
    failure_detail: str | None = None
    steps: list[StepResult]
    decisions: list[Decision] = Field(default_factory=list)
    program_id: int | None = None
    graph_run_id: int | None = None
    worker_endpoint_url: str | None = None
    unit_ids: list[int] = Field(default_factory=list)
    translation_ids: list[int] = Field(default_factory=list)
    artifact_paths: list[str] = Field(default_factory=list)
    deployment_results: list[DeploymentResult] = Field(default_factory=list)
    verification_results: list[VerificationResult] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime


class PipelineClient(Protocol):
    """The exact `PrizmanyxClient` surface `run_loop` depends on.

    A `Protocol`, not `PrizmanyxClient` itself, so tests can drive `run_loop`
    with a hand-written fake - no `httpx`, no network, no running Prizmanyx
    at all (see `tests/test_loop.py`). Every signature here is copied from
    `prizmanyx_client.PrizmanyxClient`; keep the two in sync by hand, since a
    `Protocol` cannot inherit method bodies to enforce that automatically.
    """

    def login(self) -> JSONObject: ...

    def create_program(
        self, name: str, description: str | None = None, direction: str = "aws_to_azure"
    ) -> JSONObject: ...

    def register_env_connection(
        self,
        program_id: int,
        endpoint_url: str,
        *,
        region: str = "us-east-1",
        account_id: str = "000000000000",
        label: str | None = None,
    ) -> JSONObject: ...

    def start_extraction(
        self,
        program_id: int,
        account_ids: Sequence[str],
        regions: Sequence[str],
        *,
        start_temporal: bool = True,
        **extra: object,
    ) -> JSONObject: ...

    def poll_pipeline_run(
        self, run_id: int, *, max_attempts: int = 60, interval: float = 5.0
    ) -> JSONObject: ...

    def accept_catalog_defaults(self, program_id: int) -> JSONObject: ...

    def list_units(self, graph_run_id: int) -> JSONList: ...

    def approve_unit(self, unit_id: int) -> JSONObject: ...

    def decide_disposition(
        self, unit_id: int, disposition: str = "rehost", *, rationale: str | None = None
    ) -> JSONObject: ...

    def sign_off(
        self,
        program_id: int,
        role: str,
        *,
        deliverable: str = "client_report",
        comment: str | None = None,
    ) -> JSONObject: ...

    def poll_exit_gate(
        self, program_id: int, *, max_attempts: int = 10, interval: float = 1.0
    ) -> JSONObject: ...

    def translate_unit(self, unit_id: int) -> JSONObject: ...

    def list_translations(self, unit_id: int) -> JSONList: ...

    def export_program(self, program_id: int, fmt: str = "csv") -> httpx.Response: ...

    def deploy_preview(
        self,
        translation_id: int,
        *,
        azure_subscription_id: str = "00000000-0000-0000-0000-000000000000",
        azure_resource_group: str = "foundry-preview-rg",
    ) -> JSONObject: ...

    def list_translation_resources(self, translation_id: int) -> JSONList: ...

    def verify_translation(
        self,
        translation_id: int,
        *,
        azure_subscription_id: str,
        azure_resource_group: str,
    ) -> JSONObject: ...


def _program_name(env: EnvState) -> str:
    """A fresh program name for one `run_loop` call.

    Timestamped to microsecond resolution so repeated runs against the SAME
    environment - the common case when iterating on a fix - never look like
    the same run twice in Prizmanyx's own program list; whether the route
    itself would even accept a duplicate name is untested and beside the
    point either way.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    return f"foundry-{env.name}-{stamp}"


def worker_reachable_endpoint(env: EnvState, *, worker_host: str = DEFAULT_WORKER_HOST) -> str:
    """The AWS endpoint URL Prizmanyx's discovery WORKER can actually reach.

    `register_env_connection`'s `endpoint_url` is stored once, then read
    back later by whatever process performs extraction - and that is a
    Temporal ACTIVITY running inside the `prizmanyx-temporal-worker`
    container, never the host `uvicorn` process that merely receives this
    registration call (see `DEFAULT_WORKER_HOST`'s own comment for the
    live Task 8 finding that proved this: a `localhost` endpoint was
    refused by Prizmanyx's own preflight as unreachable from that worker).
    `host.docker.internal` is Docker Desktop's own name for "the host
    machine, from inside any container" - the same form
    `materialize.py`'s emitted terraform provider already uses to reach
    this same environment, for the identical reason.

    This is deliberately NOT the address the foundry's OWN host-side boto3
    verification calls should use - those run in the foundry's own Python
    process, on the host, where `localhost` is correct and
    `host.docker.internal` would be wrong. Never point boto3 calls at this
    function's output; it exists for `register_env_connection` only.

    `worker_host` defaults to `DEFAULT_WORKER_HOST` and is the one thing a
    non-Docker-Desktop worker - a Linux host-gateway address, or a worker
    joined to this environment's own compose network under a service name -
    would need to override.
    """
    return f"http://{worker_host}:{env.ports['aws']}"


def _ensure_pipeline_run_succeeded(row: JSONObject) -> JSONObject:
    """Turn a non-`succeeded` terminal pipeline-run status into a `LoopStepError`.

    `PrizmanyxClient.poll_pipeline_run` deliberately does not raise for a
    `failed`/`canceled` terminal status - per its own docstring, that is "a
    real outcome for the loop to interpret, not a client-level error." This
    is that interpretation: the loop's own `poll_pipeline_run` STEP must
    still fail the run when the pipeline itself did not succeed, quoting
    whatever `error_message` the run row carries (falling back to the bare
    status when there is none) - never treating a non-succeeded run as good
    enough to keep going.
    """
    status = row.get("status")
    if status != "succeeded":
        detail = row.get("error_message") or f"pipeline run ended with status={status!r}"
        raise LoopStepError("poll_pipeline_run", status or "unknown", str(detail))
    return row


def _poll_translation_id(
    client: PipelineClient, unit_id: int, *, max_attempts: int, interval: float
) -> int:
    """Poll `list_translations(unit_id)` until an entry carries a non-null `id`.

    Modeled on `PrizmanyxClient._poll`'s own shape (fetch, check, sleep,
    give up after `max_attempts`) since there is no client-side poll to
    reuse across the module boundary for this particular wait.
    """
    for attempt in range(1, max_attempts + 1):
        for translation in client.list_translations(unit_id):
            translation_id = translation.get("id")
            if translation_id is not None:
                return int(translation_id)
        if attempt < max_attempts:
            time.sleep(interval)
    raise LoopStepError(
        "poll_translations",
        "timeout",
        f"no translation id appeared for migration unit {unit_id} after "
        f"{max_attempts} attempts at {interval}s intervals",
    )


def _expected_resource_group_name(resources: JSONList, translation_id: int) -> str:
    """The ResourceGroup row's own `azure_logical_name` from
    `list_translation_resources(translation_id)`'s output - the ONLY name
    `deploy_apply` and `verify` may pass as `resource_group` for this
    translation.

    This is the RG-naming closure the round-trip depends on: the verify
    bridge matches a deployed resource group's NAME against the
    translator's own expected `ResourceGroup` `azure_logical_name` (see
    `arm_materialize`'s own module docstring, contract edge 2). A
    foundry-invented name here - even a deterministic one - would make
    every resource in the group read as "missing" at verify, silently,
    while `deploy_apply` itself could still report `ok`.

    First match wins: codegen emits exactly one `ResourceGroup` scaffold
    per migration unit, so a second row is not an expected shape this
    function needs to detect or reject - it is a deliberate "pick the
    first" choice, not an accidental one.
    """
    for resource in resources:
        if resource.get("azure_resource_type") == _RESOURCE_GROUP_TOKEN:
            name = resource.get("azure_logical_name")
            if name:
                return str(name)
    raise LoopStepError(
        "deploy_apply",
        "missing_resource_group",
        f"translation {translation_id}'s expected-resources carried no "
        f"{_RESOURCE_GROUP_TOKEN!r} row to name the resource group after",
    )


def _floci_children_snapshot() -> set[str]:
    """Every currently-RUNNING container whose name starts with `floci-`.

    This module's OWN before/after snapshot around the whole `deploy_apply`
    call for one translation - independent of, and in addition to,
    `ArmApplyResult.spawned_children` (see `_persist_new_children`'s own
    docstring for why both are unioned rather than trusting either alone).
    """
    result = run_docker(["ps", "--filter", _FLOCI_PS_FILTER, "--format", "{{.Names}}"])
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def run_loop(
    env: EnvState,
    client: PipelineClient,
    *,
    worker_host: str = DEFAULT_WORKER_HOST,
    translation_poll_max_attempts: int = _TRANSLATION_POLL_MAX_ATTEMPTS,
    translation_poll_interval: float = _TRANSLATION_POLL_INTERVAL_SECONDS,
    materialize: ArmMaterializeFn = apply_expected_resources,
) -> LoopReport:
    """Walk `env`'s estate through Prizmanyx's pipeline via `client`, and
    return a full `LoopReport` - PASS or FAIL, but always a report, never a
    raised `LoopStepError` (see this module's own docstring for why).

    `worker_host` is passed straight to `worker_reachable_endpoint` for the
    `register_env_connection` call - see that function's own docstring for
    why this can't just be `env`'s own `localhost` address.

    `materialize` is the `deploy_apply` step's own injectable seam (see
    `ArmMaterializeFn`) - defaults to the real
    `arm_materialize.apply_expected_resources`, overridable so tests can
    drive `deploy_apply` with no network/docker, exactly like `client`
    already lets every other step be driven with no network at all.
    """
    started_at = datetime.now(UTC)
    steps: dict[str, StepResult] = {
        name: StepResult(name=name, status="UNRUN") for name in STEP_ORDER
    }
    decisions: list[Decision] = []
    program_id: int | None = None
    graph_run_id: int | None = None
    worker_endpoint_url: str | None = None
    unit_ids: list[int] = []
    translation_ids: list[int] = []
    artifact_paths: list[str] = []
    deployment_results: list[DeploymentResult] = []
    verification_results: list[VerificationResult] = []
    # Populated by `deploy_apply` as each translation's expected resource
    # group name is read off its own `list_translation_resources` output -
    # `verify` reuses these EXACT values rather than re-deriving them, so
    # the two steps can never drift apart on what "this translation's
    # resource group" means (the RG-naming closure `_expected_resource_group_
    # name` documents).
    translation_resource_groups: dict[int, str] = {}
    # The ownership-by-recording set this run's own `deploy_apply` has
    # confirmed spawned in floci-az so far - seeded from whatever `env`
    # already carries (a prior run against the same environment), so a
    # second `deploy_apply` batch never loses track of the first's
    # children. Persisted after EVERY translation's materialize attempt,
    # success or failure or crash (see `_persist_new_children`), never
    # batched to the end of the step.
    known_children: set[str] = set(env.children or [])
    failed_step: str | None = None
    failure_detail: str | None = None

    def _persist_new_children(new_children: set[str]) -> None:
        """Union `new_children` into `known_children` and persist `env`
        (with `children` updated) to disk immediately, if there is anything
        new to record.

        Called after EVERY `deploy_apply` materialize attempt for one
        translation - including one that raised, per carry #2 of Task 3's
        review (a transport error mid-materialize can propagate before
        `arm_materialize`'s OWN before/after snapshot completes, which
        would otherwise orphan whatever floci-az child a partial PUT batch
        already spawned). Mirrors `tf_runner.apply_in_dir`'s own
        union-not-overwrite persistence exactly, so `engine.remove_env`
        reaps these the same way it reaps the AWS side's children.
        """
        if not new_children:
            return
        known_children.update(new_children)
        state.save_state(env.model_copy(update={"children": sorted(known_children)}))

    def _step(name: str, fn: Callable[[], _T]) -> _T:
        nonlocal failed_step, failure_detail
        start = time.monotonic()
        try:
            result = fn()
        except LoopStepError as exc:
            steps[name] = StepResult(
                name=name,
                status="FAILED",
                duration_seconds=time.monotonic() - start,
                detail=exc.detail_verbatim,
            )
            failed_step = name
            failure_detail = exc.detail_verbatim
            raise
        steps[name] = StepResult(
            name=name, status="PASSED", duration_seconds=time.monotonic() - start
        )
        return result

    try:
        _step("login", client.login)

        program = _step(
            "create_program",
            lambda: client.create_program(
                _program_name(env), description=f"foundry loop run against env {env.name!r}"
            ),
        )
        program_id = program["id"]

        worker_endpoint_url = worker_reachable_endpoint(env, worker_host=worker_host)
        _step(
            "register_env_connection",
            lambda: client.register_env_connection(program_id, worker_endpoint_url),
        )

        extraction = _step(
            "start_extraction",
            lambda: client.start_extraction(program_id, [_ACCOUNT_ID], [_REGION]),
        )
        run_id = extraction["id"]

        final_run = _step(
            "poll_pipeline_run",
            lambda: _ensure_pipeline_run_succeeded(client.poll_pipeline_run(run_id)),
        )
        graph_run_id = final_run.get("graph_run_id")

        _step("accept_catalog_defaults", lambda: client.accept_catalog_defaults(program_id))
        decisions.append(Decision(step="accept_catalog_defaults", choice="accept-defaults"))

        units = _step("list_units", lambda: client.list_units(graph_run_id))
        unit_ids = [unit["id"] for unit in units]

        def _approve_every_unit() -> None:
            for unit_id in unit_ids:
                client.approve_unit(unit_id)
                decisions.append(
                    Decision(step="approve", choice="approve", detail=f"unit {unit_id}")
                )

        _step("approve", _approve_every_unit)

        def _decide_every_disposition() -> None:
            for unit_id in unit_ids:
                client.decide_disposition(unit_id, "rehost")
                decisions.append(
                    Decision(step="disposition", choice="rehost", detail=f"unit {unit_id}")
                )

        _step("disposition", _decide_every_disposition)

        def _sign_off_every_role() -> None:
            for role in _SIGN_OFF_ROLES:
                client.sign_off(program_id, role)
                decisions.append(Decision(step="sign_off", choice=role))

        _step("sign_off", _sign_off_every_role)

        _step("poll_exit_gate", lambda: client.poll_exit_gate(program_id))

        def _translate_every_unit() -> None:
            for unit_id in unit_ids:
                client.translate_unit(unit_id)

        _step("translate", _translate_every_unit)

        def _poll_every_translation() -> None:
            # Appends to the OUTER translation_ids list as each unit resolves,
            # exactly like `decisions` above - so a timeout on unit N still
            # leaves units 1..N-1's real, already-resolved ids in the report
            # instead of collapsing the whole batch's progress to nothing.
            for unit_id in unit_ids:
                translation_ids.append(
                    _poll_translation_id(
                        client,
                        unit_id,
                        max_attempts=translation_poll_max_attempts,
                        interval=translation_poll_interval,
                    )
                )

        _step("poll_translations", _poll_every_translation)

        def _export_pack() -> str:
            response = client.export_program(program_id, fmt=_EXPORT_FORMAT)
            path = ensure_env_dir(env.name) / _EXPORT_FILENAME
            path.write_bytes(response.content)
            return str(path)

        artifact_paths.append(_step("export", _export_pack))

        def _deploy_every_translation_preview() -> None:
            for translation_id in translation_ids:
                client.deploy_preview(translation_id)

        _step("deploy_preview", _deploy_every_translation_preview)

        def _deploy_apply_one(translation_id: int) -> None:
            resources = client.list_translation_resources(translation_id)
            resource_group = _expected_resource_group_name(resources, translation_id)
            translation_resource_groups[translation_id] = resource_group
            az_base_url = f"http://localhost:{env.ports['az']}"

            before = _floci_children_snapshot()
            result: ArmApplyResult | None = None
            try:
                result = materialize(
                    az_base_url, _AZ_PLACEHOLDER_SUBSCRIPTION_ID, resource_group, resources
                )
            finally:
                # Runs whether `materialize` returned normally OR raised -
                # carry #2: a transport error mid-materialize can propagate
                # BEFORE arm_materialize's own before/after snapshot
                # completes, so this module's OWN snapshot, taken around the
                # whole call, is what keeps a crash from orphaning whatever
                # floci-az child a partial PUT batch already spawned.
                # Unioned with the materializer's own reported
                # `spawned_children` (when it did return) rather than
                # trusting either alone.
                after = _floci_children_snapshot()
                reported = set(result.spawned_children) if result is not None else set()
                _persist_new_children((after - before) | reported)

            assert result is not None  # the `finally` above re-raises otherwise
            deployment_results.append(
                DeploymentResult(
                    translation_id=translation_id,
                    resource_group=resource_group,
                    ok=result.ok,
                    applied=result.applied,
                    skipped_unmapped=result.skipped_unmapped,
                    failures=result.failures,
                    provisioning_timeouts=result.provisioning_timeouts,
                )
            )
            if not result.ok:
                raise LoopStepError(
                    "deploy_apply",
                    "materialize_failed",
                    f"translation {translation_id} (resource_group={resource_group!r}): "
                    f"failures={result.failures!r} "
                    f"provisioning_timeouts={result.provisioning_timeouts!r}",
                )

        def _deploy_every_translation_apply() -> None:
            for translation_id in translation_ids:
                _deploy_apply_one(translation_id)

        _step("deploy_apply", _deploy_every_translation_apply)

        def _verify_one(translation_id: int) -> None:
            resource_group = translation_resource_groups[translation_id]
            raw = client.verify_translation(
                translation_id,
                azure_subscription_id=_AZ_PLACEHOLDER_SUBSCRIPTION_ID,
                azure_resource_group=resource_group,
            )
            result = VerificationResult(
                translation_id=translation_id,
                resource_group=resource_group,
                verification_run_id=raw.get("verification_run_id"),
                all_matched=bool(raw.get("all_matched")),
                matched_count=int(raw.get("matched_count") or 0),
                mismatch_count=int(raw.get("mismatch_count") or 0),
                missing_count=int(raw.get("missing_count") or 0),
                extra_count=int(raw.get("extra_count") or 0),
            )
            verification_results.append(result)
            if not result.all_matched:
                raise LoopStepError(
                    "verify",
                    "not_all_matched",
                    f"translation {translation_id} (resource_group={resource_group!r}): "
                    f"matched={result.matched_count} missing={result.missing_count} "
                    f"extra={result.extra_count} mismatch={result.mismatch_count}",
                )

        def _verify_every_translation() -> None:
            for translation_id in translation_ids:
                _verify_one(translation_id)

        _step("verify", _verify_every_translation)

    except LoopStepError:
        pass  # already recorded by _step; every later STEP_ORDER entry stays UNRUN

    verdict: Verdict = "FAIL" if failed_step is not None else "PASS"

    return LoopReport(
        env_name=env.name,
        verdict=verdict,
        failed_step=failed_step,
        failure_detail=failure_detail,
        steps=[steps[name] for name in STEP_ORDER],
        decisions=decisions,
        program_id=program_id,
        graph_run_id=graph_run_id,
        worker_endpoint_url=worker_endpoint_url,
        unit_ids=unit_ids,
        translation_ids=translation_ids,
        artifact_paths=artifact_paths,
        deployment_results=deployment_results,
        verification_results=verification_results,
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )
