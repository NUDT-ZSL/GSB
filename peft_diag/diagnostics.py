"""Human-readable diagnostic dashboard rendering."""

from __future__ import annotations

from typing import Any


def _status_line(status: dict[str, Any]) -> str:
    checkpoint = status.get("checkpoint", {})
    latest = checkpoint.get("latest")
    if not latest:
        return "no committed checkpoint"
    return (
        f"v{latest['version']} {latest['stage']} "
        f"(cycle {latest.get('cycle', 0)}, checksum {latest['checksum'][:12]})"
    )


def dashboard_text(status: dict[str, Any]) -> str:
    report = status.get("report") or {}
    checkpoint = status.get("checkpoint", {})
    cache = status.get("cache", {})
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("PEFT Snapshot Diagnostics")
    lines.append("=" * 72)
    lines.append(f"run id       : {status.get('run_id')}")
    lines.append(f"checkpoint   : {_status_line(status)}")
    lines.append(f"commits      : {checkpoint.get('commit_versions', [])}")
    lines.append(f"train status : {report.get('status', 'not-started')}")
    if report.get("status_detail"):
        lines.append(f"detail       : {report['status_detail']}")
    lines.append("")
    lines.append("Stage state machine")
    lines.append("  INITIALIZE -> GRAD_ACCUM -> OPTIMIZER_UPDATE -> EXPORT")
    lines.append("  checkpoint commit: only after every task completes the current stage")
    lines.append("  crash recovery   : resume at the latest committed stage; discard partial work")
    lines.append("")
    task_states = status.get("task_states_at_commit", {})
    lines.append("Task state at latest commit")
    if task_states:
        for task_id, state in sorted(task_states.items()):
            lines.append(
                f"  {task_id:<12} step={state.get('step')} "
                f"optimizer_step={state.get('update_count')} "
                f"micro_in_window={state.get('micro_in_window')}"
            )
    else:
        lines.append("  (no committed task state)")
    if report.get("task_stage_stats"):
        lines.append("")
        lines.append("Per-task cache activity")
        for task_id, stages in sorted(report["task_stage_stats"].items()):
            parts = [
                f"{stage}:{counts['cache_hits']}h/{counts['computed']}c"
                for stage, counts in sorted(stages.items())
            ]
            lines.append(f"  {task_id:<12} {' '.join(parts)}")
    lines.append("")
    lines.append(
        "Cache"
        f"              entries={cache.get('entries')} hits={cache.get('hits')} "
        f"misses={cache.get('misses')} evictions={cache.get('evictions')}"
    )
    history = status.get("revision_history", [])
    lines.append("")
    lines.append("Invalidation propagation")
    if not history:
        lines.append("  (no revision transactions)")
    else:
        latest_revision = history[-1]
        lines.append(
            f"  generation {latest_revision.get('config_generation')} affected "
            f"{latest_revision.get('affected_tasks')}"
        )
        for task_id, impact in sorted(latest_revision.get("impact", {}).items()):
            lines.append(
                f"  {task_id:<12} fields={impact.get('changed_fields')} "
                f"stages={impact.get('affected_stages')}"
            )
    lines.append("")
    lines.append("Recovery")
    notes = checkpoint.get("recovery_notes") or report.get("recovery_notes") or []
    if notes:
        for note in notes:
            lines.append(f"  {note}")
    else:
        lines.append("  latest commit is valid; no skipped or orphaned commit observed")
    errors = report.get("error")
    lines.append("")
    lines.append("Business error")
    if errors:
        lines.append(
            f"  {errors.get('code')} task={errors.get('task_id')} field={errors.get('field')}: "
            f"{errors.get('message')}"
        )
    else:
        lines.append("  (none)")
    degradation = report.get("degradation", [])
    if degradation:
        lines.append("")
        lines.append("Resource degradation")
        for entry in degradation:
            lines.append(
                f"  priority={entry.get('priority')} {entry.get('action')} "
                f"task={entry.get('task_id')} reason={entry.get('reason')}"
            )
    lines.append("=" * 72)
    return "\n".join(lines)


__all__ = ["dashboard_text"]

