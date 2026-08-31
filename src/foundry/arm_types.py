"""Translator azure-native token -> ARM PUT (path, api-version, body).

The deploy half of slice 4 PUTs a translation's EXPECTED Azure resources into a
per-env floci-az over ARM REST, so Prizmanyx's verify (the merged ARM bridge)
re-reads them and diffs against expected. This module is the mapping from one
Pulumi azure-native token - the vocabulary packages/codegen emits into every
generated program, and the same vocabulary the translator's ``produces``
entries speak - to the concrete ARM PUT that materializes it.

Two contract edges this module must hold exactly, or the round-trip breaks:

1. **Name.** Prizmanyx's verify differ matches expected vs. actual on
   ``(azure_resource_type, azure_logical_name)``. The resource NAME the foundry
   PUTs is threaded straight into the ARM path, so it MUST equal the
   translator's ``azure_logical_name`` for that resource - the caller passes it
   in; this module never invents or transforms it.

2. **Type.** The ARM provider/resourceType embedded in the PUT path must be the
   one the Prizmanyx verify bridge
   (``packages/verifier/prizmanyx_verifier/arm_types.py``'s
   ``ARM_TYPE_TO_AZURE_NATIVE``) maps BACK to the same token. If they drift,
   verify reports every deployed resource as an unexplained "extra" and every
   expected one as "missing" - the cross-language-contract-pins failure mode.
   The two tables are pinned to each other by test (see
   ``tests/test_arm_types.py``'s ``_EXPECTED_ARM_TYPE``).

**The proven set vs. the placeholders.** The five shapes RG / StorageAccount /
VirtualNetwork / Subnet / NetworkSecurityGroup are the ones the floci spike
PUT/GET clean, 200/201, unauthenticated (findings doc Leg 3, Step 2 Probe 1) -
their bodies are copied verbatim from that evidence. VirtualMachine and the
PostgreSQL flexible Server were NOT in the proven set; structural verify only
checks a resource EXISTS at its type+name, so their bodies are deliberately
minimal placeholders (location plus the smallest structurally-plausible shape),
documented as such, and lean on floci-az's demonstrated leniency (it
synthesized a full default-rule set from an empty NSG ``securityRules`` array,
i.e. it is additive, not strict).

**None, not raise, for an unmapped token.** A token this table does not cover
returns ``None``; the materializer logs and skips it, and the un-PUT resource
surfaces as ``missing`` at verify - the honest signal, not a crash. A hostile
NAME (one shaped to break out of the URL path) is a different matter: it raises
``InvalidAzureNameError``, because it is never a resource we can safely
materialize, and silently skipping it would hide an attack (or a translator
bug) rather than surface it. Token membership is checked before any name
validation, so a hostile name on an unmapped token still just returns ``None``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_DEFAULT_LOCATION = "eastus"

# Conservative Azure resource-name shape whose SOLE job here is URL-path safety,
# not enforcing Azure's per-type business rules (storage's 3-24 lowercase, etc.
# - the translator's normalization owns those; this is a strict superset that
# never wrongly rejects one). It BLOCKS every character that could break out of
# a path segment: '/' and '\\' (traversal / new segment), '%' (percent-encoding
# tricks), whitespace and control chars, and query/fragment punctuation
# ('?', '#', '@', ';', ...). Requiring an alphanumeric FIRST character also
# rejects a leading-dot name (so a bare ".." or ".hidden" cannot start a
# segment); genuine traversal additionally needs a '/', which is already
# blocked. Length caps at 90 (1 + 89), Azure's general resource-name ceiling.
_AZURE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,89}$")

# Every token this table can turn into an ARM PUT. Kept as an explicit set (not
# derived from the dispatch) so a test can pin it and so a caller can cheaply
# ask "will this be skipped?" without building the PUT.
SUPPORTED_TOKENS: frozenset[str] = frozenset(
    {
        "azure_native.resources.ResourceGroup",
        "azure_native.storage.StorageAccount",
        "azure_native.network.VirtualNetwork",
        "azure_native.network.Subnet",
        "azure_native.network.NetworkSecurityGroup",
        "azure_native.compute.VirtualMachine",
        "azure_native.dbforpostgresql.Server",
    }
)

_SUBNET_SUFFIX = "-subnet"
_VNET_SUFFIX = "-vnet"


class InvalidAzureNameError(ValueError):
    """A name/resource-group/parent that would be unsafe or malformed inside an
    ARM URL path. A ``ValueError`` subclass so callers can catch either."""


@dataclass(frozen=True)
class ArmPut:
    """One ARM PUT: where, which API version, and the request body.

    ``path`` is the ARM URL path AFTER the subscription - the caller
    (materializer) prepends ``subscriptions/{sub}/`` and the floci-az base URL,
    and appends ``?api-version={api_version}``. A resource group's path is
    subscription-level (``resourcegroups/{name}``); every other resource nests
    under ``resourceGroups/{rg}/providers/...``.
    """

    path: str
    api_version: str
    body: dict[str, Any]


def azure_native_to_arm_put(
    token: str,
    name: str,
    resource_group: str,
    properties: dict[str, Any],
) -> ArmPut | None:
    """Map one azure-native token to its ARM PUT, or ``None`` if unmapped.

    ``name`` must be the translator's ``azure_logical_name`` for the resource
    (it becomes the final path segment and is what verify matches on).
    ``properties`` is the resource's translated property bag - only
    ``location`` (all types) and ``virtual_network_name`` (Subnet's parent) are
    read; everything else is ignored, because structural verify checks
    existence+type+name, not property values.

    Raises ``InvalidAzureNameError`` when ``name`` (or, for the types that use
    them, ``resource_group`` / the Subnet's parent vnet) is not a path-safe
    Azure name - but ONLY for a mapped token; an unmapped token returns
    ``None`` before any validation.
    """
    if token not in SUPPORTED_TOKENS:
        return None

    location = _location(properties)
    safe_name = _validate_name(name, "resource name")

    match token:
        case "azure_native.resources.ResourceGroup":
            # Subscription-level: the RG's own name IS the container; it does
            # not nest under a resource_group (that argument is unused here).
            return ArmPut(
                path=f"resourcegroups/{safe_name}",
                api_version="2021-04-01",
                body={"location": location},
            )
        case "azure_native.storage.StorageAccount":
            return ArmPut(
                path=_provider_path(resource_group, "Microsoft.Storage/storageAccounts", safe_name),
                api_version="2023-01-01",
                body={
                    "location": location,
                    "sku": {"name": "Standard_LRS"},
                    "kind": "StorageV2",
                },
            )
        case "azure_native.network.VirtualNetwork":
            return ArmPut(
                path=_provider_path(resource_group, "Microsoft.Network/virtualNetworks", safe_name),
                api_version="2023-04-01",
                body={
                    "location": location,
                    "properties": {"addressSpace": {"addressPrefixes": ["10.0.0.0/16"]}},
                },
            )
        case "azure_native.network.Subnet":
            safe_rg = _validate_name(resource_group, "resource group")
            parent = _validate_name(
                _parent_vnet_name(safe_name, properties), "parent virtual network"
            )
            return ArmPut(
                path=(
                    f"resourceGroups/{safe_rg}/providers/Microsoft.Network/"
                    f"virtualNetworks/{parent}/subnets/{safe_name}"
                ),
                api_version="2023-04-01",
                body={"properties": {"addressPrefix": "10.0.1.0/24"}},
            )
        case "azure_native.network.NetworkSecurityGroup":
            return ArmPut(
                path=_provider_path(
                    resource_group, "Microsoft.Network/networkSecurityGroups", safe_name
                ),
                api_version="2023-04-01",
                body={"location": location, "properties": {"securityRules": []}},
            )
        case "azure_native.compute.VirtualMachine":
            # NOT spike-proven. Minimal placeholder body - structural verify
            # only needs the VM to exist at its type+name. hardwareProfile is a
            # nod to VM shape, not a real sizing decision.
            return ArmPut(
                path=_provider_path(resource_group, "Microsoft.Compute/virtualMachines", safe_name),
                api_version="2023-07-01",
                body={
                    "location": location,
                    "properties": {"hardwareProfile": {"vmSize": "Standard_B1s"}},
                },
            )
        case "azure_native.dbforpostgresql.Server":
            # Flexible Server API (single-server is retired). Body shape proven
            # LIVE against floci-az (2026-08-31): it returns 400 without
            # administratorLogin, so administratorLogin / administratorLoginPassword
            # / version / storage are ALL REQUIRED. These are throwaway
            # local-emulator placeholders — floci-az needs no real credential and
            # this never touches real Azure.
            #
            # ASYNC CREATE — returns 202, NOT the synchronous 200/201 every other
            # type in this table returns. Task 3's materializer MUST poll this
            # resource to provisioningState=Succeeded before verify reads it, or
            # verify races a still-provisioning server and reports it missing.
            return ArmPut(
                path=_provider_path(
                    resource_group,
                    "Microsoft.DBforPostgreSQL/flexibleServers",
                    safe_name,
                ),
                api_version="2022-12-01",
                body={
                    "location": location,
                    "sku": {"name": "Standard_B1ms", "tier": "Burstable"},
                    "properties": {
                        "administratorLogin": "pgadmin",
                        "administratorLoginPassword": "Placeholder1!",
                        "version": "16",
                        "storage": {"storageSizeGB": 32},
                    },
                },
            )

    # Unreachable: SUPPORTED_TOKENS and the match arms are pinned equal by
    # test_every_supported_token_produces_a_put. A token added to the set but
    # not the match lands here and fails loudly rather than silently skipping.
    raise AssertionError(  # pragma: no cover
        f"{token} is in SUPPORTED_TOKENS but has no ARM PUT arm"
    )


def _provider_path(resource_group: str, provider_type: str, name: str) -> str:
    """The ``resourceGroups/{rg}/providers/{provider_type}/{name}`` path for a
    resource-group-scoped resource, with ``resource_group`` validated for
    path safety (it lands in the URL just as ``name`` does)."""
    safe_rg = _validate_name(resource_group, "resource group")
    return f"resourceGroups/{safe_rg}/providers/{provider_type}/{name}"


def _parent_vnet_name(subnet_name: str, properties: dict[str, Any]) -> str:
    """The name of the VNet a subnet nests under.

    Primary source: ``properties["virtual_network_name"]`` - the translator
    ALWAYS emits it for a Subnet (subnet-to-subnet.yaml carries it as a
    required ``from_translated`` property), so this is the real, common case.

    Fallback (documented limitation): a Subnet whose properties carry no
    ``virtual_network_name`` is already a translator gap. Rather than crash,
    derive a deterministic parent from the translator's own naming convention -
    a subnet is named ``${stem}-subnet`` and its VNet ``${stem}-vnet`` from the
    same source stem - by stripping a trailing ``-subnet`` and reattaching
    ``-vnet``. This is best-effort: it will be RIGHT when both names came from
    the same stem, and at worst PUTs the subnet under a deterministically-named
    (possibly-nonexistent) parent, which surfaces honestly at verify rather
    than as a materialization crash.
    """
    explicit = properties.get("virtual_network_name")
    if isinstance(explicit, str) and explicit:
        return explicit
    stem = (
        subnet_name[: -len(_SUBNET_SUFFIX)] if subnet_name.endswith(_SUBNET_SUFFIX) else subnet_name
    )
    return f"{stem}{_VNET_SUFFIX}"


def _location(properties: dict[str, Any]) -> str:
    """The resource's Azure location: ``properties["location"]`` when it is a
    non-empty string, else the default (``eastus``)."""
    location = properties.get("location")
    if isinstance(location, str) and location:
        return location
    return _DEFAULT_LOCATION


def _validate_name(value: str, role: str) -> str:
    """Return ``value`` unchanged if it is a path-safe Azure name, else raise.

    ``role`` names which argument failed, for a message a materializer log (or
    a security triage) can act on. The check is a whitelist match against
    ``_AZURE_NAME_PATTERN`` - see its comment for exactly what is blocked and
    why a path-traversal name cannot pass.
    """
    if not isinstance(value, str) or not _AZURE_NAME_PATTERN.match(value):
        raise InvalidAzureNameError(
            f"{role} {value!r} is not a path-safe Azure name "
            f"(must match {_AZURE_NAME_PATTERN.pattern})"
        )
    return value
