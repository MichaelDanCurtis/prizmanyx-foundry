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

from foundry.loop import Decision, LoopReport, StepResult

REPORT_MD_FILENAME = "report.md"
REPORT_JSON_FILENAME = "report.json"

# The spec's As-decided fidelity trade (`docs/superpowers/specs/
# 2026-08-30-floci-cloud-testing-design.md`'s Track A/Track B split), carried
# verbatim into every report header so reading ONE report.md is enough to
# know what deploy-preview does and does not prove - never left implicit,
# never only in a spec a reader of this file may not have open.
FIDELITY_LINE = (
    "deploy-preview exercises Prizmanyx's own Pulumi deployer; "
    "apply-beyond-preview against emulated Azure is not part of slice 3 "
    "(awaits the ARM adapter, Track B)."
)


def _format_duration(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds:.2f}s"


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|") if text else ""


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
