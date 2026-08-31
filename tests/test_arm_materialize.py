"""Tests for foundry.arm_materialize: PUT a translation's EXPECTED Azure
estate into a per-env floci-az, so Prizmanyx's verify re-reads it and diffs.

Two layers, both hermetic (no docker daemon, no real floci-az, no sockets):

- The materializer itself, driven by a FAKE `arm_client` (a plain object with
  `put`/`get`) and a FAKE `run_docker` (canned `docker ps` snapshots), so every
  branch - RG-first ordering, dependency-sane order, unmapped-skip, non-2xx
  failure, the 202 poll-to-Succeeded, the poll timeout, the poll-to-Failed, and
  the before/after child delta - is exercised with zero I/O.
- One `httpx.MockTransport` test of the real `HttpxArmPutClient`, proving it
  builds the ARM URL, threads `?api-version=`, sends the body, and parses the
  response - the same seam the verifier's own client is tested through.
"""

from __future__ import annotations

import subprocess
from typing import Any

from foundry.arm_materialize import (
    ArmApplyResult,
    ArmResponse,
    HttpxArmPutClient,
    apply_expected_resources,
)

_SUB = "00000000-0000-0000-0000-000000000000"
_RG = "demo-rg"

_RG_TOKEN = "azure_native.resources.ResourceGroup"
_STORAGE = "azure_native.storage.StorageAccount"
_VNET = "azure_native.network.VirtualNetwork"
_SUBNET = "azure_native.network.Subnet"
_NSG = "azure_native.network.NetworkSecurityGroup"
_VM = "azure_native.compute.VirtualMachine"
_POSTGRES = "azure_native.dbforpostgresql.Server"
_UNMAPPED = "azure_native.appsync.GraphQLApi"


def _res(token: str, name: str, properties: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "azure_resource_type": token,
        "azure_logical_name": name,
        "azure_properties": properties or {},
    }


class _FakeArmClient:
    """A fake ARM PUT/GET client. A PUT's status is decided by the resource's
    NAME (always the last path segment for every shape this materializer PUTs):
    in `fail_names` -> 400, in `async_names` -> 202, else 201. A GET answers the
    provisioning poll from a per-name state list: a 2+ element list is walked
    one state per GET, a 1-element list STICKS at that state forever (a
    never-Succeeds server), and an absent name answers "Succeeded" at once.
    """

    def __init__(
        self,
        *,
        fail_names: tuple[str, ...] = (),
        async_names: tuple[str, ...] = (),
        get_states: dict[str, list[str]] | None = None,
    ) -> None:
        self._fail_names = set(fail_names)
        self._async_names = set(async_names)
        self._get_states = {name: list(states) for name, states in (get_states or {}).items()}
        self.puts: list[tuple[str, str, dict[str, Any]]] = []
        self.gets: list[tuple[str, str]] = []

    @staticmethod
    def _name(path: str) -> str:
        return path.rsplit("/", 1)[-1]

    def put(self, path: str, api_version: str, body: dict[str, Any]) -> ArmResponse:
        self.puts.append((path, api_version, body))
        name = self._name(path)
        if name in self._fail_names:
            return ArmResponse(status_code=400, body={"error": {"message": "bad request"}})
        if name in self._async_names:
            return ArmResponse(status_code=202, body={})
        return ArmResponse(status_code=201, body={})

    def get(self, path: str, api_version: str) -> ArmResponse:
        self.gets.append((path, api_version))
        queue = self._get_states.get(self._name(path))
        if not queue:
            state = "Succeeded"
        elif len(queue) == 1:
            state = queue[0]
        else:
            state = queue.pop(0)
        return ArmResponse(status_code=200, body={"properties": {"provisioningState": state}})


