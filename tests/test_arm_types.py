"""Tests for foundry.arm_types: the translator-token -> ARM PUT table.

Pure, no I/O, no docker, no network - every case is a deterministic mapping
from a Pulumi azure-native token (what the translator/codegen emit) to the
exact ARM PUT (path, api-version, body) the deploy half of slice 4 issues at a
per-env floci-az.

The oracle for the original five shapes is
docs/superpowers/specs/2026-08-30-floci-spike-findings.md (Leg 3, Step 2 Probe
1): the five that PUT/GET clean against floci-az, 200/201, unauthenticated.
Postgres (2026-08-31) and NetworkInterface (2026-09-02) were proven live
afterwards against the same digest-pinned image, and their tests assert the
exact proven body. VM alone is still unproven - it carries a minimal
placeholder body (see the module docstring), so its test asserts only the
structural half (path type + name + api-version + location), which is all
structural verify checks.

Cross-repo contract guard (_EXPECTED_ARM_TYPE): each token's PUT path embeds an
ARM provider/resourceType that, lowercased, must equal the key the Prizmanyx
verify bridge (packages/verifier/prizmanyx_verifier/arm_types.py's
ARM_TYPE_TO_AZURE_NATIVE) maps BACK to the same token. If these two tables ever
drift, verify reports every deployed resource as an unexplained "extra" - the
exact cross-language-contract-pins failure mode. Pinned here by copying the
bridge's own keys; a foundry test cannot import the Prizmanyx package.
"""

from __future__ import annotations

import dataclasses

import pytest

from foundry.arm_types import (
    SUPPORTED_TOKENS,
    ArmPut,
    azure_native_to_arm_put,
)

# token -> (lowercased ARM provider/resourceType the Prizmanyx bridge round-
# trips back to this token). The resource group is subscription-level and has
# no provider segment, so its ARM type is asserted from the whole-path shape
# separately (test_resource_group), not through this table.
_EXPECTED_ARM_TYPE = {
    "azure_native.storage.StorageAccount": "microsoft.storage/storageaccounts",
    "azure_native.network.VirtualNetwork": "microsoft.network/virtualnetworks",
    "azure_native.network.Subnet": "microsoft.network/virtualnetworks/subnets",
    "azure_native.network.NetworkSecurityGroup": "microsoft.network/networksecuritygroups",
    "azure_native.network.NetworkInterface": "microsoft.network/networkinterfaces",
    "azure_native.compute.VirtualMachine": "microsoft.compute/virtualmachines",
    "azure_native.dbforpostgresql.Server": "microsoft.dbforpostgresql/flexibleservers",
}


# --- resource group: subscription-level path, not under a resource group ----


def test_resource_group_put_is_subscription_level() -> None:
    put = azure_native_to_arm_put("azure_native.resources.ResourceGroup", "my-rg", "ignored-rg", {})
    assert put is not None
    # Subscription-level: "resourcegroups/{name}", NOT ".../resourceGroups/{rg}/...".
    assert put.path == "resourcegroups/my-rg"
    assert "/providers/" not in put.path
    assert put.api_version == "2021-04-01"
    assert put.body == {"location": "eastus"}


def test_resource_group_takes_location_from_properties() -> None:
    put = azure_native_to_arm_put(
        "azure_native.resources.ResourceGroup", "my-rg", "my-rg", {"location": "westus2"}
    )
    assert put is not None
    assert put.body == {"location": "westus2"}


# --- storage account (S3) ---------------------------------------------------


def test_storage_account_put() -> None:
    put = azure_native_to_arm_put(
        "azure_native.storage.StorageAccount", "spikestorageacct1", "spike-rg", {}
    )
    assert put is not None
    assert (
        put.path
        == "resourceGroups/spike-rg/providers/Microsoft.Storage/storageAccounts/spikestorageacct1"
    )
    assert put.api_version == "2023-01-01"
    assert put.body == {
        "location": "eastus",
        "sku": {"name": "Standard_LRS"},
        "kind": "StorageV2",
    }


