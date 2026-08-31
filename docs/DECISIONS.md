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
writes a non-null `endpoint_url`). The spike never exercised that SDK path:
it listed the path under its own "Not attempted (for controller
adjudication)" section, flagged for a follow-up task, having only READ
`shapers.py` rather than run it. That left it the swing fact for this
foundry's loop design (Task 6). This entry closes that gap.

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

---

## 002 — from-repo acceptance runs against the foundry's OWN examples/aws-mini, not prizmanyx-testbed

**Date:** 2026-08-31

**Question.** The Floci cloud-testing design
(`docs/superpowers/specs/2026-08-30-floci-cloud-testing-design.md:130-135`,
Prizmanyx repo) describes `from-repo <https-url>` as the path by which
"`prizmanyx-testbed` becomes simply the first such repo". Task 5 needs a
LIVE acceptance case for that path. Is the testbed that case today?

**Method.** Checked the repository itself rather than the design's intent —
a shallow clone of `https://github.com/MichaelDanCurtis/prizmanyx-testbed`
(HEAD `b605b22cdfb5fa1dff5dae616d1e3a8a45279487`, "prizmanyx-testbed: an AWS
app built to be migrated", 2026-08-13), read-only, deleted afterward.

**Finding.** **Zero `.tf` files.** Its entire tracked file set is four `.py`,
one `.sh`, one `.md`, one `.gitignore`, and one `.yaml` — and that YAML,
`infra/core.yaml`, is a **CloudFormation** template (`AWSTemplateFormatVersion:
"2010-09-09"`, 114 lines), deliberately carrying planted declared-vs-live
mismatches for Prizmanyx's reconcile surface. This matches what the foundry
plan carried forward from the 2026-08-25 slice-2 live proof, and sharpens
it: the testbed does not merely *lack* terraform pending a small addition —
its IaC is written in a different language, so "a terraform mirror in the
testbed" is new authoring work, not a port of something already there.

**Decision.** Until such a mirror exists, the from-repo acceptance case is
**the foundry's own `examples/aws-mini`**, applied through the real path:
`foundry create <name> --from-repo
https://github.com/MichaelDanCurtis/prizmanyx-foundry --path
examples/aws-mini`. That fixture is 1 vpc + 1 subnet + 1 S3 bucket + 1 SQS
queue and — deliberately — declares **no provider block of its own**, which
is the exact shape the injected `override.tf` is designed for. The
acceptance test (`tests/test_from_repo.py`, `@pytest.mark.docker`) clones
the pushed repo over the network for real, applies it against a live floci
environment, and verifies the bucket and queue host-side via boto3.

This is a self-referential acceptance case, and that is worth naming
honestly: the repo proves it can apply *its own* terraform. What it does
NOT yet prove is that an arbitrary third-party repo's terraform applies
cleanly — that needs a repo nobody here authored. The mechanism under test
(clone → subtree select → override injection → the same `apply_in_dir` a
spec-based environment uses) is identical either way; the fixture's
provenance is the only thing this case cannot speak to.

**Two design facts this path depends on, recorded because they are not
obvious from the code alone:**

1. **Terraform's override semantics reach the DEFAULT provider only.**
   `override.tf` merges into `provider "aws" {}`; a cloned repo that also
   declares *aliased* aws providers (`provider "aws" { alias = "west" }`)
   keeps those pointed wherever the repo pointed them. A repo built that way
   would have part of its estate attempt real AWS endpoints. Not mitigated
   today — stated, not silently inherited.
2. **A repo carrying its own override file is refused, not merged.**
   Terraform reads `override.tf` and every `*_override.tf` in the working
   directory, applying them in filename order with the last winning. A repo
   shipping `override.tf` would have foundry's write clobber it; one
   shipping e.g. `zzz_override.tf` would silently out-precedence foundry's
   endpoint injection — pointing the apply somewhere other than this
   environment's floci. `from_repo._reject_conflicting_override_files`
   refuses both up front, by name.

**Also settled here (mirroring, not inventing):** the URL rule is Prizmanyx's
own clone-seam rule, both halves —
`packages/analysis-harness/prizmanyx_analysis_harness/clone.py`'s
`CloneRequest.__post_init__`, the check `convention_ingest.py`'s
`clone_scan_parse_terraform` relies on. https-only, with that file's exact
message text ("Only https git URLs are supported"), and no credentials
embedded in the URL — the second half matters here for the same literal
reason it does there, since this URL becomes `git clone <url>` argv. Clones
are transient (made in a temp dir inside the env's own 0o700 state
directory, removed on the way out, success or failure), which is the same
discipline `clone_scan_parse_terraform` applies to its own.
