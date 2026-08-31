# Decisions

A running log of load-bearing technical decisions and probe results for
prizmanyx-foundry, in the order they were reached. Entries are append-only —
a decision that changes later gets a new, superseding entry; nothing here is
rewritten in place.

---

## 001 — loop-driver EC2 coverage: CONFIRMED via SHAPERS path

**Date:** 2026-08-30

**Question.** The 2026-08-30 Floci spike (Prizmanyx repo,
`docs/superpowers/specs/2026-08-30-floci-spike-findings.md`, Leg 1) found
that `aws_ec2_instance` extraction fails against Floci through
`SteampipeExporter` — the table's hydrate functions
(`getEc2LaunchTemplateData`, and separately an unconditional
`state_transition_time` transform) are not implemented by Floci and abort
the whole row export, confirmed not mitigable by `select_columns`
narrowing. The spike also found that Prizmanyx's `Boto3Exporter`/`SHAPERS`
path (`packages/extractor/prizmanyx_extractor/shapers.py`) never calls
those steampipe hydrate functions at all, and that this is exactly the path
`build_extractor` (`packages/extractor/prizmanyx_extractor/factory.py:28-33`)
selects whenever `ExtractionRequest.endpoint_url` is set — which is exactly
how a foundry loop registers its environment (the
`/api/v1/programs/{id}/aws/localstack-connections` route,
`packages/api/prizmanyx_api/app.py:2568`,
`packages/api/prizmanyx_api/aws_connect_store.py`'s
`upsert_localstack_connection`, the only connection-creation path that ever
writes a non-null `endpoint_url`). The spike explicitly flagged this SDK
path as "not tested" and left it as the swing fact for this foundry's loop
design (Task 6). This entry closes that gap.

**Method.** From this repo (skeleton only at this point — no environment
engine exists yet), by hand:

1. Stood up a throwaway, digest-pinned Floci container, matching the spike's
   validated image and this plan's Global Constraint on the docker-socket
   mount:
   ```
   docker run -d --name foundry-probe-aws -p 15566:4566 \
     -v /var/run/docker.sock:/var/run/docker.sock \
     floci/floci@sha256:bec9b9f749322444a57fb0f00d3e007ffd49015162361aab84ccecd9b4f5f8ed
   ```
   Confirmed healthy via `docker logs` (`=== AWS Local Emulator Ready ===`,
   `ec2` listed among enabled services).
2. Seeded ONE EC2 instance via boto3, mirroring the spike's seed shape
   (VPC → subnet → instance, not the bare default-VPC path):
   `create_vpc` → `vpc-3b7e172b`, `create_subnet` → `subnet-d76336a6`,
   `run_instances(ImageId="ami-0c55b159cbfafe1f0", InstanceType="t3.micro",
   MinCount=1, MaxCount=1, SubnetId=subnet_id)` → `i-517dc81649deb33c3`.
   Confirmed real-container-backed, same as the spike found: `docker ps`
   showed `floci-ec2-i-517dc81649deb33c3` running
   `public.ecr.aws/amazonlinux/amazonlinux:2023`.
3. From the **Prizmanyx checkout**, read-only (`uv run python` from
   `packages/extractor`, no Prizmanyx files created, edited, or staged —
   verified with `git status` before and after, identical), invoked the
   **real production entry point**: `Boto3Exporter(storage=storage)` — the
   same class `build_extractor` returns, not `_shape_ec2_instance` called
   directly — with an `ExtractionRequest(account_id="000000000000",
   regions=("us-east-1",), resource_types=("aws_ec2_instance",),
   endpoint_url="http://localhost:15566", allow_partial=True)` and
   `aws_credentials={"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY":
   "test", "AWS_DEFAULT_REGION": "us-east-1"}` — the exact static creds
   shape `resolve_extraction_backend` injects for a LocalStack/dev
   connection (`packages/api/prizmanyx_api/temporal/activities.py`, per the
   spike findings doc's "production-path CARRY item" section).

**Transcript (verbatim from the probe run):**

```
endpoint_url = 'http://localhost:15566'
resource_types = ('aws_ec2_instance',)
account_id = '000000000000'
---
manifest.status = <ExtractionStatus.SUCCEEDED: 'succeeded'>
manifest.plugin_version = 'boto3:1.43.3'
manifest.errors = ()
artifact count = 1
  artifact: resource_type='aws_ec2_instance' record_count=1 uri='file:///.../foundry-probe/artifacts/snapshot-20260831T053836Z-cd907b72/000000000000/us-east-1/aws_ec2_instance.jsonl'
  ROW: {
  "arn": "arn:aws:ec2:us-east-1:000000000000:instance/i-517dc81649deb33c3",
  "image_id": "ami-0c55b159cbfafe1f0",
  "instance_id": "i-517dc81649deb33c3",
  "instance_type": "t3.micro",
  "private_ip_address": "172.17.0.3",
  "state": {
    "Code": 16,
    "Name": "running"
  },
  "subnet_id": "subnet-d76336a6",
  "tags": {},
  "vpc_id": "vpc-3b7e172b"
  ... (full row carries every describe_instances field snake_cased; trimmed
      here to the identity-bearing keys — the graph-relevant ones)
}
```

No `state_transition_time` key appears in the row at all — `shapers.py`'s
`_shape_ec2_instance` only calls `describe_instances` and snake_cases
whatever boto3 returns; it never calls `GetLaunchTemplateData` or asks for
the field steampipe's transform panics on. The steampipe-side failure has no
surface to reproduce against here.

**Verdict.** **CONFIRMED via SHAPERS path — the spike's EC2 exception is
steampipe-only and does not touch the foundry loop.** Any environment the
foundry's loop driver registers through the `localstack-connections` route
gets full `aws_ec2_instance` coverage: `resolve_extraction_backend` routes
it to `Boto3Exporter`/`SHAPERS`, which extracted the seeded instance
cleanly, on the first attempt, with zero errors and zero mitigation. Task
2's random generator does **not** need to omit EC2 instances from generated
specs — the loop-driver's actual extraction path already sees them.

This says nothing about real-AWS production fidelity (per the spike's own
"production-path CARRY item": a real customer connection never carries a
non-null `endpoint_url` and therefore never takes this branch — production
extraction stays on `SteampipeExporter`, EC2 gap and all, until Floci fixes
its transform panic upstream or Prizmanyx's own registry changes). It
answers exactly the question this foundry's loop needs answered: what the
loop's OWN extraction path — the one it actually runs — can see.

**Teardown.** `docker rm -f foundry-probe-aws`, then reaped the surviving
child (`floci-ec2-i-517dc81649deb33c3` — confirmed, as the spike found,
that it survives the parent's removal): `docker ps -a --filter
name=floci- -q | xargs docker rm -f`. Verified empty afterward (`docker ps
-a --filter name=floci-` — zero rows) and confirmed the untouchable compose
stack (`prizmanyx-localstack`, `-azurite`, `-postgres`, `-temporal*`,
`-minio`, `-grafana`, `-prometheus`) unaffected throughout.