# --- virtual network (VPC) --------------------------------------------------


def test_virtual_network_put() -> None:
    put = azure_native_to_arm_put(
        "azure_native.network.VirtualNetwork", "spike-vnet", "spike-rg", {}
    )
    assert put is not None
    assert (
        put.path == "resourceGroups/spike-rg/providers/Microsoft.Network/virtualNetworks/spike-vnet"
    )
    assert put.api_version == "2023-04-01"
    assert put.body == {
        "location": "eastus",
        "properties": {"addressSpace": {"addressPrefixes": ["10.0.0.0/16"]}},
    }


# --- subnet (nested under its vnet) -----------------------------------------


def test_subnet_put_uses_parent_vnet_from_properties() -> None:
    put = azure_native_to_arm_put(
        "azure_native.network.Subnet",
        "spike-subnet",
        "spike-rg",
        {"virtual_network_name": "spike-vnet"},
    )
    assert put is not None
    assert put.path == (
        "resourceGroups/spike-rg/providers/Microsoft.Network/"
        "virtualNetworks/spike-vnet/subnets/spike-subnet"
    )
    assert put.api_version == "2023-04-01"
    assert put.body == {"properties": {"addressPrefix": "10.0.1.0/24"}}


def test_subnet_put_derives_deterministic_parent_when_absent() -> None:
    # No virtual_network_name in properties: the translator names a subnet
    # "${stem}-subnet" and its vnet "${stem}-vnet", so the fallback recovers
    # the stem and reattaches "-vnet".
    put = azure_native_to_arm_put("azure_native.network.Subnet", "web-subnet", "spike-rg", {})
    assert put is not None
    assert "/virtualNetworks/web-vnet/subnets/web-subnet" in put.path


def test_subnet_fallback_parent_when_name_has_no_subnet_suffix() -> None:
    put = azure_native_to_arm_put("azure_native.network.Subnet", "plain", "spike-rg", {})
    assert put is not None
    assert "/virtualNetworks/plain-vnet/subnets/plain" in put.path


# --- network security group (SG) --------------------------------------------


def test_network_security_group_put() -> None:
    put = azure_native_to_arm_put(
        "azure_native.network.NetworkSecurityGroup", "spike-nsg", "spike-rg", {}
    )
    assert put is not None
    assert (
        put.path
        == "resourceGroups/spike-rg/providers/Microsoft.Network/networkSecurityGroups/spike-nsg"
    )
    assert put.api_version == "2023-04-01"
    assert put.body == {"location": "eastus", "properties": {"securityRules": []}}


# --- network interface (ENI) - live-proven 2026-09-02 -----------------------


def test_network_interface_put_is_the_live_proven_body() -> None:
    put = azure_native_to_arm_put(
        "azure_native.network.NetworkInterface", "web-nic", "spike-rg", {}
    )
    assert put is not None
    assert (
        put.path == "resourceGroups/spike-rg/providers/Microsoft.Network/networkInterfaces/web-nic"
    )
    assert put.api_version == "2023-04-01"
    # Location-only is the MINIMAL body floci-az accepts (probe A, 200), and it
    # reads back with a synthesized ipConfigurations entry nobody sent. The
    # omission of ipConfigurations is deliberate, not an oversight: a real
    # Azure NIC needs a subscription-qualified subnet id, and this function is
    # never given the subscription - see the match arm's probe record.
    assert put.body == {"location": "eastus"}


def test_network_interface_carries_no_fabricated_subnet_reference() -> None:
    # Guards the decision directly: if someone later adds an ipConfigurations
    # entry here, it can only be built from a subscription this function does
    # not receive, so the id would be invented. Fail loudly if that appears.
    put = azure_native_to_arm_put(
        "azure_native.network.NetworkInterface",
        "web-nic",
        "spike-rg",
        {"subnet_id": "/subscriptions/whatever/resourceGroups/x/…"},
    )
    assert put is not None
    assert "ipConfigurations" not in put.body.get("properties", {})
    assert "subscriptions" not in str(put.body)


