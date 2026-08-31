"""Tests for foundry.prizmanyx_client: every wrapper method's HTTP shape,
the session-cookie carry, LoopStepError's error surfacing, the poll
helpers, and the log sink - all against `httpx.MockTransport` (the
idiomatic httpx testing seam - see the module's own docstring for why this
repo does not add a second, custom indirection on top of it). No network,
no running Prizmanyx.

Every path/body asserted below is cited at its own test with the exact
`packages/api/prizmanyx_api/...:line` it was read from in the Prizmanyx
repo, per the task's route inventory (itself read from that source, not
guessed from the plan).
"""

from __future__ import annotations

import json

import httpx
import pytest

from foundry.prizmanyx_client import LoopStepError, PrizmanyxClient

BASE_URL = "http://prizmanyx.test"


def _client(handler, *, user="admin", password="prizmanyx-dev", **kwargs) -> PrizmanyxClient:
    return PrizmanyxClient(
        BASE_URL, user, password, transport=httpx.MockTransport(handler), **kwargs
    )


def _login_ok(request: httpx.Request) -> httpx.Response:  # noqa: ARG001 - fake never inspects the request
    return httpx.Response(
        200,
        json={"id": 1, "username": "admin", "display_name": "Admin", "role": "admin"},
        headers={"set-cookie": "pzx_session=test-session-token; HttpOnly; Path=/; SameSite=lax"},
    )


# --- login + session cookie -------------------------------------------
# packages/api/prizmanyx_api/app.py:1390 (POST /api/v1/auth/login),
# packages/api/prizmanyx_api/auth.py:14 (SESSION_COOKIE = "pzx_session")


def test_login_posts_credentials_to_the_login_route():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return _login_ok(request)

    client = _client(handler, user="admin", password="prizmanyx-dev")
    result = client.login()

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/auth/login"
    assert captured["body"] == {"username": "admin", "password": "prizmanyx-dev"}
    assert result["username"] == "admin"


def test_session_cookie_from_login_is_sent_on_a_later_call():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/v1/auth/login":
            return _login_ok(request)
        return httpx.Response(200, json={"id": 5, "status": "running"})

    client = _client(handler)
    client.login()
    client.get_pipeline_run(5)

    # httpx.Client's own cookie jar carries it - no manual cookie plumbing
    # in this module at all.
    assert "pzx_session=test-session-token" in calls[1].headers.get("cookie", "")


def test_login_raises_missing_credentials_without_ever_calling_the_network(monkeypatch):
    monkeypatch.delenv("FOUNDRY_PRIZMANYX_USER", raising=False)
    monkeypatch.delenv("FOUNDRY_PRIZMANYX_PASSWORD", raising=False)

    def _must_not_be_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call should be made, got {request.url}")

    client = _client(_must_not_be_called, user=None, password=None)

    with pytest.raises(LoopStepError) as excinfo:
        client.login()

    assert excinfo.value.step == "login"
    assert excinfo.value.status == "missing_credentials"


def test_user_password_fall_back_to_env_vars_when_not_passed(monkeypatch):
    monkeypatch.setenv("FOUNDRY_PRIZMANYX_USER", "opuser")
    monkeypatch.setenv("FOUNDRY_PRIZMANYX_PASSWORD", "oppass")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _login_ok(request)

    client = _client(handler, user=None, password=None)
    client.login()

    assert captured["body"] == {"username": "opuser", "password": "oppass"}


def test_env_vars_are_read_once_at_construction_not_at_login_time(monkeypatch):
    monkeypatch.setenv("FOUNDRY_PRIZMANYX_USER", "original-user")
    monkeypatch.setenv("FOUNDRY_PRIZMANYX_PASSWORD", "original-pass")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _login_ok(request)

    client = _client(handler, user=None, password=None)
    # Changed AFTER construction - login() must still send the original
    # values captured at __init__ time, per the task's explicit contract
    # ("read at construction if not passed").
    monkeypatch.setenv("FOUNDRY_PRIZMANYX_USER", "changed-user")
    monkeypatch.setenv("FOUNDRY_PRIZMANYX_PASSWORD", "changed-pass")

    client.login()

    assert captured["body"] == {"username": "original-user", "password": "original-pass"}


def test_context_manager_logs_in_on_enter_and_closes_on_exit():
    login_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_calls
        login_calls += 1
        return _login_ok(request)

    client = _client(handler)
    with client as entered:
        assert entered is client
        assert login_calls == 1
    assert client._client.is_closed


