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
