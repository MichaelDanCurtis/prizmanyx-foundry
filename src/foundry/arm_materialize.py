"""The ARM materializer: PUT a translation's EXPECTED Azure estate into a
per-env floci-az, so Prizmanyx's verify re-reads it and diffs against expected.

This is the deploy half of slice 4. Its input is exactly what Prizmanyx's
``GET /translations/{id}/expected-resources`` returns - a list of
``{azure_resource_type (token), azure_logical_name, azure_properties}`` rows -
and its job is to turn each row into the concrete ARM PUT that
``arm_types.azure_native_to_arm_put`` describes, issue it against a host-side
floci-az, and record HONESTLY what happened, so the loop (Task 5) can persist
the outcome and the verify bridge can re-read the estate.

Three contract edges hold this to the verify bridge (Prizmanyx PR #334), or
the round-trip silently breaks:

1. **Name.** The verify differ matches expected vs. actual on
   ``(azure_resource_type, azure_logical_name)``. The NAME each resource is PUT
   under is threaded straight into the ARM path by ``azure_native_to_arm_put``,
   so it equals the translator's ``azure_logical_name`` - this module passes it
   through untouched.
2. **The resource group is its own match.** ARM's "list resources in a group"
   endpoint never returns the group itself, so the verify bridge fetches the RG
   separately (``get_resource_group``) and matches the deployed RG's NAME
   against the translator's expected ``ResourceGroup`` ``azure_logical_name``.
   The caller therefore passes ``resource_group`` = that expected name, and this
   module PUTs the RG under it FIRST, exactly once. The expected-resources list
   ALSO carries a ``ResourceGroup`` row (codegen emits the RG scaffold per
   unit); the resource loop skips it, so the RG is materialized once, not twice.
3. **Async provisioning.** The PostgreSQL flexible server returns 202, not the
   synchronous 200/201 every other proven shape returns; verify would race a
   still-provisioning server and report it missing. So a 202 is POLLED to
   ``provisioningState == "Succeeded"`` (bounded) before the apply is called
   done - and a poll that never reaches Succeeded is a RECORDED timeout, never a
   hang.

**Honesty, not swallowing.** A token this materializer's table does not cover
is a ``skipped_unmapped`` entry (it will surface as ``missing`` at verify - the
honest signal); a non-2xx PUT is a ``failures`` entry; a 202 that provisions
``Failed``/``Canceled`` is a ``failures`` entry; a 202 that never terminates
within the poll bound is a ``provisioning_timeouts`` entry. The apply is ``ok``
only if every MAPPED resource reached a terminal success. A path-unsafe
resource name is NOT swallowed into any of these - ``azure_native_to_arm_put``
raises ``InvalidAzureNameError`` for it, and this module lets it propagate,
because a hostile name is a translator bug or an attack, never a resource to
quietly skip.

**Child recording.** floci-az is a real-engine host: a PostgreSQL PUT spawns a
``floci-az-pg-<name>`` postgres child on the host daemon (see
``engine._DOCKER_SOCKET_MOUNT``). This module snapshots the running
``floci-``-prefixed containers immediately before and after the apply and
returns the delta as ``spawned_children``, mirroring ``tf_runner.apply``'s
ownership-by-recording so the loop can persist it into ``EnvState.children``
and ``engine.remove_env`` can reap them. Same before/after-window limitation
applies: a concurrent peer spawning its own child would be misattributed - an
accepted trade-off for a single-operator local tool, exactly as on the AWS side.

``az_base_url`` is the HOST-side address (``http://localhost:<az-port>``) - the
foundry PUTs from the host, distinct from the ``host.docker.internal`` address
the Prizmanyx worker reaches floci-az at for its own verify read.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from foundry.arm_types import ArmPut, azure_native_to_arm_put
from foundry.engine import run_docker

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

# The one token that is the resource-group CONTAINER, not a resource inside it.
# PUT once in step 1; skipped when the resource loop meets its expected-list row.
_RESOURCE_GROUP_TOKEN = "azure_native.resources.ResourceGroup"

# Dependency-sane PUT order: a subnet's ARM path nests under its vnet, so the
# vnet must exist first. NSG has no hard dependency but travels with the subnet
# tier per the slice-4 ordering (RG -> VNet -> Subnet/NSG -> the rest). Rank not
# listed here = "the rest" (rank 2). Python's sort is stable, so resources of
# equal rank keep their input order.
_TOKEN_RANK: dict[str, int] = {
    "azure_native.network.VirtualNetwork": 0,
    "azure_native.network.Subnet": 1,
    "azure_native.network.NetworkSecurityGroup": 1,
}
_REST_RANK = 2

# ARM async-create signal: 202 Accepted means "provisioning started", not done.
# Every proven-synchronous shape returns 200/201 and needs no poll.
_ASYNC_STATUS_CODE = 202

# provisioningState verdicts, from the ARM long-running-operation contract.
_PROVISIONING_SUCCEEDED = "Succeeded"
_PROVISIONING_TERMINAL_FAILURES = frozenset({"Failed", "Canceled"})

# Poll budget for a 202: ~30 attempts x 2s = a minute of headroom, matching the
# order of magnitude a real flexible-server create takes to go healthy, while
# still terminating rather than hanging. Overridable per call (tests pass 0s).
_PROVISION_POLL_MAX_ATTEMPTS = 30
_PROVISION_POLL_INTERVAL_SECONDS = 2.0

_FLOCI_PS_FILTER = "name=floci-"

_ARM_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class ArmResponse:
    """One ARM response reduced to what this materializer needs: the HTTP
    status and the parsed JSON body (``{}`` when the response carries none)."""

    status_code: int
    body: dict[str, Any]


@dataclass(frozen=True)
class ArmApplyResult:
    """The outcome of one ``apply_expected_resources`` call.

    ``ok`` is True iff every MAPPED resource reached a terminal success - no
    ``failures`` and no ``provisioning_timeouts``. ``skipped_unmapped`` does NOT
    affect ``ok``: an unmapped token is a coverage gap that surfaces as
    ``missing`` at verify, not a failure of the apply. ``applied`` counts the
    resources (the RG included) that landed successfully. ``failures`` carries
    ``{name, status, body}`` per non-2xx PUT or provisioning failure;
    ``provisioning_timeouts`` carries the names of 202 resources that never
    reached Succeeded within the poll bound. ``spawned_children`` is the
    before/after ``docker ps`` delta - the floci-az real-engine children this
    apply spawned, for the loop to persist into ``EnvState.children``.
    """

    ok: bool
    applied: int
    skipped_unmapped: list[str]
    failures: list[dict[str, Any]]
    provisioning_timeouts: list[str]
    spawned_children: list[str]


class ArmPutClient(Protocol):
    """The injected ARM PUT/GET seam. ``path`` is the full ARM path AFTER the
    base URL (``subscriptions/{sub}/...``); the client prepends the floci-az
    base URL and appends ``?api-version={api_version}``. Mirrors the verifier's
    ``ArmClient`` Protocol so both halves of the round-trip share one shape."""

    def put(self, path: str, api_version: str, body: dict[str, Any]) -> ArmResponse: ...

    def get(self, path: str, api_version: str) -> ArmResponse: ...


class HttpxArmPutClient:
    """Real ARM PUT/GET client, backed by ``httpx``.

    ``base_url`` (the host-side floci-az address) and optional ``headers`` are
    injected - this class never decides for itself whether it talks to real
    Azure or a local double. ``httpx`` is imported inside the method bodies (not
    at module scope) so the pure Protocol boundary never has to import it,
    mirroring the verifier's ``HttpxArmClient``. A ``transport`` seam lets tests
    exercise the real URL/param/body construction with zero sockets.
    """

    def __init__(
        self,
        base_url: str,
        headers: dict[str, str] | None = None,
        *,
        timeout: float = _ARM_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = headers or {}
        self._timeout = timeout
        self._transport = transport

    def put(self, path: str, api_version: str, body: dict[str, Any]) -> ArmResponse:
        import httpx  # lazy so the pure Protocol boundary never imports httpx

        with httpx.Client(timeout=self._timeout, transport=self._transport) as http:
            response = http.put(
                f"{self._base_url}/{path}",
                params={"api-version": api_version},
                json=body,
                headers=self._headers,
            )
        return ArmResponse(status_code=response.status_code, body=_safe_json(response))

    def get(self, path: str, api_version: str) -> ArmResponse:
        import httpx  # lazy so the pure Protocol boundary never imports httpx

        with httpx.Client(timeout=self._timeout, transport=self._transport) as http:
            response = http.get(
                f"{self._base_url}/{path}",
                params={"api-version": api_version},
                headers=self._headers,
            )
        return ArmResponse(status_code=response.status_code, body=_safe_json(response))


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    """The response's JSON object, or ``{}`` when it carries no JSON body (a 202
    from floci-az can be empty). A non-object JSON payload is also flattened to
    ``{}`` - this materializer only ever reads ``properties.provisioningState``
    and ``error``, both object-shaped."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


