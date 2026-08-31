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

Repo bootstrap only. The CLI currently exposes one command:

```bash
uv sync
uv run foundry version
```

The environment engine, EnvSpec, materializers, the Prizmanyx pipeline
client, and the loop driver are later slices — see
`docs/DECISIONS.md` for the probe result they build on, and the plan this
repo is being built from (tracked in the Prizmanyx repo's
`docs/superpowers/plans/2026-08-30-foundry-slices-1-3.md`) for the full slice
breakdown.

## Design provenance

This tool is Track A of the Floci cloud-testing design
(`docs/superpowers/specs/2026-08-30-floci-cloud-testing-design.md` in the
Prizmanyx repo). Track B — swapping Prizmanyx's own test suites from
LocalStack to Floci — lives inside the Prizmanyx repo itself and is a
separate, unrelated piece of work.