def test_network_interface_location_override() -> None:
    put = azure_native_to_arm_put(
        "azure_native.network.NetworkInterface", "web-nic", "spike-rg", {"location": "westus2"}
    )
    assert put is not None
    assert put.body == {"location": "westus2"}


# --- compute VM (EC2) - not spike-proven, minimal placeholder body ----------


def test_virtual_machine_put_is_minimal_and_correctly_typed() -> None:
    put = azure_native_to_arm_put("azure_native.compute.VirtualMachine", "web-vm", "spike-rg", {})
    assert put is not None
    assert put.path == "resourceGroups/spike-rg/providers/Microsoft.Compute/virtualMachines/web-vm"
    assert put.api_version == "2023-07-01"
    # Structural verify only needs existence+type+name; body carries location
    # plus placeholders, nothing more.
    assert put.body["location"] == "eastus"


# --- dbforpostgresql flexible server (RDS) - not spike-proven, minimal ------


def test_postgres_flexible_server_put_is_the_live_proven_202_shape() -> None:
    # Body proven LIVE against floci-az (2026-08-31): administratorLogin (etc.)
    # are REQUIRED (400 without them) and are throwaway emulator placeholders.
    # This create returns 202 (async) — Task 3's materializer polls it to
    # provisioningState=Succeeded before verify.
    put = azure_native_to_arm_put("azure_native.dbforpostgresql.Server", "web-db", "spike-rg", {})
    assert put is not None
    assert put.path == (
        "resourceGroups/spike-rg/providers/Microsoft.DBforPostgreSQL/flexibleServers/web-db"
    )
    assert put.api_version == "2022-12-01"
    assert put.body == {
        "location": "eastus",
        "sku": {"name": "Standard_B1ms", "tier": "Burstable"},
        "properties": {
            "administratorLogin": "pgadmin",
            "administratorLoginPassword": "Placeholder1!",
            "version": "16",
            "storage": {"storageSizeGB": 32},
        },
    }


# --- unmapped token ---------------------------------------------------------


def test_unmapped_token_returns_none() -> None:
    assert (
        azure_native_to_arm_put("azure_native.appsync.GraphQLApi", "whatever", "spike-rg", {})
        is None
    )


def test_empty_token_returns_none() -> None:
    assert azure_native_to_arm_put("", "whatever", "spike-rg", {}) is None


# --- name + resource group thread into the path -----------------------------


def test_name_and_resource_group_thread_into_path() -> None:
    put = azure_native_to_arm_put("azure_native.storage.StorageAccount", "acctname", "customrg", {})
    assert put is not None
    assert put.path == (
        "resourceGroups/customrg/providers/Microsoft.Storage/storageAccounts/acctname"
    )


# --- location default vs override -------------------------------------------


def test_default_location_when_properties_carry_none() -> None:
    put = azure_native_to_arm_put("azure_native.storage.StorageAccount", "acct", "rg", {})
    assert put is not None
    assert put.body["location"] == "eastus"


def test_location_override_from_properties() -> None:
    put = azure_native_to_arm_put(
        "azure_native.storage.StorageAccount", "acct", "rg", {"location": "northeurope"}
    )
    assert put is not None
    assert put.body["location"] == "northeurope"


# --- cross-repo contract: PUT path type round-trips through the bridge -------


def _arm_type_from_path(path: str) -> str:
    """Derive the ARM ``type`` (``namespace/resourceType[/nested...]``) from a
    PUT path exactly as ARM itself reports it: after ``/providers/`` the
    segments alternate resourceType/name, so the type is the namespace plus
    every resourceType segment with the intervening name segments dropped
    (``.../Microsoft.Network/virtualNetworks/<vnet>/subnets/<name>`` ->
    ``Microsoft.Network/virtualNetworks/subnets``)."""
    _, _, after = path.partition("/providers/")
    segments = after.split("/")
    namespace = segments[0]
    resource_types = segments[1::2]  # type1, type2, ... skipping the names
    return "/".join([namespace, *resource_types])