@dataclass(frozen=True)
class _ResourceOutcome:
    """One resource's terminal verdict, aggregated by the caller. Exactly one
    of the three states is set: applied (a success), a failure record, or a
    provisioning-timeout name."""

    applied: bool = False
    failure: dict[str, Any] | None = None
    provisioning_timeout: str | None = None


def apply_expected_resources(
    az_base_url: str,
    subscription_id: str,
    resource_group: str,
    resources: list[dict[str, Any]],
    *,
    arm_client: ArmPutClient | None = None,
    poll_max_attempts: int = _PROVISION_POLL_MAX_ATTEMPTS,
    poll_interval_seconds: float = _PROVISION_POLL_INTERVAL_SECONDS,
) -> ArmApplyResult:
    """PUT the expected Azure estate into a host-side floci-az and report it.

    ``resources`` is the ``GET /translations/{id}/expected-resources`` shape:
    ``[{azure_resource_type, azure_logical_name, azure_properties}]``.
    ``resource_group`` is the translator's expected ResourceGroup
    ``azure_logical_name`` - the RG is PUT under it FIRST, then every other
    resource in dependency-sane order. ``arm_client`` is injected for tests; in
    production it defaults to a ``HttpxArmPutClient`` built from ``az_base_url``.

    See the module docstring for the honesty model (skips vs. failures vs.
    timeouts), the RG-once contract, and the child recording. Lets an
    ``InvalidAzureNameError`` from a path-unsafe resource name propagate rather
    than swallowing it.
    """
    if arm_client is None:
        arm_client = HttpxArmPutClient(az_base_url)

    before = _running_floci_container_names()

    applied = 0
    skipped_unmapped: list[str] = []
    failures: list[dict[str, Any]] = []
    provisioning_timeouts: list[str] = []

    def _record(outcome: _ResourceOutcome) -> None:
        nonlocal applied
        if outcome.applied:
            applied += 1
        if outcome.failure is not None:
            failures.append(outcome.failure)
        if outcome.provisioning_timeout is not None:
            provisioning_timeouts.append(outcome.provisioning_timeout)

    # Step 1: ensure the resource group exists, under the expected name, first.
    rg_put = azure_native_to_arm_put(_RESOURCE_GROUP_TOKEN, resource_group, resource_group, {})
    assert rg_put is not None  # ResourceGroup is always a supported token
    _record(
        _apply_one(
            arm_client,
            resource_group,
            rg_put,
            subscription_id,
            poll_max_attempts=poll_max_attempts,
            poll_interval_seconds=poll_interval_seconds,
        )
    )

    # Step 2: each resource, in dependency-sane order; the RG scaffold row is
    # skipped (already ensured in step 1 - never double-PUT).
    for resource in _ordered(resources):
        token = str(resource.get("azure_resource_type", ""))
        name = str(resource.get("azure_logical_name", ""))
        properties = resource.get("azure_properties") or {}
        if token == _RESOURCE_GROUP_TOKEN:
            continue
        put = azure_native_to_arm_put(token, name, resource_group, properties)
        if put is None:
            skipped_unmapped.append(token)
            logger.info("arm_materialize.skipped_unmapped token=%s name=%s", token, name)
            continue
        _record(
            _apply_one(
                arm_client,
                name,
                put,
                subscription_id,
                poll_max_attempts=poll_max_attempts,
                poll_interval_seconds=poll_interval_seconds,
            )
        )

    after = _running_floci_container_names()
    spawned_children = sorted(after - before)

    return ArmApplyResult(
        ok=not failures and not provisioning_timeouts,
        applied=applied,
        skipped_unmapped=skipped_unmapped,
        failures=failures,
        provisioning_timeouts=provisioning_timeouts,
        spawned_children=spawned_children,
    )