def test_context_manager_closes_the_client_when_login_fails_on_enter(monkeypatch):
    monkeypatch.delenv("FOUNDRY_PRIZMANYX_USER", raising=False)
    monkeypatch.delenv("FOUNDRY_PRIZMANYX_PASSWORD", raising=False)

    def _must_not_be_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP call should be made, got {request.url}")

    client = _client(_must_not_be_called, user=None, password=None)

    with pytest.raises(LoopStepError):
        client.__enter__()

    assert client._client.is_closed


# --- LoopStepError: verbatim detail, fallback, and the 2-arg timeout shape --


def test_a_422_surfaces_the_json_bodys_detail_verbatim():
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(422, json={"detail": "disposition must be one of (...)"})

    client = _client(handler)

    with pytest.raises(LoopStepError) as excinfo:
        client.decide_disposition(1, "not-a-real-disposition")  # type: ignore[arg-type]

    assert excinfo.value.step == "decide_disposition"
    assert excinfo.value.status == 422
    assert excinfo.value.detail_verbatim == "disposition must be one of (...)"


def test_a_dict_shaped_detail_is_serialized_not_dropped():
    # app.py's pipeline-run-creation failure path raises a *dict* detail
    # (`{"id": row["id"], "error_message": str(exc)}`) rather than a plain
    # string - LoopStepError.detail_verbatim is typed str, so this must be
    # serialized, not silently discarded.
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(502, json={"detail": {"id": 9, "error_message": "boom"}})

    client = _client(handler)

    with pytest.raises(LoopStepError) as excinfo:
        client.start_extraction(1, ["000000000000"], ["us-east-1"])

    assert excinfo.value.detail_verbatim == json.dumps({"id": 9, "error_message": "boom"})


def test_detail_falls_back_to_raw_text_when_the_body_is_not_json():
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(
            500, text="internal server error", headers={"content-type": "text/plain"}
        )

    client = _client(handler)

    with pytest.raises(LoopStepError) as excinfo:
        client.get_pipeline_run(1)

    assert excinfo.value.detail_verbatim == "internal server error"


def test_loop_step_error_two_argument_form_defaults_detail_to_empty_string():
    err = LoopStepError("some_step", "timeout")

    assert err.step == "some_step"
    assert err.status == "timeout"
    assert err.detail_verbatim == ""


# --- each wrapper hits the expected path --------------------------------


def test_create_program_posts_expected_path_and_body():
    # packages/api/prizmanyx_api/app.py:1742 (POST /api/v1/programs)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 7, "name": "estate-1"})

    client = _client(handler)
    result = client.create_program("estate-1")

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/programs"
    assert captured["body"] == {
        "name": "estate-1",
        "description": None,
        "direction": "aws_to_azure",
    }
    assert result["id"] == 7


def test_register_env_connection_posts_expected_path_and_body():
    # packages/api/prizmanyx_api/app.py:2568
    # (POST /api/v1/programs/{program_id}/aws/localstack-connections)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 1, "status": "connected"})

    client = _client(handler)
    result = client.register_env_connection(7, "http://localhost:15600")

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/programs/7/aws/localstack-connections"
    assert captured["body"] == {
        "endpoint_url": "http://localhost:15600",
        "region": "us-east-1",
        "account_id": "000000000000",
        "label": None,
    }
    assert result["status"] == "connected"


def test_start_extraction_posts_expected_path_and_body():
    # packages/api/prizmanyx_api/app.py:3408 (POST /api/v1/pipeline-runs)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 42, "status": "pending"})

    client = _client(handler)
    result = client.start_extraction(7, ["000000000000"], ["us-east-1"])

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/pipeline-runs"
    assert captured["body"] == {
        "program_id": 7,
        "account_ids": ["000000000000"],
        "regions": ["us-east-1"],
        "start_temporal": True,
    }
    assert result["id"] == 42


def test_start_extraction_passes_extra_kwargs_through_to_the_body():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 42})

    client = _client(handler)
    client.start_extraction(
        7, ["000000000000"], ["us-east-1"], azure_resource_group="foundry-preview-rg"
    )

    assert captured["body"]["azure_resource_group"] == "foundry-preview-rg"


def test_get_pipeline_run_gets_expected_path():
    # packages/api/prizmanyx_api/app.py:3516 (GET /api/v1/pipeline-runs/{id})
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={"id": 42, "status": "succeeded", "graph_run_id": 3})

    client = _client(handler)
    result = client.get_pipeline_run(42)

    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/pipeline-runs/42"
    assert result["graph_run_id"] == 3


