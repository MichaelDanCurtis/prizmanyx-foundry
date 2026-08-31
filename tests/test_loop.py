"""Tests for foundry.loop and foundry.report: the pipeline walk, its
PASS/FAIL verdict, the decisions honesty table, and the report.md/report.json
files it produces - all against a hand-written FAKED `PrizmanyxClient` (see
`foundry.loop.PipelineClient`, the Protocol this fake satisfies structurally).
No network, no httpx transport, no running Prizmanyx anywhere in this file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from foundry import state
from foundry.loop import (
    STEP_ORDER,
    Decision,
    LoopReport,
    StepResult,
    run_loop,
)
from foundry.prizmanyx_client import LoopStepError
from foundry.report import FIDELITY_LINE, load_report, write_report
from foundry.state import EnvState, env_dir


@pytest.fixture(autouse=True)
def _isolated_envs_dir(tmp_path, monkeypatch):
    # Same isolation as tests/test_state.py - run_loop's own export step
    # writes into the env's real directory via state.ensure_env_dir, so this
    # must never touch the real ~/.prizmanyx-foundry on the machine running
    # the suite.
    monkeypatch.setattr(state, "ENVS_DIR", tmp_path / "envs")


def _env(name: str = "demo", port: int = 15600) -> EnvState:
    return EnvState(
        name=name,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ports={"aws": port},
        spec_path=Path(f"/envs/{name}/spec.yaml"),
        status="running",
    )


class _FakeClient:
    """A hand-written double satisfying `foundry.loop.PipelineClient`
    structurally - every method the loop calls, none of the polling/HTTP
    machinery `PrizmanyxClient` itself needs.

    `translate_fails_at`, when set, makes `translate_unit` raise a
    `LoopStepError` for that one migration unit id (and only that one) -
    the fixture behind the "409 at translate" test.
    """

    def __init__(
        self,
        *,
        units: list[dict[str, object]] | None = None,
        translate_fails_at: int | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._units = units if units is not None else [{"id": 1}, {"id": 2}]
        self._translate_fails_at = translate_fails_at
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
        return [{"id": 100 + unit_id, "migration_unit_id": unit_id}]

    def export_program(self, program_id, fmt="csv"):
        self.calls.append("export_program")
        self.exported_program_id = program_id
        self.exported_format = fmt
        return httpx.Response(200, content=b"section,value\nprogram,7\n")

    def deploy_preview(self, translation_id):
        self.calls.append(f"deploy_preview:{translation_id}")
        return {"workflow_id": "wf-deploy", "mode": "preview", "translation_id": translation_id}


def _step_statuses(report: LoopReport) -> dict[str, str]:
    return {step.name: step.status for step in report.steps}


# --- happy path: every step PASSED, every decision recorded ----------------


def test_happy_path_is_pass_with_every_step_recorded():
    client = _FakeClient()
    report = run_loop(_env(), client)

    assert report.verdict == "PASS"
    assert report.failed_step is None
    assert report.failure_detail is None
    assert [step.name for step in report.steps] == list(STEP_ORDER)
    assert all(step.status == "PASSED" for step in report.steps)
    assert all(step.duration_seconds is not None for step in report.steps)
    assert all(step.duration_seconds >= 0 for step in report.steps)


def test_happy_path_captures_program_graph_run_units_and_translation_ids():
    client = _FakeClient()
    report = run_loop(_env(), client)

    assert report.program_id == 7
    assert report.graph_run_id == 55
    assert report.unit_ids == [1, 2]
    assert report.translation_ids == [101, 102]


def test_happy_path_registers_the_localhost_endpoint_for_the_envs_own_port():
    client = _FakeClient()
    run_loop(_env(port=15900), client)

    assert client.registered_endpoint_url == "http://localhost:15900"


def test_happy_path_writes_the_export_pack_into_the_envs_directory():
    client = _FakeClient()
    report = run_loop(_env(), client)

    assert len(report.artifact_paths) == 1
    exported = Path(report.artifact_paths[0])
    assert exported == env_dir("demo") / "export.csv"
    assert exported.read_bytes() == b"section,value\nprogram,7\n"


def test_happy_path_calls_every_client_method_in_pipeline_order():
    client = _FakeClient()
    run_loop(_env(), client)

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
    ]


# --- decisions: every auto-walked gate, verbatim ----------------------------


def test_decisions_table_carries_every_auto_gate_verbatim():
    client = _FakeClient()
    report = run_loop(_env(), client)

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
    report = run_loop(_env(), client)

    assert report.verdict == "FAIL"
    assert report.failed_step == "translate"
    assert report.failure_detail == "migration unit is not approved"


def test_a_409_at_translate_leaves_every_later_step_explicitly_unrun():
    client = _FakeClient(translate_fails_at=2)
    report = run_loop(_env(), client)

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
    report = run_loop(_env(), client)

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
    report = run_loop(_env(), client)

    assert report.artifact_paths == []
    assert report.translation_ids == []
    # list_units still ran (before the failure), so unit_ids is populated -
    # this is real progress the report should not hide either.
    assert report.unit_ids == [1, 2]


def test_a_409_at_translate_aborts_the_batch_the_second_unit_is_never_called():
    client = _FakeClient(translate_fails_at=1)
    run_loop(_env(), client)

    # unit 1 fails first (units are processed in list order) - unit 2's
    # translate_unit must never be attempted once the batch has aborted.
    assert "translate_unit:2" not in client.calls


# --- a terminal pipeline-run status other than "succeeded" is a FAIL too --
# (PrizmanyxClient.poll_pipeline_run does not raise for this itself - the
# loop is the layer responsible for treating "failed" as a real failure.)


def test_a_failed_pipeline_run_status_fails_the_poll_step_even_without_an_exception():
    class _RunFailsClient(_FakeClient):
        def poll_pipeline_run(self, run_id):
            self.calls.append("poll_pipeline_run")
            return {"id": run_id, "status": "failed", "error_message": "extractor crashed"}

    client = _RunFailsClient()
    report = run_loop(_env(), client)

    assert report.verdict == "FAIL"
    assert report.failed_step == "poll_pipeline_run"
    assert report.failure_detail == "extractor crashed"
    assert _step_statuses(report)["accept_catalog_defaults"] == "UNRUN"


# --- report.md / report.json ------------------------------------------------


def test_write_report_produces_a_markdown_file_with_the_fidelity_line_and_verdict():
    client = _FakeClient()
    report = run_loop(_env(), client)

    md_path, _json_path = write_report(report, env_dir("demo"))

    text = md_path.read_text()
    assert text.startswith("# Foundry loop report")
    assert FIDELITY_LINE in text
    assert "PASS" in text
    assert "translate" in text  # every step name appears in the steps table
    assert "rehost" in text  # every decision choice appears in the decisions table


def test_write_report_produces_a_markdown_file_naming_the_failed_step():
    client = _FakeClient(translate_fails_at=1)
    report = run_loop(_env(), client)

    md_path, _json_path = write_report(report, env_dir("demo"))

    text = md_path.read_text()
    assert "FAIL" in text
    assert "translate" in text
    assert "migration unit is not approved" in text


def test_report_json_round_trips_through_load_report():
    client = _FakeClient()
    report = run_loop(_env(), client)

    write_report(report, env_dir("demo"))
    reloaded = load_report(env_dir("demo"))

    assert reloaded == report


def test_write_report_creates_the_directory_if_it_does_not_exist_yet(tmp_path):
    client = _FakeClient()
    report = run_loop(_env(), client)

    target = tmp_path / "somewhere" / "nested"
    md_path, json_path = write_report(report, target)

    assert md_path.is_file()
    assert json_path.is_file()


def test_step_result_and_decision_are_plain_pydantic_models_with_expected_defaults():
    # Pins the {step, choice, why: "foundry default"} shape the plan states
    # verbatim, independent of any one run_loop scenario.
    decision = Decision(step="approve", choice="approve")
    assert decision.why == "foundry default"
    assert decision.detail == ""

    unrun = StepResult(name="translate", status="UNRUN")
    assert unrun.duration_seconds is None
    assert unrun.detail == ""
