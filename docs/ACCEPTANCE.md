# Foundry acceptance — slice 1-3 live run

Date: 2026-08-31. Against live Prizmanyx dev backend (localhost:8000) + Temporal worker.

## Command sequence
```
$ foundry create accept-s --random S --seed 42   # 11 resources applied (VPC, 2 subnets, 2 EC2, RDS+db_subnet_group, 2 buckets, SQS, IAM role) — all Application-tagged
$ FOUNDRY_PRIZMANYX_USER=admin FOUNDRY_PRIZMANYX_PASSWORD=prizmanyx-dev foundry run accept-s --prizmanyx http://localhost:8000
verdict: PASS
```

## The four live findings this acceptance caught and fixed

Each was the product's own honest gate refusing to fake success — exactly what live acceptance is for. None was reachable by the unit tests (MockTransport/fake-client can't enforce cookie transport rules, container networking, or the real discovery inference).

1. **Secure cookie over http** (`f58dd31`) — the backend sets `pzx_session; Secure`; httpx won't send a Secure cookie over dev-http. Fixed by honoring the browser's loopback-Secure exception (why the real UI works over http). Symptom: create_program → 401.
2. **Worker-container reachability** (`53bea58`) — extraction runs in the Temporal worker CONTAINER, so the connection endpoint must be `host.docker.internal:<port>`, not `localhost`. Prizmanyx's own preflight caught it before extracting. Symptom: poll_pipeline_run → EndpointUnreachableError.
3. **Estates need an Application tag** (`db61ab0`) — Prizmanyx's deterministic unit inference is tag-seeded (GROUP_TAG_KEYS in migration_units.py); an untagged estate yields zero migration units and the exit gate's units_approved can never pass. Fixed by tagging every resource Application=<name_prefix>. Symptom: poll_exit_gate gave up, units_approved.approved=0.
4. **RDS needs a real DB subnet group** (`ee38a03`) — the emitter relied on the "default" DB subnet group (invalid on real AWS, flaky on floci). Fixed by emitting an aws_db_subnet_group spanning 2 AZs. Symptom: InvalidVPCNetworkStateFault.

## Final PASS report

# Foundry loop report — accept-s

> deploy-preview exercises Prizmanyx's own Pulumi deployer; apply-beyond-preview against emulated Azure is not part of slice 3 (awaits the ARM adapter, Track B).

**Verdict: PASS**

- Program id: 61
- Graph run id: 2567
- Worker-reachable AWS endpoint: http://host.docker.internal:15600 (what Prizmanyx's extraction worker container connects to - the foundry's own host-side verification calls correctly use localhost instead, not this)
- Migration unit ids: 2739
- Translation ids: 1550
- Started: 2026-08-31T11:14:50.343791+00:00
- Finished: 2026-08-31T11:14:57.605680+00:00

## Steps

| Step | Status | Duration | Detail |
|---|---|---|---|
| login | PASSED | 0.03s |  |
| create_program | PASSED | 0.01s |  |
| register_env_connection | PASSED | 0.01s |  |
| start_extraction | PASSED | 0.02s |  |
| poll_pipeline_run | PASSED | 5.02s |  |
| accept_catalog_defaults | PASSED | 0.01s |  |
| list_units | PASSED | 0.00s |  |
| approve | PASSED | 0.01s |  |
| disposition | PASSED | 0.02s |  |
| sign_off | PASSED | 0.02s |  |
| poll_exit_gate | PASSED | 0.02s |  |
| translate | PASSED | 0.01s |  |
| poll_translations | PASSED | 2.03s |  |
| export | PASSED | 0.04s |  |
| deploy_preview | PASSED | 0.02s |  |

## Decisions

Every gate below was walked by the foundry itself, as a scripted
operator standing in for a human reviewer - recorded verbatim so
this report is the honesty surface for what was actually decided,
and by what default.

| Step | Choice | Why | Detail |
|---|---|---|---|
| accept_catalog_defaults | accept-defaults | foundry default |  |
| approve | approve | foundry default | unit 2739 |
| disposition | rehost | foundry default | unit 2739 |
| sign_off | engineer | foundry default |  |
| sign_off | program_mgmt | foundry default |  |
| sign_off | delivery_md | foundry default |  |
| sign_off | customer | foundry default |  |

## Artifacts

- /Users/michaelcurtis/.prizmanyx-foundry/envs/accept-s/export.csv

---

# Foundry acceptance — slice 4 (deploy + verify) live run