def _ordered(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resources sorted dependency-sane (VNet -> Subnet/NSG -> the rest), stable
    within each tier so equal-rank resources keep their input order."""
    return sorted(
        resources,
        key=lambda r: _TOKEN_RANK.get(str(r.get("azure_resource_type", "")), _REST_RANK),
    )


def _apply_one(
    arm_client: ArmPutClient,
    name: str,
    put: ArmPut,
    subscription_id: str,
    *,
    poll_max_attempts: int,
    poll_interval_seconds: float,
) -> _ResourceOutcome:
    """PUT one resource and classify the result: an applied success, a failure
    ``{name, status, body}``, or a provisioning-timeout name. A 202 is polled to
    a terminal provisioning state; a 200/201 is terminal on its own."""
    full_path = f"subscriptions/{subscription_id}/{put.path}"
    response = arm_client.put(full_path, put.api_version, put.body)
    status = response.status_code

    if status == _ASYNC_STATUS_CODE:
        return _poll_async(
            arm_client,
            name,
            full_path,
            put.api_version,
            max_attempts=poll_max_attempts,
            interval=poll_interval_seconds,
        )

    if 200 <= status < 300:
        return _ResourceOutcome(applied=True)

    logger.warning("arm_materialize.put_failed name=%s status=%s", name, status)
    return _ResourceOutcome(failure={"name": name, "status": status, "body": response.body})


def _poll_async(
    arm_client: ArmPutClient,
    name: str,
    full_path: str,
    api_version: str,
    *,
    max_attempts: int,
    interval: float,
) -> _ResourceOutcome:
    """Poll a 202 resource's GET to a terminal provisioning state.

    ``Succeeded`` -> applied. ``Failed``/``Canceled`` -> a failure (decisively,
    without polling to the bound). Neither within ``max_attempts`` -> a recorded
    provisioning timeout, never a hang.
    """
    last_state: str | None = None
    for attempt in range(1, max_attempts + 1):
        response = arm_client.get(full_path, api_version)
        last_state = _provisioning_state(response.body)
        if last_state == _PROVISIONING_SUCCEEDED:
            return _ResourceOutcome(applied=True)
        if last_state in _PROVISIONING_TERMINAL_FAILURES:
            logger.warning("arm_materialize.provisioning_failed name=%s state=%s", name, last_state)
            return _ResourceOutcome(
                failure={
                    "name": name,
                    "status": "provisioning_failed",
                    "body": {"provisioningState": last_state},
                }
            )
        if attempt < max_attempts:
            time.sleep(interval)

    logger.warning(
        "arm_materialize.provisioning_timeout name=%s last_state=%s attempts=%d",
        name,
        last_state,
        max_attempts,
    )
    return _ResourceOutcome(provisioning_timeout=name)


def _provisioning_state(body: dict[str, Any]) -> str | None:
    """``body["properties"]["provisioningState"]`` when present and a string,
    else None (a body that has not yet reported one is "not terminal")."""
    properties = body.get("properties")
    if isinstance(properties, dict):
        state = properties.get("provisioningState")
        if isinstance(state, str):
            return state
    return None


def _running_floci_container_names() -> set[str]:
    """Every currently-RUNNING container whose name starts with ``floci-``.

    A parallel to ``tf_runner._running_floci_container_names`` - the SAME
    ownership-by-recording mechanism the AWS side uses - kept local here so this
    module's ``run_docker`` is the seam tests monkeypatch, rather than reaching
    across into another module's private helper and its binding. The
    before/after delta over one apply is this apply's own floci-az real-engine
    children (``floci-az-pg-<name>``); the floci-az parent is already running
    before the window, so it never appears in the delta.
    """
    result = run_docker(["ps", "--filter", _FLOCI_PS_FILTER, "--format", "{{.Names}}"])
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}
