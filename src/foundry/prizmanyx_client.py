"""The Prizmanyx pipeline client: an operator, not a delegate.

`PrizmanyxClient` drives a *running* Prizmanyx exactly the way a human
operator would through the browser - it signs in with a real username and
password, gets back the same httponly session cookie the UI gets, and calls
the same documented `/api/v1/...` routes the UI calls. It holds no special
access: everything here is a route any signed-in operator can already reach.
Task 7's loop is the only caller; this module owns zero looping policy of
its own (which disposition to pick, how many retries make sense for a given
step) - it is a thin, typed, honestly-erroring transport, nothing more.

The route inventory below was read from Prizmanyx source, not guessed from
the plan (the plan's claim that the e2e suite walks the pipeline was WRONG -
`tests/e2e/test_full_pipeline.py` only checks fixture/registry preconditions
and starts no run at all). Every path is cited at its call site with the
file:line it was read from, in `../Prizmanyx` next to this repo.

Auth mechanism (read from `packages/api/prizmanyx_api/app.py:1390-1430` and
`apps/web/e2e-ui/smoke.spec.ts`): a session is a cookie, not a bearer token.
`POST /api/v1/auth/login {username, password}` sets `pzx_session` (httponly,
samesite=lax, path=/ - `packages/api/prizmanyx_api/auth.py:129-139`).
`httpx.Client` keeps its own cookie jar across requests made through the
same instance, so logging in once and reusing this client is *all* that is
needed to carry the session - there is no manual cookie plumbing here, and
none should ever be added.

Two deliberate departures from "thin wrapper returning parsed JSON":

- `export_program` returns the raw `httpx.Response`, not `.json()` - the
  program-export route (`app.py:9062`) answers CSV or HTML, never JSON.
- The engagement-scoped export route needs team + engagement setup this
  client never creates. `export_program` calls the program-scoped route
  instead (`GET /api/v1/programs/{program_id}/program-export`) - the
  pragmatic path for a client that only ever holds a `program_id`.

Every method is a single call through `_request`, the one seam between this
class and the network - mirrors this repo's existing single-seam habit
(`engine.run_docker`, `from_repo.run_git`, `from_repo._clone_repo`). Tests
inject `httpx.MockTransport` at construction (`transport=`) rather than
monkeypatching a function, because that IS the idiomatic httpx seam - no
custom indirection needed on top of it. `_request` does three things and
nothing else: make the call, log `(step, method, path, status)` to whatever
sink the caller supplied, and raise `LoopStepError` on a non-2xx response
with the response's own "detail" field carried over verbatim.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, Protocol

import httpx

#: Read at construction time only, and only when the caller did not pass an
#: explicit user/password - never written to disk, never logged, never
#: attached to any request other than the one login POST body.
_USER_ENV_VAR = "FOUNDRY_PRIZMANYX_USER"
_PASSWORD_ENV_VAR = "FOUNDRY_PRIZMANYX_PASSWORD"

_DEFAULT_TIMEOUT_SECONDS = 30.0

# Pipeline-run polling: sized off this project's own scale assumption
# ("single-account snapshot: <= 5 minutes wall clock") rather than picked
# arbitrarily - 60 attempts * 5s covers one full snapshot's worst case with
# room to spare.
_DEFAULT_RUN_POLL_MAX_ATTEMPTS = 60
_DEFAULT_RUN_POLL_INTERVAL_SECONDS = 5.0

# Exit-gate polling only ever waits on synchronous DB writes this same
# client just made (a disposition PUT, a sign-off POST) becoming visible to
# a subsequent read - never a long-running workflow - so a much shorter
# budget than the pipeline-run poll is intentional, not an oversight.
_DEFAULT_GATE_POLL_MAX_ATTEMPTS = 10
_DEFAULT_GATE_POLL_INTERVAL_SECONDS = 1.0

# PipelineRunStatus, verbatim from packages/api/prizmanyx_api/pipeline_runs.py:7
# (single "l" in "canceled" - American spelling is the server's own).
_PIPELINE_RUN_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled"})

# deployments_routes.py:176's trigger_deployment reads these from the body
# when unset in the environment and 422s without either - the loop wants
# preview to work with no real Azure subscription at hand, so this client
# always sends these placeholders rather than relying on server-side env
# vars that a bare `foundry` checkout will not have set.
_PREVIEW_PLACEHOLDER_SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000000"
_PREVIEW_PLACEHOLDER_RESOURCE_GROUP = "foundry-preview-rg"

# packages/api/prizmanyx_api/auth.py:14 - SESSION_COOKIE = "pzx_session".
# Duplicated here (not imported) because this client has no dependency on
# the Prizmanyx package itself - it is an external operator, deliberately
# decoupled from the product's own code.
_SESSION_COOKIE_NAME = "pzx_session"

# Browsers treat "localhost", 127.0.0.1, and ::1 as a "potentially
# trustworthy origin" (the Secure Contexts spec's loopback exception) and
# will send a Secure-flagged cookie back to one of these hosts even over
# plain http - it is the ONLY reason Prizmanyx's own UI works at all against
# a plaintext dev backend on http://localhost. httpx has no such exception:
# it is a faithful RFC 6265 client, and RFC 6265 alone says a Secure cookie
# is never sent over http, loopback host or not (`http.cookiejar`'s
# `return_ok_secure`, called on every real request, checks only the
# request's scheme). Confirmed against a live dev backend (Task 8
# acceptance): `login()` got 200 + `Set-Cookie: ...; Secure`, the cookie was
# stored, and the very next call got 401 with no Cookie header at all -
# httpx's jar had silently withheld it. `_is_loopback_http` below is this
# client's own version of the browser's exception, applied ONLY where the
# browser would apply it - see its own docstring for why both conditions
# (scheme AND host) are required, not just one.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_loopback_http(url: httpx.URL) -> bool:
    """True if `url` is plain http to a loopback host - the one case where
    this client must replicate the browser's Secure-cookie loopback
    exception, because httpx otherwise won't.

    Both conditions are required, mirroring the browser rule exactly: an
    https base gets ordinary Secure-cookie handling regardless of host
    (Secure's own requirement is already satisfied by TLS), and a
    non-loopback http base gets ordinary handling too - there is no
    exception for it, in a browser or here, and inventing one would be
    exactly the "globally disable cookie security" this must not do.
    """
    return url.scheme == "http" and url.host in _LOOPBACK_HOSTS


#: The complete disposition vocabulary (app.py:6047's PUT .../disposition,
#: enforced server-side by disposition_store.set_disposition - a ValueError
#: there becomes this route's 422). Typed here for editor/type-checker help
#: only; the server remains the single source of truth and this client does
#: not re-validate before sending.
Disposition = Literal[
    "rehost", "replatform", "refactor", "repurchase", "rebuild", "retain", "retire"
]

JSONObject = dict[str, Any]
JSONList = list[dict[str, Any]]


class _LoggerLike(Protocol):
    def info(self, msg: str, *args: object) -> None: ...


class LoopStepError(Exception):
    """Raised by every `PrizmanyxClient` method that fails to complete its step.

    `status` is the HTTP status code (`int`) for a non-2xx response, or a
    short machine-readable marker string for a failure with no HTTP response
    at all: `"timeout"` when a poll helper (`poll_pipeline_run`,
    `poll_exit_gate`) exhausts its attempt budget without the condition it
    was polling for becoming true, or `"missing_credentials"` when `login()`
    runs with no username/password available from either the constructor or
    the `FOUNDRY_PRIZMANYX_USER`/`FOUNDRY_PRIZMANYX_PASSWORD` environment.

    `detail_verbatim` is the JSON response body's own `"detail"` field taken
    exactly as the server wrote it (FastAPI's `HTTPException` shape) - never
    reworded or summarized - falling back to the raw response text when the
    body is not JSON or carries no `"detail"` key. The two no-HTTP-response
    cases above have nothing to quote verbatim, so this carries a short
    human-readable description instead; the parameter defaults to `""` so
    the two-argument call `LoopStepError(step, "timeout")` is valid on its
    own without forcing every caller to supply a third argument.
    """

    def __init__(self, step: str, status: int | str, detail_verbatim: str = "") -> None:
        self.step = step
        self.status = status
        self.detail_verbatim = detail_verbatim
        super().__init__(f"{step} failed: status={status!r} detail={detail_verbatim!r}")


def _detail_from_response(response: httpx.Response) -> str:
    """The response's JSON `"detail"` field, verbatim, else the raw text.

    A `str` detail (the overwhelming common case - every `HTTPException` in
    app.py raises one) is returned exactly as-is. The one route that raises
    a *dict* detail (`app.py`'s pipeline-run-creation failure path) is
    serialized rather than dropped, since `LoopStepError.detail_verbatim` is
    typed `str` - "verbatim" here means full fidelity, not literally
    whatever Python type the field happened to hold.
    """
    try:
        body = response.json()
    except ValueError:
        return response.text
    if isinstance(body, dict) and "detail" in body:
        detail = body["detail"]
        return detail if isinstance(detail, str) else json.dumps(detail)
    return response.text


class PrizmanyxClient:
    """Signs in to a running Prizmanyx and drives its migration pipeline.

    Construct with `base_url` and, optionally, `user`/`password` (else read
    once from `FOUNDRY_PRIZMANYX_USER`/`FOUNDRY_PRIZMANYX_PASSWORD` - see the
    module docstring). Call `login()` explicitly, or use the instance as a
    context manager (`with PrizmanyxClient(...) as client:`), which logs in
    on `__enter__` and always closes the underlying `httpx.Client` on exit -
    including when `login()` itself raises, so a failed sign-in never leaks
    the connection pool.

    `log`, when given, receives a `(step, method, path, status)` record for
    every HTTP call this client makes - the run log Task 7's report is built
    from. Anything with an `.append` method (a plain `list` is the common
    case) gets a dict appended; anything with an `.info` method (a
    `logging.Logger`, or any duck-typed equivalent) gets a formatted line.
    `None` (the default) means "don't log".
    """

    def __init__(
        self,
        base_url: str,
        user: str | None = None,
        password: str | None = None,
        *,
        log: list[JSONObject] | _LoggerLike | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._user = user if user is not None else os.environ.get(_USER_ENV_VAR)
        self._password = password if password is not None else os.environ.get(_PASSWORD_ENV_VAR)
        self._log_sink = log
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)
        self._loopback_http = _is_loopback_http(self._client.base_url)
        # Set True by `_carry_loopback_session_cookie` the moment `login()`
        # actually finds and de-secures the session cookie on a
        # loopback-http base. Stays False when this base needs no
        # workaround at all, or if login has not happened yet.
        self._loopback_cookie_carried = False

    def __enter__(self) -> PrizmanyxClient:
        try:
            self.login()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # The one seam: every route call goes through here.
    # ------------------------------------------------------------------

    def _log(self, step: str, method: str, path: str, status: int | str) -> None:
        sink = self._log_sink
        if sink is None:
            return
        if hasattr(sink, "append"):
            sink.append({"step": step, "method": method, "path": path, "status": status})
        elif hasattr(sink, "info"):
            sink.info("%s %s %s -> %s", step, method, path, status)

    def _request(
        self,
        step: str,
        method: str,
        path: str,
        *,
        json_body: object | None = None,
        params: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        response = self._client.request(method, path, json=json_body, params=params)
        self._log(step, method, path, response.status_code)
        if response.is_success:
            return response
        raise LoopStepError(step, response.status_code, _detail_from_response(response))

    def _poll(
        self,
        step: str,
        fetch: Callable[[], JSONObject],
        is_done: Callable[[JSONObject], bool],
        *,
        max_attempts: int,
        interval: float,
    ) -> JSONObject:
        """Call `fetch()` up to `max_attempts` times, sleeping `interval`
        seconds between attempts, until `is_done` accepts the result.

        A real HTTP error from `fetch()` propagates immediately - a poll
        waits on a *condition*, it does not paper over a genuine failure by
        retrying it silently (anti-pattern 9's rule, applied here). Only
        exhausting the attempt budget without `is_done` ever returning True
        raises, and it raises `LoopStepError(step, "timeout", ...)`.
        """
        result: JSONObject = {}
        for attempt in range(1, max_attempts + 1):
            result = fetch()
            if is_done(result):
                return result
            if attempt < max_attempts:
                time.sleep(interval)
        raise LoopStepError(
            step,
            "timeout",
            f"gave up after {max_attempts} attempts at {interval}s intervals "
            "without satisfying the poll condition",
        )

    # ------------------------------------------------------------------
    # Auth — packages/api/prizmanyx_api/app.py:1390 (POST /api/v1/auth/login)
    # ------------------------------------------------------------------

    def login(self) -> JSONObject:
        """Sign in and let `httpx.Client`'s own cookie jar carry the session
        cookie on every subsequent call made through this instance.

        On a loopback-http base (see `_is_loopback_http`), the jar alone is
        not enough - the real session cookie is `Secure`, and httpx's jar
        will never volunteer a `Secure` cookie over plain http, browser
        loopback exception or not. `_carry_loopback_session_cookie` fixes
        this once, right here, so every later call needs no special-casing
        at all - it just goes through the jar exactly like any other call.
        """
        if not self._user or not self._password:
            raise LoopStepError(
                "login",
                "missing_credentials",
                f"no user/password given and {_USER_ENV_VAR}/{_PASSWORD_ENV_VAR} are unset",
            )
        response = self._request(
            "login",
            "POST",
            "/api/v1/auth/login",
            json_body={"username": self._user, "password": self._password},
        )
        if self._loopback_http:
            self._carry_loopback_session_cookie()
        return response.json()

    def _carry_loopback_session_cookie(self) -> None:
        """After a successful login on a loopback-http base, flip the real
        session cookie's own `Secure` flag off, in place, inside the jar.

        This mutates the EXACT `http.cookiejar.Cookie` object the login
        response's `Set-Cookie` header already caused the jar to store,
        correctly scoped to the right domain and path by httpx itself - it
        does not construct a fresh lookalike cookie. That distinction
        matters: `http.cookiejar` silently appends `.local` to a bareword
        host like "localhost" when it computes a cookie's domain (verified
        empirically against this exact host), so an attempt to re-set a
        same-named cookie with a hand-guessed `domain="localhost"` creates
        a SECOND, non-colliding jar entry that never matches any real
        request instead of replacing the first - a foot-gun this sidesteps
        entirely by never re-deriving the domain at all.

        Once flipped, `http.cookiejar`'s own gate for sending a cookie back
        (`return_ok_secure`: `cookie.secure and request.type != "https"`)
        is satisfied trivially, and the ordinary jar mechanism carries the
        cookie on every later call with no per-request code of any kind.
        """
        for cookie in self._client.cookies.jar:
            if cookie.name == _SESSION_COOKIE_NAME:
                cookie.secure = False
                self._loopback_cookie_carried = True

    # ------------------------------------------------------------------
    # Program — app.py:1742 (POST /api/v1/programs)
    # ------------------------------------------------------------------

    def create_program(
        self,
        name: str,
        description: str | None = None,
        direction: str = "aws_to_azure",
    ) -> JSONObject:
        """A fresh program (the loop makes one per run so estates never
        collide). Returns `ProgramResponse` JSON; `["id"]` is the program id
        every other method below takes."""
        response = self._request(
            "create_program",
            "POST",
            "/api/v1/programs",
            json_body={"name": name, "description": description, "direction": direction},
        )
        return response.json()

    # ------------------------------------------------------------------
    # AWS connection — app.py:2568 (POST .../aws/localstack-connections)
    # ------------------------------------------------------------------

    def register_env_connection(
        self,
        program_id: int,
        endpoint_url: str,
        *,
        region: str = "us-east-1",
        account_id: str = "000000000000",
        label: str | None = None,
    ) -> JSONObject:
        """Register the foundry environment's AWS endpoint as this program's
        source connection. `endpoint_url` is a required parameter, never
        hardcoded here: the endpoint must be reachable from wherever Prizmanyx
        actually EXTRACTS, which is its Temporal worker CONTAINER, not the API
        process - so the caller passes `http://host.docker.internal:<port>`
        (the worker-reachable form; see `loop.worker_reachable_endpoint`), NOT
        `http://localhost:<port>` (which inside the worker container means the
        container itself, and which the foundry's own host-side boto3
        verification uses instead). This method just carries whatever form the
        caller decides. `region` defaults to `"us-east-1"` because every
        floci-backed environment forces that region (see this repo's own
        `aws_connect_models.LocalStackConnectRequest`, which requires it with
        no default) - overridable, never assumed silently.
        """
        response = self._request(
            "register_env_connection",
            "POST",
            f"/api/v1/programs/{program_id}/aws/localstack-connections",
            json_body={
                "endpoint_url": endpoint_url,
                "region": region,
                "account_id": account_id,
                "label": label,
            },
        )
        return response.json()

    # ------------------------------------------------------------------
    # Extraction — app.py:3408 (POST /api/v1/pipeline-runs),
    #              app.py:3516 (GET /api/v1/pipeline-runs/{id})
    # ------------------------------------------------------------------

    def start_extraction(
        self,
        program_id: int,
        account_ids: Sequence[str],
        regions: Sequence[str],
        *,
        start_temporal: bool = True,
        **extra: object,
    ) -> JSONObject:
        """Start a pipeline run. Ingest and discover are internal workflow
        stages with no separate route - a `succeeded` run with a non-null
        `graph_run_id` (see `poll_pipeline_run`) IS discovery-complete.
        `**extra` passes any other `PipelineRunCreateRequest` field
        (`resource_types`, `azure_subscription_id`, `environment`, ...)
        straight through; the server 422s on anything it does not know.
        """
        response = self._request(
            "start_extraction",
            "POST",
            "/api/v1/pipeline-runs",
            json_body={
                "program_id": program_id,
                "account_ids": list(account_ids),
                "regions": list(regions),
                "start_temporal": start_temporal,
                **extra,
            },
        )
        return response.json()

    def get_pipeline_run(self, run_id: int) -> JSONObject:
        response = self._request("get_pipeline_run", "GET", f"/api/v1/pipeline-runs/{run_id}")
        return response.json()

    def poll_pipeline_run(
        self,
        run_id: int,
        *,
        max_attempts: int = _DEFAULT_RUN_POLL_MAX_ATTEMPTS,
        interval: float = _DEFAULT_RUN_POLL_INTERVAL_SECONDS,
    ) -> JSONObject:
        """Poll until the run reaches a terminal status (`succeeded`,
        `failed`, or `canceled`) and return that final row. Does NOT raise
        just because the terminal status is `failed`/`canceled` - that is a
        real outcome for the loop to interpret, not a client-level error.
        Raises `LoopStepError(step, "timeout", ...)` only if no terminal
        status is reached within `max_attempts`."""
        return self._poll(
            "poll_pipeline_run",
            lambda: self.get_pipeline_run(run_id),
            lambda row: row.get("status") in _PIPELINE_RUN_TERMINAL_STATUSES,
            max_attempts=max_attempts,
            interval=interval,
        )

    # ------------------------------------------------------------------
    # Destination catalog — app.py:2907 (POST .../destination/catalog/review)
    # ------------------------------------------------------------------

    def accept_catalog_defaults(self, program_id: int) -> JSONObject:
        """Accept-all-defaults for the Azure service catalog (no body - the
        route reads only the path parameter and the session user). This is
        deliberately the ONLY catalog route this client calls:
        `POST .../destination/catalog` (no `/review` suffix) is a per-service
        decision endpoint, not the accept-all-defaults gate, and calling it
        here would be the wrong route entirely."""
        response = self._request(
            "accept_catalog_defaults",
            "POST",
            f"/api/v1/programs/{program_id}/destination/catalog/review",
        )
        return response.json()

    # ------------------------------------------------------------------
    # Migration units — app.py:4638 (GET), :5727 (approve), :6047 (disposition)
    # ------------------------------------------------------------------

    def list_units(self, graph_run_id: int) -> JSONList:
        response = self._request(
            "list_units",
            "GET",
            "/api/v1/migration-units",
            params={"graph_run_id": graph_run_id},
        )
        return response.json()

    def approve_unit(self, unit_id: int) -> JSONObject:
        """The hard translate gate (no body - the route takes none)."""
        response = self._request(
            "approve_unit", "POST", f"/api/v1/migration-units/{unit_id}/approve"
        )
        return response.json()

    def decide_disposition(
        self,
        unit_id: int,
        disposition: Disposition = "rehost",
        *,
        rationale: str | None = None,
    ) -> JSONObject:
        """Record the 7-R decision. `rehost` is the loop's own default (it
        proceeds to translate); `retire`/`retain` are valid but block
        translate per-unit (`exit_gate.enforce_unit_translatable`) - passing
        either is a deliberate choice for the caller to make, not this
        client's to guess."""
        body: JSONObject = {"disposition": disposition}
        if rationale is not None:
            body["rationale"] = rationale
        response = self._request(
            "decide_disposition",
            "PUT",
            f"/api/v1/migration-units/{unit_id}/disposition",
            json_body=body,
        )
        return response.json()

    # ------------------------------------------------------------------
    # Sign-offs — app.py:9226 (POST .../sign-offs),
    #             app.py:9762 (GET .../exit-gate)
    # ------------------------------------------------------------------

    def sign_off(
        self,
        program_id: int,
        role: str,
        *,
        deliverable: str = "client_report",
        comment: str | None = None,
    ) -> JSONObject:
        """Sign one role for `deliverable` as the session user (`signed_by`
        is stamped server-side, never taken from the request). The gate
        needs all four required roles (`engineer`, `program_mgmt`,
        `delivery_md`, `customer`) signed for `client_report` - `role` stays
        a plain `str` here rather than a narrower type because the full
        role vocabulary (`sign_off_store.SIGN_OFF_ROLES`) also allows the
        optional `bd`, which this signature must not silently exclude."""
        body: JSONObject = {"role": role, "deliverable": deliverable}
        if comment is not None:
            body["comment"] = comment
        response = self._request(
            "sign_off", "POST", f"/api/v1/programs/{program_id}/sign-offs", json_body=body
        )
        return response.json()

    def get_exit_gate(self, program_id: int) -> JSONObject:
        response = self._request(
            "get_exit_gate", "GET", f"/api/v1/discovery/{program_id}/exit-gate"
        )
        return response.json()

    def poll_exit_gate(
        self,
        program_id: int,
        *,
        max_attempts: int = _DEFAULT_GATE_POLL_MAX_ATTEMPTS,
        interval: float = _DEFAULT_GATE_POLL_INTERVAL_SECONDS,
    ) -> JSONObject:
        """Poll until every exit-gate condition passes (or the run is
        `ungoverned`, which reports `passed: true` outright - see
        `exit_gate.evaluate_exit_gate`). Raises `LoopStepError(step,
        "timeout", ...)` if the gate has not opened within `max_attempts`."""
        return self._poll(
            "poll_exit_gate",
            lambda: self.get_exit_gate(program_id),
            lambda result: bool(result.get("passed")),
            max_attempts=max_attempts,
            interval=interval,
        )

    # ------------------------------------------------------------------
    # Translate — app.py:5778 (POST .../translate), :6074 (GET translations)
    # ------------------------------------------------------------------

    def translate_unit(self, unit_id: int) -> JSONObject:
        """Start translation of one approved unit (no body). Returns
        `{"workflow_id": ..., "migration_unit_id": ...}` - the translation's
        own id is a separate lookup (`list_translations`), since the
        translation row is created by the workflow, not this route."""
        response = self._request(
            "translate_unit", "POST", f"/api/v1/migration-units/{unit_id}/translate"
        )
        return response.json()

    def list_translations(self, unit_id: int) -> JSONList:
        response = self._request(
            "list_translations",
            "GET",
            "/api/v1/translations",
            params={"migration_unit_id": unit_id},
        )
        return response.json()

    # ------------------------------------------------------------------
    # Export — app.py:9062 (GET .../program-export)
    # ------------------------------------------------------------------

    def export_program(self, program_id: int, fmt: str = "csv") -> httpx.Response:
        """Export the cockpit pack for `program_id` as CSV or HTML.

        Returns the raw `httpx.Response` rather than parsed JSON - unlike
        every other method here, this route answers `text/csv` or
        `text/html` (see `program_export.build_program_csv`/`_html`), so
        there is no JSON to parse. The caller reads `.text`/`.content` and,
        if it wants the server's suggested filename, the
        `Content-Disposition` response header.

        This is the program-scoped export, a deliberate choice over the
        enterprise ENGAGEMENT export: the engagement route needs a team and
        an engagement record this client never creates, while
        program-export needs only the `program_id` this client already
        holds from `create_program` - the pragmatic path for an
        operator-only client with no team-setup story of its own.
        """
        return self._request(
            "export_program",
            "GET",
            f"/api/v1/programs/{program_id}/program-export",
            params={"format": fmt},
        )

    # ------------------------------------------------------------------
    # Deploy preview — deployments_routes.py:176 (POST .../deployments/trigger)
    # ------------------------------------------------------------------

    def deploy_preview(
        self,
        translation_id: int,
        *,
        azure_subscription_id: str = _PREVIEW_PLACEHOLDER_SUBSCRIPTION_ID,
        azure_resource_group: str = _PREVIEW_PLACEHOLDER_RESOURCE_GROUP,
    ) -> JSONObject:
        """Trigger a PREVIEW deploy (never `staging_apply`/production - this
        client offers no way to type the confirmation those modes require,
        by design). Placeholder Azure identifiers are sent by default so
        preview works with no real Azure subscription configured anywhere -
        the route 422s without SOME subscription id and resource group,
        either from the body or from server-side environment variables a
        bare foundry checkout will not have set. Gated on the exit gate and
        design-certification (both surface as 409s, handled generically by
        `_request` like every other error)."""
        response = self._request(
            "deploy_preview",
            "POST",
            "/api/v1/deployments/trigger",
            json_body={
                "translation_id": translation_id,
                "mode": "preview",
                "azure_subscription_id": azure_subscription_id,
                "azure_resource_group": azure_resource_group,
            },
        )
        return response.json()
