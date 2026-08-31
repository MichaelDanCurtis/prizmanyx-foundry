"""Tests for foundry.loop and foundry.report: the pipeline walk, its
PASS/FAIL verdict, the decisions honesty table, and the report.md/report.json
files it produces - all against a hand-written FAKED `PrizmanyxClient` (see
`foundry.loop.PipelineClient`, the Protocol this fake satisfies structurally).
No network, no httpx transport, no running Prizmanyx anywhere in this file.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from foundry import state
from foundry.arm_materialize import ArmApplyResult
from foundry.loop import (
    DEFAULT_WORKER_HOST,
    STEP_ORDER,
    Decision,
    LoopReport,
    StepResult,
    run_loop,
    worker_reachable_endpoint,
)
from foundry.prizmanyx_client import LoopStepError
from foundry.report import FIDELITY_LINE, load_report, render_markdown, write_report
from foundry.state import EnvState, env_dir


@pytest.fixture(autouse=True)
def _isolated_envs_dir(tmp_path, monkeypatch):
    # Same isolation as tests/test_state.py - run_loop's own export step
    # writes into the env's real directory via state.ensure_env_dir, so this
    # must never touch the real ~/.prizmanyx-foundry on the machine running
    # the suite.
    monkeypatch.setattr(state, "ENVS_DIR", tmp_path / "envs")


@pytest.fixture(autouse=True)
def _no_real_docker(monkeypatch):
    # deploy_apply's OWN before/after `docker ps` snapshot (loop.py's
    # `_floci_children_snapshot`) must never shell out for real in this
    # file - every test here fakes both the client and the materializer
    # (see the module docstring), so docker must be faked too. Defaults to
    # "no containers, ever" (empty before AND after every call); tests that
    # care about a spawned-children delta drive it through the fake
    # `materialize`'s own `spawned_children` field instead (see
    # `_FakeDocker` below for the one test that DOES want a real ps delta).
    def _empty_ps(args, *, timeout=None):  # noqa: ARG001
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("foundry.loop.run_docker", _empty_ps)


class _FakeDocker:
    """Answers `docker ps --filter name=floci-` from a queue of canned name
    snapshots, one per call - for the one test that exercises loop.py's OWN
    before/after diff directly (`_no_real_docker`'s default answers "no
    containers" for everything else)."""

    def __init__(self, snapshots: list[set[str]]) -> None:
        self._snapshots = [set(s) for s in snapshots]

    def __call__(self, args, *, timeout=None):  # noqa: ARG002
        names = self._snapshots.pop(0) if self._snapshots else set()
        return subprocess.CompletedProcess(
            args, 0, stdout="".join(f"{n}\n" for n in sorted(names)), stderr=""
        )


def _env(name: str = "demo", port: int = 15600, az_port: int = 14577) -> EnvState:
    return EnvState(
        name=name,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ports={"aws": port, "az": az_port},
        spec_path=Path(f"/envs/{name}/spec.yaml"),
        status="running",
    )


class _FakeClient:
    """A hand-written double satisfying `foundry.loop.PipelineClient`
    structurally - every method the loop calls, none of the polling/HTTP
    machinery `PrizmanyxClient` itself needs.

    `translate_fails_at`/`approve_fails_at`, when set, make `translate_unit`/
    `approve_unit` raise a `LoopStepError` for that one migration unit id
    (and only that one) - the fixtures behind the "409 at translate" and
    "failure at a non-boundary unit" tests. `translation_never_appears_for`
    makes `list_translations` return empty forever for that one unit id,
    forcing `_poll_translation_id` to exhaust its attempt budget and time
    out - the fixture behind the partial-translation-ids test.

    `expected_resources`, when set, maps translation id -> the
    `list_translation_resources` row list to return for it - defaults to a
    ResourceGroup row named `rg-<tid>` plus one StorageAccount row, which is
    enough to exercise the RG-naming closure (`deploy_apply`/`verify` must
    both use `rg-<tid>`, never a foundry-invented name). `verify_responses`,
    when set, maps translation id -> the `verify_translation` JSON body to
    return for it - defaults to an all-matched, zero-mismatch verdict.
    `verify_502_for`, when set, makes `verify_translation` raise the same
    `LoopStepError` shape the real 502 "bridge unreachable" response
    produces, for that one translation id.
    """

    def __init__(
        self,
        *,
        units: list[dict[str, object]] | None = None,
        translate_fails_at: int | None = None,
        approve_fails_at: int | None = None,
        translation_never_appears_for: int | None = None,
        expected_resources: dict[int, list[dict[str, object]]] | None = None,
        verify_responses: dict[int, dict[str, object]] | None = None,
        verify_502_for: int | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._units = units if units is not None else [{"id": 1}, {"id": 2}]
        self._translate_fails_at = translate_fails_at
        self._approve_fails_at = approve_fails_at
        self._translation_never_appears_for = translation_never_appears_for
        self._expected_resources = expected_resources or {}
        self._verify_responses = verify_responses or {}
        self._verify_502_for = verify_502_for
        self.verify_requests: list[tuple[int, str, str]] = []
        # Populated as the loop calls through - every parameter below is
        # captured somewhere rather than silently accepted-and-dropped, so
        # a test can assert on exactly what the loop sent without this
        # class carrying dead, unused-argument-lint-suppressed parameters.
        self.registered_endpoint_url: str | None = None

    def login(self):
        self.calls.append("login")
        return {"id": 1, "username": "admin"}

    def create_program(self, name, description=None):
        self.calls.append("create_program")
        self.created_program_description = description
        return {"id": 7, "name": name}

    def register_env_connection(self, program_id, endpoint_url):
        self.calls.append("register_env_connection")
        self.registered_endpoint_url = endpoint_url
        return {"id": 1, "status": "connected", "program_id": program_id}

    def start_extraction(self, program_id, account_ids, regions):
        self.calls.append("start_extraction")
        return {
            "id": 42,
            "status": "pending",
            "program_id": program_id,
            "account_ids": list(account_ids),
            "regions": list(regions),
        }

    def poll_pipeline_run(self, run_id):
        self.calls.append("poll_pipeline_run")
        return {"id": run_id, "status": "succeeded", "graph_run_id": 55}

    def accept_catalog_defaults(self, program_id):
        self.calls.append("accept_catalog_defaults")
        return {"reviewed_at": "2026-08-31T00:00:00Z", "entries": [], "program_id": program_id}

    def list_units(self, graph_run_id):
        self.calls.append("list_units")
        self.listed_graph_run_id = graph_run_id
        return [dict(unit) for unit in self._units]

    def approve_unit(self, unit_id):
        self.calls.append(f"approve_unit:{unit_id}")
        if self._approve_fails_at is not None and unit_id == self._approve_fails_at:
            raise LoopStepError("approve_unit", 409, f"migration unit {unit_id} is not reviewable")
        return {"id": unit_id, "review_status": "approved"}

    def decide_disposition(self, unit_id, disposition="rehost"):
        self.calls.append(f"decide_disposition:{unit_id}")
        return {"id": unit_id, "disposition": disposition}

    def sign_off(self, program_id, role):
        self.calls.append(f"sign_off:{role}")
        return {"gate": {"complete": role == "customer"}, "program_id": program_id}

    def poll_exit_gate(self, program_id):
        self.calls.append("poll_exit_gate")
        return {
            "passed": True,
            "conditions": {"dispositions_decided": True, "sign_offs_complete": True},
            "program_id": program_id,
        }

    def translate_unit(self, unit_id):
        self.calls.append(f"translate_unit:{unit_id}")
        if self._translate_fails_at is not None and unit_id == self._translate_fails_at:
            raise LoopStepError("translate_unit", 409, "migration unit is not approved")
        return {"workflow_id": f"wf-{unit_id}", "migration_unit_id": unit_id}

    def list_translations(self, unit_id):
        self.calls.append(f"list_translations:{unit_id}")
        if unit_id == self._translation_never_appears_for:
            return []
        return [{"id": 100 + unit_id, "migration_unit_id": unit_id}]

    def export_program(self, program_id, fmt="csv"):
        self.calls.append("export_program")
        self.exported_program_id = program_id
        self.exported_format = fmt
        return httpx.Response(200, content=b"section,value\nprogram,7\n")

    def deploy_preview(self, translation_id):
        self.calls.append(f"deploy_preview:{translation_id}")
        return {"workflow_id": "wf-deploy", "mode": "preview", "translation_id": translation_id}

    def list_translation_resources(self, translation_id):
        self.calls.append(f"list_translation_resources:{translation_id}")
        return self._expected_resources.get(
            translation_id,
            [
                {
                    "azure_resource_type": "azure_native.resources.ResourceGroup",
                    "azure_logical_name": f"rg-{translation_id}",
                    "azure_properties": {},
                },
                {
                    "azure_resource_type": "azure_native.storage.StorageAccount",
                    "azure_logical_name": f"sa-{translation_id}",
                    "azure_properties": {},
                },
            ],
        )

    def verify_translation(self, translation_id, *, azure_subscription_id, azure_resource_group):
        self.calls.append(f"verify_translation:{translation_id}")
        self.verify_requests.append((translation_id, azure_subscription_id, azure_resource_group))
        if translation_id == self._verify_502_for:
            raise LoopStepError("verify", 502, "ARM endpoint http://floci-az:8080 unreachable")
        return self._verify_responses.get(
            translation_id,
            {
                "verification_run_id": 900 + translation_id,
                "all_matched": True,
                "matched_count": 2,
                "mismatch_count": 0,
                "missing_count": 0,
                "extra_count": 0,
            },
        )


class _FakeMaterializer:
    """A hand-written double for `run_loop`'s `materialize` seam
    (`foundry.loop.ArmMaterializeFn`) - the fake behind every `deploy_apply`
    test, no `arm_materialize`, no docker, no httpx.

    `fail_for`, when set, makes the call for that one translation id return
    an `ArmApplyResult` with `ok=False` and a canned failure - every OTHER
    translation still gets a clean `ok=True` result. `spawned_children`
    applies to every call.
    """

    def __init__(
        self,
        *,
        fail_for: int | None = None,
        spawned_children: list[str] | None = None,
        applied: int = 2,
    ) -> None:
        self.calls: list[tuple[str, str, str, list[dict[str, object]]]] = []
        self._fail_for = fail_for
        self._spawned_children = spawned_children or []
        self._applied = applied

    def __call__(self, az_base_url, subscription_id, resource_group, resources):
        self.calls.append((az_base_url, subscription_id, resource_group, resources))
        # The resources list always carries the RG row plus the fixture's
        # own translation id baked into every logical name (see
        # `_FakeClient.list_translation_resources`'s default) - recover it
        # from `resource_group` ("rg-<tid>") to decide fail_for cleanly.
        translation_id = int(resource_group.removeprefix("rg-"))
        if translation_id == self._fail_for:
            return ArmApplyResult(
                ok=False,
                applied=0,
                skipped_unmapped=[],
                failures=[{"name": "sa-x", "status": 400, "body": {"error": "bad request"}}],
                provisioning_timeouts=[],
                spawned_children=self._spawned_children,
            )
        return ArmApplyResult(
            ok=True,
            applied=self._applied,
            skipped_unmapped=[],
            failures=[],
            provisioning_timeouts=[],
            spawned_children=self._spawned_children,
        )


def _run(env: EnvState, client, **kwargs) -> LoopReport:
    """`run_loop`, defaulting `materialize` to a clean `_FakeMaterializer()`
    when the caller does not supply one.

    Every pre-existing test in this file predates `deploy_apply` and calls
    `run_loop` with no opinion at all about materializing - `run_loop`'s own
    default `materialize` is the REAL `arm_materialize.apply_expected_
    resources`, which would make a genuine httpx call against
    `http://localhost:<az_port>` with no floci-az listening there in this
    hermetic suite. Routing every call in this file through here (instead of
    `run_loop` directly) keeps that default from ever firing unfaked - the
    same "no network, no httpx transport" promise this module's own
    docstring already makes for `client`.
    """
    kwargs.setdefault("materialize", _FakeMaterializer())
    return run_loop(env, client, **kwargs)


def _step_statuses(report: LoopReport) -> dict[str, str]:
    return {step.name: step.status for step in report.steps}


# --- happy path: every step PASSED, every decision recorded ----------------


def test_happy_path_is_pass_with_every_step_recorded():
    client = _FakeClient()
    report = _run(_env(), client)

    assert report.verdict == "PASS"
    assert report.failed_step is None
    assert report.failure_detail is None
    assert [step.name for step in report.steps] == list(STEP_ORDER)
    assert all(step.status == "PASSED" for step in report.steps)
    assert all(step.duration_seconds is not None for step in report.steps)
    assert all(step.duration_seconds >= 0 for step in report.steps)


def test_happy_path_captures_program_graph_run_units_and_translation_ids():
    client = _FakeClient()
    report = _run(_env(), client)

    assert report.program_id == 7
    assert report.graph_run_id == 55
    assert report.unit_ids == [1, 2]
    assert report.translation_ids == [101, 102]


def test_happy_path_registers_the_worker_reachable_endpoint_by_default():
    # Extraction runs as a Temporal ACTIVITY inside the prizmanyx-temporal-
    # worker container, not the host `uvicorn` process that receives this
    # registration call - `localhost` from inside that container is the
    # container itself, unreachable (Task 8 live finding). The default must
    # be the worker-reachable form, not the host-reachable one.
    client = _FakeClient()
    _run(_env(port=15900), client)

    assert client.registered_endpoint_url == "http://host.docker.internal:15900"


def test_run_loop_honors_a_worker_host_override():
    # A Linux host-gateway, or a worker sharing this environment's own
    # compose network under a service name, would need something other
    # than Docker Desktop's host.docker.internal.
    client = _FakeClient()
    _run(_env(port=15900), client, worker_host="172.17.0.1")

    assert client.registered_endpoint_url == "http://172.17.0.1:15900"


def test_worker_reachable_endpoint_default_and_override():
    env = _env(port=15900)

    assert DEFAULT_WORKER_HOST == "host.docker.internal"
    assert worker_reachable_endpoint(env) == "http://host.docker.internal:15900"
    assert worker_reachable_endpoint(env, worker_host="my-worker") == "http://my-worker:15900"


def test_happy_path_records_the_worker_endpoint_url_on_the_report():
    client = _FakeClient()
    report = _run(_env(port=15900), client)

    assert report.worker_endpoint_url == "http://host.docker.internal:15900"
    # It must be exactly what was actually sent to register_env_connection -
    # never a value the report merely computed independently and hoped
    # matched.
    assert report.worker_endpoint_url == client.registered_endpoint_url


def test_happy_path_writes_the_export_pack_into_the_envs_directory():
    client = _FakeClient()
    report = _run(_env(), client)

    assert len(report.artifact_paths) == 1
    exported = Path(report.artifact_paths[0])
    assert exported == env_dir("demo") / "export.csv"
    assert exported.read_bytes() == b"section,value\nprogram,7\n"


def test_happy_path_calls_every_client_method_in_pipeline_order():
    client = _FakeClient()
    _run(_env(), client)

    assert client.calls == [
        "login",
        "create_program",
        "register_env_connection",
        "start_extraction",
        "poll_pipeline_run",
        "accept_catalog_defaults",
        "list_units",
        "approve_unit:1",
        "approve_unit:2",
        "decide_disposition:1",
        "decide_disposition:2",
        "sign_off:engineer",
        "sign_off:program_mgmt",
        "sign_off:delivery_md",
        "sign_off:customer",
        "poll_exit_gate",
        "translate_unit:1",
        "translate_unit:2",
        "list_translations:1",
        "list_translations:2",
        "export_program",
        "deploy_preview:101",
        "deploy_preview:102",
        "list_translation_resources:101",
        "list_translation_resources:102",
        "verify_translation:101",
        "verify_translation:102",
    ]


# --- decisions: every auto-walked gate, verbatim ----------------------------


def test_decisions_table_carries_every_auto_gate_verbatim():
    client = _FakeClient()
    report = _run(_env(), client)

    assert report.decisions == [
        Decision(step="accept_catalog_defaults", choice="accept-defaults", detail=""),
        Decision(step="approve", choice="approve", detail="unit 1"),
        Decision(step="approve", choice="approve", detail="unit 2"),
        Decision(step="disposition", choice="rehost", detail="unit 1"),
        Decision(step="disposition", choice="rehost", detail="unit 2"),
        Decision(step="sign_off", choice="engineer", detail=""),
        Decision(step="sign_off", choice="program_mgmt", detail=""),
        Decision(step="sign_off", choice="delivery_md", detail=""),
        Decision(step="sign_off", choice="customer", detail=""),
    ]
    assert all(decision.why == "foundry default" for decision in report.decisions)


# --- failure: a 409 at translate aborts there, later steps stay UNRUN ------


def test_a_409_at_translate_fails_that_step_with_verbatim_detail():
    client = _FakeClient(translate_fails_at=2)
    report = _run(_env(), client)

    assert report.verdict == "FAIL"
    assert report.failed_step == "translate"
    assert report.failure_detail == "migration unit is not approved"


def test_a_409_at_translate_leaves_every_later_step_explicitly_unrun():
    client = _FakeClient(translate_fails_at=2)
    report = _run(_env(), client)

    statuses = _step_statuses(report)
    passed_before_translate = STEP_ORDER[: STEP_ORDER.index("translate")]
    unrun_after_translate = STEP_ORDER[STEP_ORDER.index("translate") + 1 :]

    for name in passed_before_translate:
        assert statuses[name] == "PASSED", f"{name} should have PASSED before the failure"
    assert statuses["translate"] == "FAILED"
    for name in unrun_after_translate:
        assert statuses[name] == "UNRUN", f"{name} must be recorded UNRUN, not absent or faked"

    # Every STEP_ORDER name is present - "UNRUN" is a recorded status, the
    # step entries themselves are never simply missing from the list.
    assert [step.name for step in report.steps] == list(STEP_ORDER)
    assert len(report.steps) == len(STEP_ORDER)


def test_a_409_at_translate_still_records_every_decision_made_before_it():
    # Everything up through sign_off succeeded in this scenario - those
    # decisions were genuinely walked and must survive the later failure.
    client = _FakeClient(translate_fails_at=2)
    report = _run(_env(), client)

    assert report.decisions == [
        Decision(step="accept_catalog_defaults", choice="accept-defaults", detail=""),
        Decision(step="approve", choice="approve", detail="unit 1"),
        Decision(step="approve", choice="approve", detail="unit 2"),
        Decision(step="disposition", choice="rehost", detail="unit 1"),
        Decision(step="disposition", choice="rehost", detail="unit 2"),
        Decision(step="sign_off", choice="engineer", detail=""),
        Decision(step="sign_off", choice="program_mgmt", detail=""),
        Decision(step="sign_off", choice="delivery_md", detail=""),
        Decision(step="sign_off", choice="customer", detail=""),
    ]


def test_a_409_at_translate_leaves_no_artifact_and_no_translation_ids():
    client = _FakeClient(translate_fails_at=1)
    report = _run(_env(), client)

    assert report.artifact_paths == []
    assert report.translation_ids == []
    # list_units still ran (before the failure), so unit_ids is populated -
    # this is real progress the report should not hide either.
    assert report.unit_ids == [1, 2]


def test_a_409_at_translate_aborts_the_batch_the_second_unit_is_never_called():
    client = _FakeClient(translate_fails_at=1)
    _run(_env(), client)

    # unit 1 fails first (units are processed in list order) - unit 2's
    # translate_unit must never be attempted once the batch has aborted.
    assert "translate_unit:2" not in client.calls


def test_a_failure_at_a_non_boundary_unit_in_a_longer_roster_preserves_earlier_progress():
    # 5 units, failure at unit 3 (neither the first nor the last) - proves
    # the abort-on-first-failure behavior generalizes past a 2-unit roster
    # where "first" and "next-to-last" are indistinguishable.
    units = [{"id": i} for i in range(1, 6)]
    client = _FakeClient(units=units, approve_fails_at=3)
    report = _run(_env(), client)

    assert report.verdict == "FAIL"
    assert report.failed_step == "approve"
    assert report.failure_detail == "migration unit 3 is not reviewable"

    statuses = _step_statuses(report)
    for name in STEP_ORDER[: STEP_ORDER.index("approve")]:
        assert statuses[name] == "PASSED", f"{name} should have PASSED before the failure"
    assert statuses["approve"] == "FAILED"
    for name in STEP_ORDER[STEP_ORDER.index("approve") + 1 :]:
        assert statuses[name] == "UNRUN", f"{name} must be recorded UNRUN, not absent or faked"

    # Units 1-2 were genuinely approved before unit 3 failed; units 4-5
    # were never attempted once the batch aborted.
    assert "approve_unit:1" in client.calls
    assert "approve_unit:2" in client.calls
    assert "approve_unit:3" in client.calls
    assert "approve_unit:4" not in client.calls
    assert "approve_unit:5" not in client.calls

    # list_units itself succeeded in full - only the approve BATCH aborted
    # partway through - so all five ids are known to the report.
    assert report.unit_ids == [1, 2, 3, 4, 5]

    # Decisions carry accept_catalog_defaults's own (it precedes approve in
    # STEP_ORDER and genuinely ran) plus exactly the 1-2 approve successes -
    # no decision for unit 3 (it failed, nothing was decided) and none for
    # the never-attempted 4-5.
    assert report.decisions == [
        Decision(step="accept_catalog_defaults", choice="accept-defaults", detail=""),
        Decision(step="approve", choice="approve", detail="unit 1"),
        Decision(step="approve", choice="approve", detail="unit 2"),
    ]


# --- a translation timeout mid-batch preserves ids already resolved -------
# (the same honesty rule "decisions" already gets: partial progress made
# before a failure must survive that failure, never collapse to empty.)


def test_a_translation_timeout_preserves_the_id_already_resolved_before_it():
    client = _FakeClient(translation_never_appears_for=2)
    report = _run(_env(), client, translation_poll_max_attempts=2, translation_poll_interval=0)

    assert report.verdict == "FAIL"
    assert report.failed_step == "poll_translations"
    # Unit 1 resolved to translation id 101 BEFORE unit 2's poll exhausted
    # its budget - that resolved id must not be lost just because the batch
    # as a whole did not finish.
    assert report.translation_ids == [101]

    statuses = _step_statuses(report)
    for name in STEP_ORDER[: STEP_ORDER.index("poll_translations")]:
        assert statuses[name] == "PASSED", f"{name} should have PASSED before the failure"
    assert statuses["poll_translations"] == "FAILED"
    for name in STEP_ORDER[STEP_ORDER.index("poll_translations") + 1 :]:
        assert statuses[name] == "UNRUN", f"{name} must be recorded UNRUN, not absent or faked"

    # Unit 2 was polled exactly twice (the max_attempts budget given) before
    # the loop gave up - never zero, never unbounded.
    assert client.calls.count("list_translations:2") == 2


# --- a terminal pipeline-run status other than "succeeded" is a FAIL too --
# (PrizmanyxClient.poll_pipeline_run does not raise for this itself - the
# loop is the layer responsible for treating "failed" as a real failure.)


def test_a_failed_pipeline_run_status_fails_the_poll_step_even_without_an_exception():
    class _RunFailsClient(_FakeClient):
        def poll_pipeline_run(self, run_id):
            self.calls.append("poll_pipeline_run")
            return {"id": run_id, "status": "failed", "error_message": "extractor crashed"}

    client = _RunFailsClient()
    report = _run(_env(), client)

    assert report.verdict == "FAIL"
    assert report.failed_step == "poll_pipeline_run"
    assert report.failure_detail == "extractor crashed"
    assert _step_statuses(report)["accept_catalog_defaults"] == "UNRUN"


# --- deploy_apply + verify: the loop's own round trip against emulated Azure -


def test_happy_path_deploy_apply_and_verify_pass_with_all_matched_recorded():
    client = _FakeClient()
    materializer = _FakeMaterializer()
    report = _run(_env(), client, materialize=materializer)

    assert report.verdict == "PASS"
    statuses = _step_statuses(report)
    assert statuses["deploy_apply"] == "PASSED"
    assert statuses["verify"] == "PASSED"

    assert [d.translation_id for d in report.deployment_results] == [101, 102]
    assert all(d.ok for d in report.deployment_results)

    assert [v.translation_id for v in report.verification_results] == [101, 102]
    assert all(v.all_matched for v in report.verification_results)
    assert [v.matched_count for v in report.verification_results] == [2, 2]


def test_a_materialize_failure_fails_deploy_apply_and_leaves_verify_unrun():
    client = _FakeClient()
    materializer = _FakeMaterializer(fail_for=101)
    report = _run(_env(), client, materialize=materializer)

    assert report.verdict == "FAIL"
    assert report.failed_step == "deploy_apply"
    assert "101" in report.failure_detail

    statuses = _step_statuses(report)
    assert statuses["deploy_preview"] == "PASSED"
    assert statuses["deploy_apply"] == "FAILED"
    assert statuses["verify"] == "UNRUN"

    # The failing translation's own deployment result IS recorded - real,
    # already-happened progress, exactly like `decisions`/`unit_ids` survive
    # a partial batch elsewhere in this file.
    assert len(report.deployment_results) == 1
    assert report.deployment_results[0].translation_id == 101
    assert report.deployment_results[0].ok is False
    assert report.deployment_results[0].failures

    # verify never ran at all for either translation.
    assert report.verification_results == []
    assert "verify_translation:101" not in client.calls
    assert "verify_translation:102" not in client.calls


def test_a_missing_resource_at_verify_fails_that_step_with_the_counts():
    client = _FakeClient(
        verify_responses={
            102: {
                "verification_run_id": 42,
                "all_matched": False,
                "matched_count": 1,
                "mismatch_count": 0,
                "missing_count": 1,
                "extra_count": 0,
            }
        }
    )
    report = _run(_env(), client, materialize=_FakeMaterializer())

    assert report.verdict == "FAIL"
    assert report.failed_step == "verify"
    assert "missing=1" in report.failure_detail
    assert "102" in report.failure_detail

    # translation 101 verified clean and IS recorded before 102's failure -
    # partial progress, not collapsed to empty.
    assert [v.translation_id for v in report.verification_results] == [101, 102]
    assert report.verification_results[0].all_matched is True
    assert report.verification_results[1].all_matched is False
    assert report.verification_results[1].missing_count == 1


def test_a_502_from_verify_fails_the_step_with_the_bridge_detail_verbatim():
    client = _FakeClient(verify_502_for=101)
    report = _run(_env(), client, materialize=_FakeMaterializer())

    assert report.verdict == "FAIL"
    assert report.failed_step == "verify"
    assert report.failure_detail == "ARM endpoint http://floci-az:8080 unreachable"
    # Nothing for 101 was recorded (the call raised outright, honest) and
    # 102 was never attempted once the batch aborted.
    assert report.verification_results == []
    assert "verify_translation:102" not in client.calls


def test_the_resource_group_name_used_is_the_expected_resources_rg_row_not_a_foundry_name():
    # Carry #1 (mandatory, Task 3's review): deploy_apply and verify must
    # both use the ResourceGroup row's OWN azure_logical_name, never a
    # foundry-invented one like "foundry-<env>-<tid>" - a mismatch there
    # makes verify read every resource as missing while deploy_apply can
    # still report ok=True.
    client = _FakeClient(
        expected_resources={
            101: [
                {
                    "azure_resource_type": "azure_native.resources.ResourceGroup",
                    "azure_logical_name": "unit-42-prod-rg",
                    "azure_properties": {},
                },
                {
                    "azure_resource_type": "azure_native.storage.StorageAccount",
                    "azure_logical_name": "sa1",
                    "azure_properties": {},
                },
            ]
        },
        units=[{"id": 1}],
    )
    # `_FakeMaterializer`'s own `__call__` derives fail_for's id from the
    # "rg-<tid>" naming convention `_FakeClient`'s DEFAULT resources use -
    # this test deliberately uses a DIFFERENT resource group name, so a
    # plain call-capturing fake stands in instead.
    calls: list[tuple[str, str, str, list[dict[str, object]]]] = []

    def _materialize(az_base_url, subscription_id, resource_group, resources):
        calls.append((az_base_url, subscription_id, resource_group, resources))
        return ArmApplyResult(
            ok=True,
            applied=2,
            skipped_unmapped=[],
            failures=[],
            provisioning_timeouts=[],
            spawned_children=[],
        )

    report = _run(_env(), client, materialize=_materialize)

    assert report.verdict == "PASS"
    assert len(calls) == 1
    az_base_url, subscription_id, resource_group, _resources = calls[0]
    assert resource_group == "unit-42-prod-rg"
    assert az_base_url == "http://localhost:14577"
    assert subscription_id == "00000000-0000-0000-0000-000000000000"

    # verify_translation got asked to diff the EXACT same resource group -
    # never a value independently re-derived.
    assert client.verify_requests == [
        (101, "00000000-0000-0000-0000-000000000000", "unit-42-prod-rg")
    ]
    assert report.deployment_results[0].resource_group == "unit-42-prod-rg"
    assert report.verification_results[0].resource_group == "unit-42-prod-rg"


def test_spawned_children_from_deploy_apply_are_persisted_into_env_state():
    # Carry #2 (mandatory, Task 3's review): union, like the AWS side
    # (tf_runner.apply_in_dir), so engine.remove_env can reap them later.
    env = _env()
    client = _FakeClient()
    materializer = _FakeMaterializer(spawned_children=["floci-az-pg-demo"])

    _run(env, client, materialize=materializer)

    reloaded = state.load_state(env.name)
    assert reloaded.children == ["floci-az-pg-demo"]


def test_spawned_children_union_with_children_the_env_already_carried():
    env = _env()
    env = env.model_copy(update={"children": ["floci-rds-old-child"]})
    state.save_state(env)
    client = _FakeClient()
    materializer = _FakeMaterializer(spawned_children=["floci-az-pg-demo"])

    _run(env, client, materialize=materializer)

    reloaded = state.load_state(env.name)
    assert sorted(reloaded.children) == ["floci-az-pg-demo", "floci-rds-old-child"]


def test_a_crash_mid_materialize_still_persists_whatever_children_appeared(monkeypatch):
    # Carry #2's own worst case: a transport error propagates OUT of
    # `materialize` before it can return its own `spawned_children` at all -
    # loop.py's OWN before/after `docker ps` snapshot, taken around the
    # whole call, is what keeps this from orphaning the child silently.
    monkeypatch.setattr("foundry.loop.run_docker", _FakeDocker([set(), {"floci-az-pg-crashed"}]))
    env = _env()
    client = _FakeClient()

    def _crashes(az_base_url, subscription_id, resource_group, resources):  # noqa: ARG001
        raise ConnectionError("floci-az refused the connection")

    with pytest.raises(ConnectionError):
        _run(env, client, materialize=_crashes)

    reloaded = state.load_state(env.name)
    assert reloaded.children == ["floci-az-pg-crashed"]


def test_report_json_round_trips_deployment_and_verification_results():
    client = _FakeClient()
    report = _run(_env(), client, materialize=_FakeMaterializer())

    write_report(report, env_dir("demo"))
    reloaded = load_report(env_dir("demo"))

    assert reloaded == report
    assert reloaded.deployment_results == report.deployment_results
    assert reloaded.verification_results == report.verification_results


def test_write_report_renders_the_deployment_and_verification_sections():
    client = _FakeClient()
    report = _run(_env(), client, materialize=_FakeMaterializer())

    md_path, _json_path = write_report(report, env_dir("demo"))
    text = md_path.read_text()

    assert "## Deployments (deploy_apply)" in text
    assert "## Verification (verify)" in text
    assert "rg-101" in text
    assert "True" in text  # ok / all_matched rendered


# --- report.md / report.json ------------------------------------------------


def test_write_report_produces_a_markdown_file_with_the_fidelity_line_and_verdict():
    client = _FakeClient()
    report = _run(_env(), client)

    md_path, _json_path = write_report(report, env_dir("demo"))

    text = md_path.read_text()
    assert text.startswith("# Foundry loop report")
    assert FIDELITY_LINE in text
    assert "PASS" in text
    assert "translate" in text  # every step name appears in the steps table
    assert "rehost" in text  # every decision choice appears in the decisions table
    # The worker-reachable endpoint is distinct from the foundry's own
    # host-side verification endpoint - the report header must say so, not
    # just carry the bare URL with no context.
    assert "http://host.docker.internal:15600" in text
    assert "localhost" in text


def test_write_report_produces_a_markdown_file_naming_the_failed_step():
    client = _FakeClient(translate_fails_at=1)
    report = _run(_env(), client)

    md_path, _json_path = write_report(report, env_dir("demo"))

    text = md_path.read_text()
    assert "FAIL" in text
    assert "translate" in text
    assert "migration unit is not approved" in text


def test_report_json_round_trips_through_load_report():
    client = _FakeClient()
    report = _run(_env(), client)

    write_report(report, env_dir("demo"))
    reloaded = load_report(env_dir("demo"))

    assert reloaded == report


def test_write_report_creates_the_directory_if_it_does_not_exist_yet(tmp_path):
    client = _FakeClient()
    report = _run(_env(), client)

    target = tmp_path / "somewhere" / "nested"
    md_path, json_path = write_report(report, target)

    assert md_path.is_file()
    assert json_path.is_file()


def test_a_multiline_detail_is_neutralized_in_markdown_but_faithful_in_json(tmp_path):
    # A raw embedded newline inside a table cell splits that row across two
    # lines of the rendered file, corrupting every row below it - _escape_cell
    # must fold it into something that stays on one line. report.json has no
    # such structural constraint, so the raw newline must survive there.
    two_line_detail = "line one\nline two"
    report = LoopReport(
        env_name="demo",
        verdict="FAIL",
        failed_step="translate",
        failure_detail=two_line_detail,
        steps=[
            StepResult(
                name="translate", status="FAILED", duration_seconds=0.1, detail=two_line_detail
            )
        ],
        decisions=[Decision(step="approve", choice="approve", detail="unit 1\nunit 2")],
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        finished_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    text = render_markdown(report)

    assert "line one<br>line two" in text
    assert "unit 1<br>unit 2" in text
    # Exactly one rendered line per table row - a raw "\n" in the cell would
    # have produced two lines for "translate"'s row instead of one.
    assert len([line for line in text.splitlines() if line.startswith("| translate ")]) == 1
    assert len([line for line in text.splitlines() if line.startswith("| approve ")]) == 1

    write_report(report, tmp_path)
    reloaded = load_report(tmp_path)

    assert reloaded.failure_detail == two_line_detail
    assert reloaded.steps[0].detail == two_line_detail
    assert reloaded.decisions[0].detail == "unit 1\nunit 2"


def test_step_result_and_decision_are_plain_pydantic_models_with_expected_defaults():
    # Pins the {step, choice, why: "foundry default"} shape the plan states
    # verbatim, independent of any one run_loop scenario.
    decision = Decision(step="approve", choice="approve")
    assert decision.why == "foundry default"
    assert decision.detail == ""

    unrun = StepResult(name="translate", status="UNRUN")
    assert unrun.duration_seconds is None
    assert unrun.detail == ""