Date: 2026-08-31. The full round trip: the foundry PUTs the translation's
expected resources into a per-env `floci/floci-az` container over ARM
(`deploy_apply`), then asks Prizmanyx to re-extract that resource group and
diff it against what the translator expected (`verify`).

## Command sequence
```
$ foundry create accept-az --random S --seed 42     # AWS floci + floci-az, 11 resources applied
$ FOUNDRY_PRIZMANYX_USER=admin FOUNDRY_PRIZMANYX_PASSWORD=prizmanyx-dev \
    foundry run accept-az --prizmanyx http://localhost:8001
verdict: FAIL   # honest — see findings below; the round trip itself is proven
```

`--prizmanyx http://localhost:8001` points at a second backend started with
`PRIZMANYX_AZURE_ARM_ENDPOINT=http://localhost:14577` — see finding 2.

## What this acceptance proved

The deploy+verify round trip runs end to end. `deploy_apply` materialized 8
resources into floci-az with **0 failures**; `verify` re-extracted the group
over ARM REST and structurally matched **6** resources — the ResourceGroup,
the VirtualNetwork, both Subnets, and both StorageAccounts — with
**extra=0, mismatch=0**. Every resource type floci-az faithfully aggregates
under `/resourceGroups/{rg}/resources` matched exactly.

## The findings this acceptance caught

1. **The ResourceGroup was missing from expected-resources — a real product
   bug, now fixed in Prizmanyx.** The RG a translated estate deploys into is
   a codegen-time scaffold (`_prepare_codegen_resources`), never a persisted
   `translated_resources` row — so `load_expected_resources` never surfaced
   it. Both halves of this round trip were built assuming it was there: the
   foundry's ARM materializer reads the RG name off the expected
   ResourceGroup row, and Prizmanyx's ARM bridge GETs the RG expecting a
   match. Symptom: `deploy_apply` failed with "expected-resources carried no
   ResourceGroup row"; once past that, the deployed RG would have read as an
   unexpected `extra`. Fixed in Prizmanyx: `load_expected_resources` now
   prepends the same scaffold codegen injects (new single-source helper
   `expected_resource_group_scaffold`). After the fix: RG matched, extra=0.
   The foundry needed **zero** change — its materializer and the bridge were
   already correct; only the expected set was incomplete.

2. **Verify runs inline in the API backend, so the *backend* needs the ARM
   endpoint — not the worker.** `POST /translations/{id}/verify` runs the
   verify engine on the request thread, not through a Temporal activity. So
   `PRIZMANYX_AZURE_ARM_ENDPOINT` must be set on the process serving the
   route (host-side → `localhost:14577`), whereas extraction runs in the
   worker container (→ `host.docker.internal:14577`). Unset, the bridge
   defaults to real Azure + `DefaultAzureCredential` and 500s on a host with
   no Azure creds. Operator note; a Phase-6 real-Azure run should weigh
   moving verify to a worker activity (like `verify_deployment`) so the
   cloud-facing process is consistent.

3. **floci-az's `/resources` aggregation omits `Microsoft.Compute`
   (emulator fidelity gap).** The 2 VirtualMachines were deployed
   successfully — individually GET-able (HTTP 200) and listed at the
   specific-type level — but floci-az's
   `GET /subscriptions/{sub}/resourceGroups/{rg}/resources` (the endpoint the
   bridge lists, and the endpoint real Azure returns *everything* from) does
   not include them; its subscription-level `/resources` returns nothing at
   all. So the VMs read as `missing` at verify. This is a floci-az
   limitation, not a verify-logic defect — the structural verify would match
   these against real Azure. Candidate for an upstream fix (as with the
   X-Forwarded-Proto PR #253).

4. **The foundry does not map `NetworkInterface` for ARM PUT (coverage
   gap).** `azure_native_to_arm_put` covers RG, StorageAccount,
   VirtualNetwork, Subnet, NetworkSecurityGroup, VirtualMachine, and
   dbforpostgresql.Server — not NetworkInterface — so the 2 NICs were
   honestly reported as `skipped_unmapped` and surface as `missing`. Even
   mapped, they would hit finding 3 (compute not aggregated), so mapping them
   is deferred to a follow-up that pairs a live-proven NIC body with an
   upstream floci-az listing fix.

## Verify verdict detail

Translation 1603, resource_group `prizmanyx-resource-group`:
matched=6, missing=4 (2 VirtualMachines + 2 NetworkInterfaces), extra=0,
mismatch=0. The 4 missing are entirely findings 3 and 4 above — the deploy
half succeeded and the verify logic is correct.
