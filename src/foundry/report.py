"""Render a `LoopReport` as `report.md` + `report.json`, and read them back.

`report.json` is pydantic's own `model_dump_json`/`model_validate_json` -
the exact `state.save_state`/`load_state` pattern, so a report round-trips
byte-for-byte in every field. `report.md` is a human-readable rendering with
no independent source of truth: every line comes straight off the same
`LoopReport`, so the two files can never silently disagree about the
verdict, a step's status, or a decision.
"""

from __future__ import annotations

from pathlib import Path

from foundry.loop import Decision, DeploymentResult, LoopReport, StepResult, VerificationResult

REPORT_MD_FILENAME = "report.md"
REPORT_JSON_FILENAME = "report.json"

# The spec's As-decided fidelity trade (`docs/superpowers/specs/
# 2026-08-30-floci-cloud-testing-design.md`'s Track A/Track B split), carried
# verbatim into every report header so reading ONE report.md is enough to
# know what each of `deploy_preview`/`deploy_apply`/`verify` does and does
# not prove - never left implicit, never only in a spec a reader of this
# file may not have open. Rewritten for slice 4 (Task 5): `deploy_preview`
# still only exercises Prizmanyx's own Pulumi deployer against emulated
# Azure - it never applies. `deploy_apply` is the foundry's OWN ARM
# materializer (`arm_materialize.apply_expected_resources`), not Pulumi's
# apply - a deliberate fidelity trade, stated rather than implied. `verify`
# is Prizmanyx's REAL ARM bridge (`PrizmanyxClient.verify_translation`), but
# STRUCTURAL only - existence, type, and name, not property-depth (that is
# Phase 6, not this slice).
FIDELITY_LINE = (
    "deploy_preview exercises Prizmanyx's own Pulumi deployer (never applies); "
    "deploy_apply is the foundry's own ARM materializer into emulated Azure, "
    "NOT Pulumi's apply; verify is Prizmanyx's real ARM bridge, but STRUCTURAL "
    "only - existence/type/name, not property-depth (Phase 6)."
)


def _format_duration(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds:.2f}s"


def _escape_cell(text: str) -> str:
    """Make `text` safe to sit inside one Markdown table cell.

    A raw embedded newline splits a `| ... |` row across two physical
    lines, corrupting every row rendered after it - a multi-line
    `failure_detail` (a stack trace, a multi-line server error) is exactly
    the kind of text this needs to survive. `\\r\\n` is folded before the
    lone `\\n`/`\\r` replacements so a single Windows line ending never
    produces two markers. This only affects the Markdown rendering -
    `report.json` carries the original text untouched (`LoopReport.
    model_dump_json`, never routed through this function), so nothing here
    trades fidelity for cosmetics.
    """
    if not text:
        return ""
    escaped = text.replace("|", "\\|")
    return escaped.replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")


def _render_step_row(step: StepResult) -> str:
    return (
        f"| {step.name} | {step.status} | {_format_duration(step.duration_seconds)} "
        f"| {_escape_cell(step.detail)} |"
    )


def _render_decision_row(decision: Decision) -> str:
    return (
        f"| {decision.step} | {decision.choice} | {decision.why} "
        f"| {_escape_cell(decision.detail)} |"
    )


def _render_deployment_row(deployment: DeploymentResult) -> str:
    return (
        f"| {deployment.translation_id} | {deployment.resource_group} | {deployment.ok} "
        f"| {deployment.applied} | {len(deployment.skipped_unmapped)} "
        f"| {len(deployment.failures)} | {len(deployment.provisioning_timeouts)} |"
    )


def _render_verification_row(verification: VerificationResult) -> str:
    return (
        f"| {verification.translation_id} | {verification.resource_group} "
        f"| {verification.all_matched} | {verification.matched_count} "
        f"| {verification.mismatch_count} | {verification.missing_count} "
        f"| {verification.extra_count} |"
    )


def render_markdown(report: LoopReport) -> str:
    """A self-contained Markdown rendering of `report` - everything a human
    needs to judge one run without opening report.json."""
    lines = [
        f"# Foundry loop report — {report.env_name}",
        "",
        f"> {FIDELITY_LINE}",
        "",
        f"**Verdict: {report.verdict}**",
        "",
    ]
    if report.verdict == "FAIL":
        lines += [f"Failed at step **{report.failed_step}**: {report.failure_detail}", ""]
    lines += [
        f"- Program id: {report.program_id}",
        f"- Graph run id: {report.graph_run_id}",
        f"- Worker-reachable AWS endpoint: {report.worker_endpoint_url} (what "
        "Prizmanyx's extraction worker container connects to - the foundry's own "
        "host-side verification calls correctly use localhost instead, not this)",
        f"- Migration unit ids: {', '.join(str(i) for i in report.unit_ids) or '(none)'}",
        f"- Translation ids: {', '.join(str(i) for i in report.translation_ids) or '(none)'}",
        f"- Started: {report.started_at.isoformat()}",
        f"- Finished: {report.finished_at.isoformat()}",
        "",
        "## Steps",
        "",
        "| Step | Status | Duration | Detail |",
        "|---|---|---|---|",
        *(_render_step_row(step) for step in report.steps),
        "",
        "## Decisions",
        "",
        "Every gate below was walked by the foundry itself, as a scripted",
        "operator standing in for a human reviewer - recorded verbatim so",
        "this report is the honesty surface for what was actually decided,",
        "and by what default.",
        "",
        "| Step | Choice | Why | Detail |",
        "|---|---|---|---|",
        *(_render_decision_row(decision) for decision in report.decisions),
        "",
        "## Artifacts",
        "",
        *([f"- {path}" for path in report.artifact_paths] or ["(none)"]),
        "",
        "## Deployments (deploy_apply)",
        "",
        "Each row is one translation's `arm_materialize.apply_expected_resources`",
        "outcome - `ok` true iff every MAPPED resource reached a terminal success.",
        "`skipped_unmapped` is a coverage gap (surfaces as `missing` at verify",
        "below), not a failure of the apply itself.",
        "",
        "| Translation | Resource group | OK | Applied | Skipped (unmapped) "
        "| Failures | Provisioning timeouts |",
        "|---|---|---|---|---|---|---|",
        *(_render_deployment_row(deployment) for deployment in report.deployment_results),
        "",
        "## Verification (verify)",
        "",
        "Each row is Prizmanyx's own `verify_translation` verdict, recorded",
        "verbatim - STRUCTURAL only (existence/type/name, not property-depth).",
        "",
        "| Translation | Resource group | All matched | Matched | Mismatch | Missing | Extra |",
        "|---|---|---|---|---|---|---|",
        *(_render_verification_row(verification) for verification in report.verification_results),
        "",
    ]
    return "\n".join(lines)


def write_report(report: LoopReport, directory: Path) -> tuple[Path, Path]:
    """Write `report` as both `report.md` and `report.json` under
    `directory` (created if missing), and return `(md_path, json_path)`."""
    directory.mkdir(parents=True, exist_ok=True)
    md_path = directory / REPORT_MD_FILENAME
    json_path = directory / REPORT_JSON_FILENAME
    md_path.write_text(render_markdown(report))
    json_path.write_text(report.model_dump_json(indent=2))
    return md_path, json_path


def load_report(directory: Path) -> LoopReport:
    """Read back the `report.json` a prior `write_report` wrote under `directory`."""
    return LoopReport.model_validate_json((directory / REPORT_JSON_FILENAME).read_text())