@pytest.mark.parametrize(("token", "expected_arm_type"), sorted(_EXPECTED_ARM_TYPE.items()))
def test_put_path_type_round_trips_to_bridge_key(token: str, expected_arm_type: str) -> None:
    # The ARM type embedded in the PUT path, lowercased, must equal the key the
    # Prizmanyx verify bridge maps BACK to this token - else verify would never
    # match the deployed resource.
    put = azure_native_to_arm_put(token, "res", "rg", {"virtual_network_name": "vnet"})
    assert put is not None
    assert "/providers/" in put.path  # a provider-scoped resource
    assert _arm_type_from_path(put.path).lower() == expected_arm_type


def test_every_supported_token_produces_a_put() -> None:
    for token in SUPPORTED_TOKENS:
        put = azure_native_to_arm_put(token, "res", "rg", {"virtual_network_name": "vnet"})
        assert put is not None, f"{token} is in SUPPORTED_TOKENS but produced no ArmPut"
        assert isinstance(put, ArmPut)
        assert put.api_version
        assert put.path


def test_supported_tokens_matches_the_documented_set() -> None:
    assert (
        frozenset(
            {
                "azure_native.resources.ResourceGroup",
                "azure_native.storage.StorageAccount",
                "azure_native.network.VirtualNetwork",
                "azure_native.network.Subnet",
                "azure_native.network.NetworkSecurityGroup",
                "azure_native.network.NetworkInterface",
                "azure_native.compute.VirtualMachine",
                "azure_native.dbforpostgresql.Server",
            }
        )
        == SUPPORTED_TOKENS
    )


# --- injection safety: a name/rg/parent lands in a URL path -----------------

_HOSTILE_NAMES = [
    "../../etc/passwd",
    "a/b",
    "name/../../../x",
    "..",
    ".hidden",
    "with space",
    "semi;colon",
    "back\\slash",
    "percent%2e%2e",
    "question?mark",
    "hash#frag",
    "at@sign",
    "new\nline",
    "",
]


@pytest.mark.parametrize("hostile", _HOSTILE_NAMES)
def test_path_injection_shaped_name_is_rejected(hostile: str) -> None:
    with pytest.raises(ValueError):
        azure_native_to_arm_put("azure_native.storage.StorageAccount", hostile, "spike-rg", {})


@pytest.mark.parametrize("hostile", _HOSTILE_NAMES)
def test_path_injection_shaped_resource_group_is_rejected(hostile: str) -> None:
    with pytest.raises(ValueError):
        azure_native_to_arm_put("azure_native.storage.StorageAccount", "acct", hostile, {})


# An EMPTY virtual_network_name is "absent", not hostile - it correctly falls
# back to the deterministic derived parent (tested above), so it is excluded
# from the parent-vnet rejection cases; every other hostile shape must raise.
@pytest.mark.parametrize("hostile", [h for h in _HOSTILE_NAMES if h])
def test_path_injection_shaped_parent_vnet_is_rejected(hostile: str) -> None:
    with pytest.raises(ValueError):
        azure_native_to_arm_put(
            "azure_native.network.Subnet",
            "spike-subnet",
            "spike-rg",
            {"virtual_network_name": hostile},
        )


def test_hostile_name_on_unmapped_token_still_returns_none_not_raise() -> None:
    # Token membership is checked first, so an unmapped token is skipped
    # (None) without ever validating - a hostile name only raises for a token
    # we would actually PUT.
    assert azure_native_to_arm_put("azure_native.appsync.GraphQLApi", "../evil", "rg", {}) is None


# --- ArmPut is a frozen dataclass -------------------------------------------


def test_arm_put_is_frozen() -> None:
    put = azure_native_to_arm_put("azure_native.resources.ResourceGroup", "rg", "rg", {})
    assert put is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        put.path = "mutated"  # type: ignore[misc]
