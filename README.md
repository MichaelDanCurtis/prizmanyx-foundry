# prizmanyx-foundry

A standalone tool that manufactures disposable [Floci](https://floci.io)-backed
AWS (and, later, Azure) test estates and drives
[Prizmanyx](https://github.com/MichaelDanCurtis/Prizmanyx)'s pipeline against
them from the outside, as a scripted operator, reporting a PASS/FAIL verdict
per run.

## The three boundaries

These are load-bearing and apply to every line of code added to this repo:

1. **This is a standalone tool. It is NOT part of the Prizmanyx application.**
   It lives in its own repository, ships its own CLI, and is versioned and
   released independently of Prizmanyx.
2. **It talks to Prizmanyx only through its public API, as an operator
   client.** This repo never imports Prizmanyx code and never touches
   Prizmanyx's database directly. Every pipeline step the loop driver walks
   (extract, ingest, discover, catalog, approve, translate, export, deploy
   preview, verify) goes through the same HTTP API the operator UI uses —
   nothing here is a backdoor or a test-mode flag grown inside Prizmanyx
   itself.
3. **Floci containers get the docker-socket mount
   (`-v /var/run/docker.sock:/var/run/docker.sock`), and that is
   host-root-equivalent access — local dev machines only, never shared
   hosts.** Floci's real-engine emulation (RDS, Lambda, ElastiCache, and EC2
   all spin up their own backing containers) shells out to the Docker daemon
   to do it, and that requires handing the Floci container the socket. Anyone
   with a live Docker socket can start, stop, or inspect any container on the
   host and mount arbitrary host paths into a new one — it is effectively
   root on the machine running the daemon. This is an accepted, deliberate
   trade-off for a throwaway container on a developer's own machine, for the
   duration of a single environment's life. It is never acceptable on a
   shared host or CI runner, and this tool does not attempt to make it safe
   there.

## Status

Slices 1–3 are complete: the environment engine, EnvSpec (hand-written and
seeded-random), both materializers (spec and from-repo), the lifecycle
commands, the Prizmanyx pipeline client, and the loop driver (`foundry run`)
that walks the full extract → discover → gate → translate → export →
deploy-preview pipeline against a running Prizmanyx and returns a PASS/FAIL
verdict. A live acceptance run against the dev backend passed end to end —
see `docs/ACCEPTANCE.md` (and the four real integration findings it caught).
The deploy+verify leg against emulated **Azure** is slice 4 and awaits the
ARM adapter (`docs/superpowers/specs/2026-08-30-floci-cloud-testing-design.md`
in the Prizmanyx repo, the *As decided* note); a local web UI is slice 5.
`docs/DECISIONS.md` carries the probe results and design calls the whole tool
builds on.

```bash
uv sync

uv run foundry version

# Create an environment and materialize an estate in it — one of three sources:
uv run foundry create demo --spec examples/specs/small.yaml
uv run foundry create demo --random S --seed 42
uv run foundry create demo \
  --from-repo https://github.com/MichaelDanCurtis/prizmanyx-foundry \
  --path examples/aws-mini

uv run foundry ls          # every tracked environment, with LIVE container status

# Drive Prizmanyx's pipeline against the environment and get a verdict:
export FOUNDRY_PRIZMANYX_USER=admin FOUNDRY_PRIZMANYX_PASSWORD=prizmanyx-dev
uv run foundry run demo --prizmanyx http://localhost:8000
# → PASS/FAIL, with a per-step + per-decision report written into the env dir.

uv run foundry rm demo     # container, attributable children, state dir
```

`foundry run` is a scripted operator: it logs in, registers the environment
as an AWS connection (at the **worker-reachable** endpoint —
`host.docker.internal:<port>`, since Prizmanyx's extraction runs in a
container, overridable with `--worker-host`), and walks every human gate with
a recorded default (accept-catalog-defaults, approve, rehost, the four
sign-offs). The estate it materializes carries an `Application` tag on every
resource because Prizmanyx's deterministic unit inference is tag-seeded — an
untagged estate yields zero migration units (`docs/DECISIONS.md` entry 004).
The report is the honesty surface: a failure names the first failing step
verbatim and marks every later step `UNRUN`, never faked.

`--from-repo` clones any https git repository and applies **its** terraform
at **this** environment's floci endpoints, by injecting an `override.tf`
carrying the same provider/endpoints block a spec-based environment gets.
`examples/aws-mini` is this repo's own acceptance fixture for that path —
see `docs/DECISIONS.md` entry 002 for why the acceptance case is
self-referential today, and for the two override-semantics limits it
inherits.

Terraform and git are invoked as **external tools** — the terraform CLI runs
in a pinned throwaway container (`hashicorp/terraform:1.9`) and is never
vendored or shipped by this repo. That matters: the terraform CLI is
BUSL-1.1, and only invocation, not distribution, is in scope here.

## Design provenance

This tool is Track A of the Floci cloud-testing design
(`docs/superpowers/specs/2026-08-30-floci-cloud-testing-design.md` in the
Prizmanyx repo). Track B — swapping Prizmanyx's own test suites from
LocalStack to Floci — lives inside the Prizmanyx repo itself and is a
separate, unrelated piece of work.