def test_accept_catalog_defaults_posts_expected_path_with_no_body():
    # packages/api/prizmanyx_api/app.py:2907-2908
    # (POST /api/v1/programs/{program_id}/destination/catalog/review)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["content"] = request.content
        return httpx.Response(200, json={"reviewed_at": "2026-08-31T00:00:00Z", "entries": []})

    client = _client(handler)
    result = client.accept_catalog_defaults(7)

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/programs/7/destination/catalog/review"
    assert captured["content"] == b""
    assert result["reviewed_at"] is not None


def test_list_units_gets_expected_path_with_graph_run_id_query():
    # packages/api/prizmanyx_api/app.py:4638 (GET /api/v1/migration-units)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json=[
                {"id": 1, "review_status": "pending_review", "disposition": None, "slug": "unit-1"}
            ],
        )

    client = _client(handler)
    result = client.list_units(3)

    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/migration-units"
    assert captured["params"] == {"graph_run_id": "3"}
    assert result == [
        {"id": 1, "review_status": "pending_review", "disposition": None, "slug": "unit-1"}
    ]


def test_approve_unit_posts_expected_path():
    # packages/api/prizmanyx_api/app.py:5727
    # (POST /api/v1/migration-units/{id}/approve)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={"id": 1, "review_status": "approved"})

    client = _client(handler)
    result = client.approve_unit(1)

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/migration-units/1/approve"
    assert result["review_status"] == "approved"


def test_decide_disposition_puts_expected_path_and_body_with_rehost_default():
    # packages/api/prizmanyx_api/app.py:6047
    # (PUT /api/v1/migration-units/{id}/disposition)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 1, "disposition": "rehost"})

    client = _client(handler)
    result = client.decide_disposition(1)

    assert captured["method"] == "PUT"
    assert captured["path"] == "/api/v1/migration-units/1/disposition"
    assert captured["body"] == {"disposition": "rehost"}
    assert result["disposition"] == "rehost"


def test_decide_disposition_includes_rationale_when_given():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 1, "disposition": "retire"})

    client = _client(handler)
    client.decide_disposition(1, "retire", rationale="no live usage in 90 days")

    assert captured["body"] == {"disposition": "retire", "rationale": "no live usage in 90 days"}


def test_sign_off_posts_expected_path_and_body():
    # packages/api/prizmanyx_api/app.py:9226 (POST /api/v1/programs/{id}/sign-offs)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"gate": {"complete": False}})

    client = _client(handler)
    result = client.sign_off(7, "engineer")

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/programs/7/sign-offs"
    assert captured["body"] == {"role": "engineer", "deliverable": "client_report"}
    assert "gate" in result


def test_get_exit_gate_gets_expected_path():
    # packages/api/prizmanyx_api/app.py:9762 (GET /api/v1/discovery/{id}/exit-gate)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={"passed": True, "conditions": {}})

    client = _client(handler)
    result = client.get_exit_gate(7)

    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/discovery/7/exit-gate"
    assert result["passed"] is True


def test_translate_unit_posts_expected_path():
    # packages/api/prizmanyx_api/app.py:5778
    # (POST /api/v1/migration-units/{id}/translate)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={"workflow_id": "wf-1", "migration_unit_id": 1})

    client = _client(handler)
    result = client.translate_unit(1)

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/migration-units/1/translate"
    assert result["workflow_id"] == "wf-1"


def test_list_translations_gets_expected_path_with_query():
    # packages/api/prizmanyx_api/app.py:6074 (GET /api/v1/translations)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=[{"id": 99, "migration_unit_id": 1}])

    client = _client(handler)
    result = client.list_translations(1)

    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/translations"
    assert captured["params"] == {"migration_unit_id": "1"}
    assert result[0]["id"] == 99


def test_export_program_gets_expected_path_and_returns_the_raw_response():
    # packages/api/prizmanyx_api/app.py:9062 (GET /api/v1/programs/{id}/program-export)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            text="section,value\nprogram,estate-1\n",
            headers={
                "content-type": "text/csv; charset=utf-8",
                "content-disposition": 'attachment; filename="program-7.csv"',
            },
        )

    client = _client(handler)
    response = client.export_program(7)

    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/programs/7/program-export"
    assert captured["params"] == {"format": "csv"}
    assert isinstance(response, httpx.Response)
    assert response.text == "section,value\nprogram,estate-1\n"
    assert "program-7.csv" in response.headers["content-disposition"]