class _FakeDocker:
    """Answers `docker ps --filter name=floci-` from a queue of canned name
    snapshots (one per call), enough to drive the materializer's before/after
    child-delta bookkeeping with no real docker."""

    def __init__(self, snapshots: list[set[str]]) -> None:
        self._snapshots = [set(s) for s in snapshots]
        self.calls: list[list[str]] = []

    def __call__(
        self,
        args: list[str],
        *,
        timeout: float | None = None,  # noqa: ARG002 - accepted because run_docker passes it
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args and args[0] == "ps":
            names = self._snapshots.pop(0) if self._snapshots else set()
            return subprocess.CompletedProcess(
                args, 0, stdout="".join(f"{n}\n" for n in sorted(names)), stderr=""
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _patch_docker(monkeypatch, before: set[str] = frozenset(), after: set[str] | None = None):
    """Install a fake `run_docker` answering the two `ps` snapshots (before and
    after the apply) the materializer takes. Defaults to no spawned children."""
    after = set(before) if after is None else after
    fake = _FakeDocker([set(before), set(after)])
    monkeypatch.setattr("foundry.arm_materialize.run_docker", fake)
    return fake


def _apply(resources, arm_client, monkeypatch, *, before=frozenset(), after=None, **kwargs):
    _patch_docker(monkeypatch, before=before, after=after)
    return apply_expected_resources(
        "http://localhost:14577", _SUB, _RG, resources, arm_client=arm_client, **kwargs
    )


def _put_names(client: _FakeArmClient) -> list[str]:
    return [path.rsplit("/", 1)[-1] for path, _, _ in client.puts]


# --- the resource group is PUT first, then each resource --------------------


def test_resource_group_is_put_first_then_each_resource(monkeypatch) -> None:
    client = _FakeArmClient()
    # Two same-rank ("the rest") resources, so this test isolates RG-first +
    # each-resource from the dependency-sane reordering its own tests cover.
    result = _apply([_res(_STORAGE, "acct1"), _res(_VM, "web-vm")], client, monkeypatch)

    assert isinstance(result, ArmApplyResult)
    # The very first PUT is the resource group, at the subscription-level path.
    assert client.puts[0][0] == f"subscriptions/{_SUB}/resourcegroups/{_RG}"
    assert _put_names(client) == [_RG, "acct1", "web-vm"]
    assert result.applied == 3  # RG + storage + vm
    assert result.ok
    assert result.failures == []
    assert result.skipped_unmapped == []
    assert result.provisioning_timeouts == []


# --- dependency-sane order: VNet before Subnet, whatever the input order -----


def test_vnet_is_put_before_its_subnet_regardless_of_input_order(monkeypatch) -> None:
    client = _FakeArmClient()
    # Subnet listed BEFORE its vnet in the input - the materializer must still
    # PUT the vnet first (the subnet's ARM path nests under the vnet).
    resources = [
        _res(_SUBNET, "app-subnet", {"virtual_network_name": "demo-vnet"}),
        _res(_VNET, "demo-vnet"),
    ]
    result = _apply(resources, client, monkeypatch)

    names = _put_names(client)
    assert names[0] == _RG
    assert names.index("demo-vnet") < names.index("app-subnet")
    assert result.ok


def test_nsg_and_subnet_follow_the_vnet(monkeypatch) -> None:
    client = _FakeArmClient()
    resources = [
        _res(_STORAGE, "acct1"),
        _res(_NSG, "demo-nsg"),
        _res(_SUBNET, "app-subnet", {"virtual_network_name": "demo-vnet"}),
        _res(_VNET, "demo-vnet"),
    ]
    result = _apply(resources, client, monkeypatch)

    names = _put_names(client)
    assert names.index("demo-vnet") < names.index("app-subnet")
    assert names.index("demo-vnet") < names.index("demo-nsg")
    # Storage (the-rest tier) never jumps ahead of the vnet either.
    assert names.index("demo-vnet") < names.index("acct1")
    assert result.applied == 5


# --- an unmapped token is skipped, not a crash ------------------------------


def test_unmapped_token_lands_in_skipped_unmapped_not_a_crash(monkeypatch) -> None:
    client = _FakeArmClient()
    result = _apply([_res(_STORAGE, "acct1"), _res(_UNMAPPED, "some-api")], client, monkeypatch)

    assert result.skipped_unmapped == [_UNMAPPED]
    # The unmapped resource was never PUT; the mapped ones still were.
    assert _put_names(client) == [_RG, "acct1"]
    assert result.applied == 2  # RG + storage
    # A skip is not a failure - every MAPPED resource still succeeded.
    assert result.ok
    assert result.failures == []


# --- a non-2xx PUT is a recorded failure, ok False --------------------------


def test_non_2xx_put_is_recorded_as_a_failure_and_ok_is_false(monkeypatch) -> None:
    client = _FakeArmClient(fail_names=("bad-acct",))
    result = _apply([_res(_STORAGE, "bad-acct")], client, monkeypatch)

    assert not result.ok
    assert result.failures == [
        {"name": "bad-acct", "status": 400, "body": {"error": {"message": "bad request"}}}
    ]
    assert result.provisioning_timeouts == []
    assert result.applied == 1  # only the RG landed


# --- a 202 polls its GET until Succeeded, then counts as applied ------------


def test_202_polls_until_succeeded_then_ok(monkeypatch) -> None:
    client = _FakeArmClient(
        async_names=("web-db",),
        get_states={"web-db": ["Creating", "Succeeded"]},
    )
    result = _apply(
        [_res(_POSTGRES, "web-db")],
        client,
        monkeypatch,
        poll_max_attempts=5,
        poll_interval_seconds=0,
    )

    assert result.ok
    assert result.applied == 2  # RG + the async db, once it reached Succeeded
    assert result.provisioning_timeouts == []
    assert result.failures == []
    # It actually polled the GET (not assumed done off the 202 alone).
    assert len(client.gets) == 2
    assert client.gets[0][0] == f"subscriptions/{_SUB}/{_provider_path('web-db')}"


def _provider_path(name: str) -> str:
    return f"resourceGroups/{_RG}/providers/Microsoft.DBforPostgreSQL/flexibleServers/{name}"


# --- a 202 that never Succeeds is a provisioning timeout, ok False ----------


def test_202_that_never_succeeds_is_a_provisioning_timeout_ok_false(monkeypatch) -> None:
    client = _FakeArmClient(
        async_names=("web-db",),
        get_states={"web-db": ["Creating"]},  # 1-element list = sticky, never Succeeds
    )
    result = _apply(
        [_res(_POSTGRES, "web-db")],
        client,
        monkeypatch,
        poll_max_attempts=3,
        poll_interval_seconds=0,
    )

    assert not result.ok
    assert result.provisioning_timeouts == ["web-db"]
    assert result.failures == []
    assert result.applied == 1  # only the RG; the db never reached Succeeded
    assert len(client.gets) == 3  # bounded, did not hang


# --- a 202 whose provisioning goes Failed is a failure, not a timeout -------


def test_202_provisioning_failed_is_recorded_as_a_failure(monkeypatch) -> None:
    client = _FakeArmClient(
        async_names=("web-db",),
        get_states={"web-db": ["Failed"]},
    )
    result = _apply(
        [_res(_POSTGRES, "web-db")],
        client,
        monkeypatch,
        poll_max_attempts=5,
        poll_interval_seconds=0,
    )

    assert not result.ok
    assert result.provisioning_timeouts == []
    assert result.failures == [
        {
            "name": "web-db",
            "status": "provisioning_failed",
            "body": {"provisioningState": "Failed"},
        }
    ]
    # Failed decisively on the first GET - it did not keep polling to the bound.
    assert len(client.gets) == 1


# --- the before/after child delta is captured -------------------------------


def test_spawned_children_are_captured_from_before_after_snapshots(monkeypatch) -> None:
    client = _FakeArmClient(async_names=("web-db",))
    result = _apply(
        [_res(_POSTGRES, "web-db")],
        client,
        monkeypatch,
        before={"floci-az-demo", "floci-demo"},
        after={"floci-az-demo", "floci-demo", "floci-az-pg-web-db"},
        poll_interval_seconds=0,
    )

    assert result.spawned_children == ["floci-az-pg-web-db"]
    assert result.ok


def test_no_spawned_children_when_the_snapshots_are_equal(monkeypatch) -> None:
    client = _FakeArmClient()
    result = _apply(
        [_res(_STORAGE, "acct1")],
        client,
        monkeypatch,
        before={"floci-az-demo"},
        after={"floci-az-demo"},
    )
    assert result.spawned_children == []


# --- the RG entry in the expected list is not double-PUT --------------------


def test_resource_group_entry_in_the_list_is_not_double_put(monkeypatch) -> None:
    # load_expected_resources returns the ResourceGroup scaffold as one of its
    # rows; the materializer ensures the RG once (step 1) and must NOT PUT it a
    # second time when it meets that same entry in the resource loop.
    client = _FakeArmClient()
    resources = [_res(_RG_TOKEN, _RG, {"location": "eastus"}), _res(_STORAGE, "acct1")]
    result = _apply(resources, client, monkeypatch)

    rg_path = f"subscriptions/{_SUB}/resourcegroups/{_RG}"
    assert sum(1 for path, _, _ in client.puts if path == rg_path) == 1
    assert _put_names(client) == [_RG, "acct1"]
    assert result.applied == 2


# --- location from properties threads into the RG/resource body -------------


def test_empty_resource_list_still_ensures_the_resource_group(monkeypatch) -> None:
    client = _FakeArmClient()
    result = _apply([], client, monkeypatch)
    assert _put_names(client) == [_RG]
    assert result.applied == 1
    assert result.ok


# --- the real HttpxArmPutClient, through httpx.MockTransport -----------------


def test_httpx_arm_put_client_builds_url_params_body_and_parses() -> None:
    import httpx

    seen: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), request.read()))
        if request.method == "PUT":
            return httpx.Response(201, json={})
        return httpx.Response(200, json={"properties": {"provisioningState": "Succeeded"}})

    transport = httpx.MockTransport(handler)
    client = HttpxArmPutClient("http://localhost:14577/", transport=transport)

    put_resp = client.put(
        f"subscriptions/{_SUB}/resourcegroups/{_RG}", "2021-04-01", {"location": "eastus"}
    )
    assert put_resp.status_code == 201

    get_resp = client.get(f"subscriptions/{_SUB}/resourcegroups/{_RG}", "2021-04-01")
    assert get_resp.status_code == 200
    assert get_resp.body["properties"]["provisioningState"] == "Succeeded"

    put_method, put_url, put_body = seen[0]
    assert put_method == "PUT"
    assert put_url == (
        f"http://localhost:14577/subscriptions/{_SUB}/resourcegroups/{_RG}?api-version=2021-04-01"
    )
    assert b'"location"' in put_body
    assert seen[1][0] == "GET"


def test_httpx_arm_put_client_returns_empty_body_when_response_is_not_json() -> None:
    import httpx

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, text="")  # a 202 with no JSON body

    client = HttpxArmPutClient("http://localhost:14577", transport=httpx.MockTransport(handler))
    resp = client.put("subscriptions/s/resourcegroups/rg", "2021-04-01", {})
    assert resp.status_code == 202
    assert resp.body == {}