def test_deploy_preview_posts_expected_path_with_placeholder_azure_fields():
    # packages/api/prizmanyx_api/deployments_routes.py:176
    # (POST /api/v1/deployments/trigger)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"workflow_id": "wf-2", "stack_name": "unit-1-staging", "mode": "preview"}
        )

    client = _client(handler)
    result = client.deploy_preview(99)

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/deployments/trigger"
    assert captured["body"] == {
        "translation_id": 99,
        "mode": "preview",
        "azure_subscription_id": "00000000-0000-0000-0000-000000000000",
        "azure_resource_group": "foundry-preview-rg",
    }
    assert result["mode"] == "preview"


# --- poll helpers: return on success, raise LoopStepError on timeout ------


def test_poll_pipeline_run_returns_the_final_row_once_a_terminal_status_is_reached():
    statuses = iter(["pending", "running", "succeeded"])

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        status = next(statuses)
        body = {"id": 1, "status": status}
        if status == "succeeded":
            body["graph_run_id"] = 55
        return httpx.Response(200, json=body)

    client = _client(handler)
    result = client.poll_pipeline_run(1, max_attempts=5, interval=0)

    assert result["status"] == "succeeded"
    assert result["graph_run_id"] == 55


def test_poll_pipeline_run_raises_loop_step_error_timeout_after_max_attempts():
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"id": 1, "status": "running"})

    client = _client(handler)

    with pytest.raises(LoopStepError) as excinfo:
        client.poll_pipeline_run(1, max_attempts=3, interval=0)

    assert excinfo.value.step == "poll_pipeline_run"
    assert excinfo.value.status == "timeout"
    assert call_count == 3


def test_poll_pipeline_run_does_not_raise_on_a_failed_terminal_status():
    # A "failed" run is a real outcome for the loop to interpret, not a
    # client-level error - only exhausting max_attempts without reaching
    # ANY terminal status raises.
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json={"id": 1, "status": "failed", "error_message": "boom"})

    client = _client(handler)
    result = client.poll_pipeline_run(1, max_attempts=3, interval=0)

    assert result["status"] == "failed"


def test_poll_exit_gate_returns_once_passed_is_true():
    results = iter([{"passed": False}, {"passed": False}, {"passed": True, "conditions": {}}])

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json=next(results))

    client = _client(handler)
    result = client.poll_exit_gate(7, max_attempts=5, interval=0)

    assert result["passed"] is True


def test_poll_exit_gate_raises_loop_step_error_timeout_after_max_attempts():
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json={"passed": False, "conditions": {}})

    client = _client(handler)

    with pytest.raises(LoopStepError) as excinfo:
        client.poll_exit_gate(7, max_attempts=2, interval=0)

    assert excinfo.value.step == "poll_exit_gate"
    assert excinfo.value.status == "timeout"


# --- log sink: list or logger-like object ---------------------------------


def test_log_sink_list_records_step_method_path_status_in_order():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return _login_ok(request)
        return httpx.Response(200, json={"id": 1, "status": "running"})

    log: list[dict[str, object]] = []
    client = _client(handler, log=log)
    client.login()
    client.get_pipeline_run(1)

    assert log == [
        {"step": "login", "method": "POST", "path": "/api/v1/auth/login", "status": 200},
        {
            "step": "get_pipeline_run",
            "method": "GET",
            "path": "/api/v1/pipeline-runs/1",
            "status": 200,
        },
    ]


def test_log_sink_logger_like_object_receives_an_info_call():
    class _RecordingLogger:
        def __init__(self) -> None:
            self.messages: list[tuple[object, ...]] = []

        def info(self, msg: str, *args: object) -> None:
            self.messages.append((msg, *args))

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json={"id": 1, "status": "running"})

    logger = _RecordingLogger()
    client = _client(handler, log=logger)
    client.get_pipeline_run(1)

    assert len(logger.messages) == 1
    assert logger.messages[0][1:] == ("get_pipeline_run", "GET", "/api/v1/pipeline-runs/1", 200)


def test_log_sink_records_the_status_of_a_failed_call_too():
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(404, json={"detail": "not found"})

    log: list[dict[str, object]] = []
    client = _client(handler, log=log)

    with pytest.raises(LoopStepError):
        client.get_pipeline_run(999)

    assert log == [
        {
            "step": "get_pipeline_run",
            "method": "GET",
            "path": "/api/v1/pipeline-runs/999",
            "status": 404,
        }
    ]
