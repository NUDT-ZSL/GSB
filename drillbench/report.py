from __future__ import annotations

import html
import json
from typing import Any

from .models import (
    DEPENDENCY_SATISFIED_STAGES,
    FINAL_STAGES,
    OUTCOME_NOT_STARTED,
    STAGE_PENDING,
    STAGE_RUNNING,
)
from .store import StateStore


def build_status(store: StateStore, run_id: str) -> dict[str, Any]:
    bundle = store.get_run_bundle(run_id)
    run = bundle["run"]
    snapshot_info = bundle["snapshot_info"]
    snapshot = snapshot_info["snapshot"]
    plan = snapshot["plan"]
    steps_by_seq = {int(row["seq"]): row for row in bundle["steps"]}
    obs_by_step: dict[int, list[dict[str, Any]]] = {}
    for row in bundle["observations"]:
        obs_by_step.setdefault(int(row["step_seq"]), []).append(row)
    consumption_by_id = {
        row["injection_id"]: row for row in bundle["consumptions"]
    }
    windows = {
        int(window["step"]): window
        for window in snapshot["clock"]["step_windows"]
    }

    step_output: list[dict[str, Any]] = []
    blocked_steps: list[dict[str, Any]] = []
    for plan_step in sorted(plan["steps"], key=lambda item: int(item["seq"])):
        seq = int(plan_step["seq"])
        row = steps_by_seq[seq]
        deps = [int(dep) for dep in plan_step["depends_on"]]
        unsatisfied = [
            dep
            for dep in deps
            if dep not in steps_by_seq
            or steps_by_seq[dep]["stage"] not in DEPENDENCY_SATISFIED_STAGES
        ]
        stage = str(row["stage"])
        if stage == STAGE_PENDING and unsatisfied:
            progress = "BLOCKED_BY_DEPENDENCY"
        elif stage == STAGE_PENDING:
            progress = "READY"
        elif stage == STAGE_RUNNING:
            progress = "IN_PROGRESS_UNCOMMITTED"
        elif stage in FINAL_STAGES:
            progress = "FINAL"
        else:
            progress = "UNKNOWN"
        observation_rows = obs_by_step.get(seq, [])
        observation = None
        if observation_rows:
            item = max(observation_rows, key=lambda obs: int(obs["attempt_no"]))
            observation = {
                "step": int(item["step_seq"]),
                "attempt_no": int(item["attempt_no"]),
                "code": item["code"],
                "outcome_class": item["outcome_class"],
                "stage": item["stage"],
                "event": item["event"],
                "response_status": item["response_status"],
                "body_sha256": item["body_sha256"],
                "injected_at_ms": item["injected_at_ms"],
                "injections": json.loads(item["injections_json"]),
                "detail": json.loads(item["detail_json"]),
                "result_digest": item["result_digest"],
            }
        outcome_class = row["outcome_class"]
        if stage == STAGE_RUNNING:
            outcome_class = "RUNNING_UNCOMMITTED"
        if stage == STAGE_PENDING and outcome_class == OUTCOME_NOT_STARTED:
            outcome_class = OUTCOME_NOT_STARTED
        started_at = row["started_at_ms"]
        ended_at = row["ended_at_ms"]
        elapsed = (
            int(ended_at) - int(started_at)
            if started_at is not None and ended_at is not None
            else None
        )
        step_item = {
            "seq": seq,
            "name": plan_step["name"],
            "dependencies": deps,
            "stage": stage,
            "outcome_code": row["outcome_code"],
            "outcome_class": outcome_class,
            "progress": progress,
            "attempt_no": int(row["attempt_no"]),
            "started": started_at is not None,
            "started_at_ms": started_at,
            "ended_at_ms": ended_at,
            "logical_elapsed_ms": elapsed,
            "window": windows.get(seq),
            "blocked_by": unsatisfied,
            "observation": observation,
        }
        step_output.append(step_item)
        if progress == "BLOCKED_BY_DEPENDENCY":
            blocked_steps.append(
                {"step": seq, "blocked_by": unsatisfied}
            )

    injection_output: list[dict[str, Any]] = []
    for injection in sorted(
        snapshot["injections"],
        key=lambda item: (
            int(item["at_ms"]),
            int(item["step"]),
            item["id"],
        ),
    ):
        consumption = consumption_by_id.get(injection["id"])
        injection_output.append(
            {
                "id": injection["id"],
                "step": int(injection["step"]),
                "at_ms": int(injection["at_ms"]),
                "type": injection["type"],
                "rollback_ms": int(injection.get("rollback_ms", 0)),
                "description": injection.get("description", ""),
                "consumed": consumption is not None,
                "consumed_at_ms": (
                    int(consumption["consumed_at_ms"])
                    if consumption is not None
                    else None
                ),
                "consuming_attempt_no": (
                    int(consumption["attempt_no"])
                    if consumption is not None
                    else None
                ),
                "outcome_code": (
                    consumption["outcome_code"]
                    if consumption is not None
                    else None
                ),
                "state": "CONSUMED" if consumption is not None else "PENDING",
            }
        )

    return {
        "schema_version": 1,
        "run": {
            "run_id": run["run_id"],
            "plan": run["plan_name"],
            "version": int(run["version"]),
            "state": run["state"],
            "logical_clock_ms": int(run["logical_clock_ms"]),
            "observation_digest": run["observation_digest"],
            "injection_watermark": int(run["injection_watermark"]),
            "commit_seq": int(run["commit_seq"]),
            "commit_head": run["commit_head"],
            "bound_version_is_current": (
                bundle["pointer_version"] == int(run["version"])
            ),
            "current_snapshot_version": bundle["pointer_version"],
        },
        "clock": {
            "start_ms": int(snapshot["clock"]["start_ms"]),
            "current_ms": int(run["logical_clock_ms"]),
            "tick_logical_ms": int(snapshot["clock"]["tick"]["logical_ms"]),
            "tick_real_ms": int(snapshot["clock"]["tick"]["real_ms"]),
            "wall_clock_used_for_decision": False,
            "step_windows": sorted(
                [
                    {
                        "step": int(item["step"]),
                        "start_ms": int(item["start_ms"]),
                        "end_ms": int(item["end_ms"]),
                    }
                    for item in snapshot["clock"]["step_windows"]
                ],
                key=lambda item: item["step"],
            ),
        },
        "steps": step_output,
        "injections": injection_output,
        "blocked_steps": blocked_steps,
        "summary": {
            "observed": sum(
                1 for item in step_output if item["stage"] == "OBSERVED"
            ),
            "timed_out": sum(
                1 for item in step_output if item["stage"] == "TIMED_OUT"
            ),
            "aborted": sum(
                1 for item in step_output if item["stage"] == "ABORTED"
            ),
            "skipped": sum(
                1 for item in step_output if item["stage"] == "SKIPPED"
            ),
            "pending": sum(
                1 for item in step_output if item["stage"] == "PENDING"
            ),
            "running": sum(
                1 for item in step_output if item["stage"] == "RUNNING"
            ),
            "injections_consumed": sum(
                1 for item in injection_output if item["consumed"]
            ),
            "injections_pending": sum(
                1 for item in injection_output if not item["consumed"]
            ),
        },
    }


def inject_report(store: StateStore, run_id: str) -> dict[str, Any]:
    status = build_status(store, run_id)
    return {
        "ok": True,
        "run_id": run_id,
        "version": status["run"]["version"],
        "logical_clock_ms": status["run"]["logical_clock_ms"],
        "injection_watermark": status["run"]["injection_watermark"],
        "injections": status["injections"],
    }


def render_text(status: dict[str, Any]) -> str:
    run = status["run"]
    lines = [
        f"DRILL {run['plan']}  version={run['version']}  run={run['run_id']}",
        (
            f"clock={run['logical_clock_ms']}ms  state={run['state']}  "
            f"pointer={run['current_snapshot_version']}"
        ),
        "",
        "SEQ  STAGE      OUTCOME_CLASS             OUTCOME_CODE             START  END",
    ]
    for step in status["steps"]:
        lines.append(
            f"{step['seq']:>3}  {step['stage']:<10} {step['outcome_class']:<25} "
            f"{step['outcome_code']:<24} "
            f"{str(step['started_at_ms'] if step['started_at_ms'] is not None else '-'):>5}  "
            f"{str(step['ended_at_ms'] if step['ended_at_ms'] is not None else '-'):>4}"
        )
        if step["blocked_by"]:
            lines.append(
                f"     blocked by steps: {', '.join(map(str, step['blocked_by']))}"
            )
    lines.append("")
    lines.append("INJECTIONS")
    for injection in status["injections"]:
        state = "consumed" if injection["consumed"] else "pending"
        lines.append(
            f"  {injection['id']}: step={injection['step']} "
            f"at={injection['at_ms']}ms type={injection['type']} "
            f"state={state} outcome={injection['outcome_code'] or '-'}"
        )
    lines.append("")
    summary = status["summary"]
    lines.append(
        "SUMMARY observed={observed} timed_out={timed_out} aborted={aborted} "
        "skipped={skipped} pending={pending} running={running} "
        "injections={injections_consumed}/{total}".format(
            **summary,
            total=summary["injections_consumed"] + summary["injections_pending"],
        )
    )
    return "\n".join(lines) + "\n"


def render_html(status: dict[str, Any]) -> str:
    run = status["run"]
    clock = status["clock"]
    all_start = min(
        [clock["start_ms"]]
        + [item["start_ms"] for item in clock["step_windows"]]
    )
    all_end = max(
        [clock["current_ms"]]
        + [item["end_ms"] for item in clock["step_windows"]]
    )
    span = max(1, all_end - all_start)

    def esc(value: Any) -> str:
        return html.escape(str(value))

    step_rows: list[str] = []
    for step in status["steps"]:
        window = step["window"] or {}
        start_pct = 100.0 * (
            int(window.get("start_ms", all_start)) - all_start
        ) / span
        width_pct = 100.0 * (
            int(window.get("end_ms", all_start)) - int(window.get("start_ms", all_start))
        ) / span
        stage = esc(step["stage"])
        detail = ""
        if step["observation"]:
            detail = (
                f"attempt {step['observation']['attempt_no']} · "
                f"inject@{step['observation']['injected_at_ms']}ms · "
                f"{esc(step['observation']['detail'].get('error') or 'OK')}"
            )
        blocked = (
            " · blocked by " + ",".join(map(str, step["blocked_by"]))
            if step["blocked_by"]
            else ""
        )
        step_rows.append(
            f"""
            <tr>
              <td>{step['seq']}</td>
              <td><strong>{esc(step['name'])}</strong><small>{esc(detail)}{esc(blocked)}</small></td>
              <td><span class="badge {stage.lower()}">{stage}</span></td>
              <td>{esc(step['outcome_class'])}<br><code>{esc(step['outcome_code'])}</code></td>
              <td>{esc(step['started_at_ms'] if step['started_at_ms'] is not None else '-')}</td>
              <td>{esc(step['ended_at_ms'] if step['ended_at_ms'] is not None else '-')}</td>
              <td class="timeline-cell">
                <div class="timeline"><div class="bar {stage.lower()}" style="left:{start_pct:.2f}%;width:{max(width_pct, 0.4):.2f}%"></div></div>
              </td>
            </tr>
            """
        )
    injection_rows: list[str] = []
    for injection in status["injections"]:
        injection_rows.append(
            f"""
            <tr>
              <td><code>{esc(injection['id'])}</code></td>
              <td>{injection['step']}</td>
              <td>{injection['at_ms']}</td>
              <td>{esc(injection['type'])}</td>
              <td><span class="badge {'observed' if injection['consumed'] else 'pending'}">{esc(injection['state'])}</span></td>
              <td>{esc(injection['outcome_code'] or '-')}</td>
            </tr>
            """
        )
    summary = status["summary"]
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DrillBench · {esc(run['plan'])}</title>
<style>
:root {{
  --bg:#0d1117; --panel:#161b22; --line:#30363d; --text:#e6edf3;
  --muted:#8b949e; --accent:#58a6ff; --ok:#3fb950; --warn:#d29922;
  --bad:#f85149; --skip:#a371f7;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text);
  font:14px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace; }}
main {{ max-width:1240px; margin:0 auto; padding:32px 22px 60px; }}
h1 {{ font-size:24px; margin:0 0 4px; }}
.sub {{ color:var(--muted); margin-bottom:24px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin:18px 0 26px; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px; }}
.card .k {{ color:var(--muted); font-size:12px; }} .card .v {{ font-size:22px; margin-top:4px; }}
.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px; margin:16px 0; overflow:auto; }}
table {{ width:100%; border-collapse:collapse; }}
th,td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
th {{ color:var(--muted); font-weight:normal; font-size:12px; text-transform:uppercase; }}
small {{ display:block; color:var(--muted); margin-top:3px; }}
code {{ color:#79c0ff; }}
.badge {{ display:inline-block; border-radius:999px; padding:2px 9px; font-size:12px; border:1px solid; }}
.badge.observed {{ color:var(--ok); border-color:var(--ok); }}
.badge.timed_out {{ color:var(--warn); border-color:var(--warn); }}
.badge.aborted {{ color:var(--bad); border-color:var(--bad); }}
.badge.skipped {{ color:var(--skip); border-color:var(--skip); }}
.badge.pending {{ color:var(--muted); border-color:var(--muted); }}
.badge.running {{ color:var(--accent); border-color:var(--accent); }}
.timeline-cell {{ min-width:230px; }}
.timeline {{ position:relative; height:12px; background:#0b0f14; border-radius:6px; overflow:hidden; }}
.bar {{ position:absolute; top:0; bottom:0; min-width:3px; background:var(--accent); }}
.bar.observed {{ background:var(--ok); }} .bar.timed_out {{ background:var(--warn); }}
.bar.aborted {{ background:var(--bad); }} .bar.skipped {{ background:var(--skip); }}
.bar.pending {{ background:#484f58; }}
footer {{ color:var(--muted); margin-top:26px; }}
</style>
</head>
<body><main>
<h1>{esc(run['plan'])}</h1>
<div class="sub">run {esc(run['run_id'])} · snapshot v{run['version']} · deterministic logical clock</div>
<div class="cards">
  <div class="card"><div class="k">LOGICAL CLOCK</div><div class="v">{run['logical_clock_ms']} ms</div></div>
  <div class="card"><div class="k">RUN STATE</div><div class="v">{esc(run['state'])}</div></div>
  <div class="card"><div class="k">OBSERVED</div><div class="v">{summary['observed']}</div></div>
  <div class="card"><div class="k">TIMED OUT</div><div class="v">{summary['timed_out']}</div></div>
  <div class="card"><div class="k">ABORTED</div><div class="v">{summary['aborted']}</div></div>
  <div class="card"><div class="k">SKIPPED</div><div class="v">{summary['skipped']}</div></div>
</div>
<div class="panel">
<table><thead><tr><th>#</th><th>Step</th><th>Stage</th><th>Outcome</th><th>Start</th><th>End</th><th>Logical window</th></tr></thead>
<tbody>{''.join(step_rows)}</tbody></table>
</div>
<div class="panel">
<table><thead><tr><th>Injection</th><th>Step</th><th>At ms</th><th>Type</th><th>State</th><th>Outcome</th></tr></thead>
<tbody>{''.join(injection_rows)}</tbody></table>
</div>
<div class="panel">
<div>observation digest: <code>{esc(run['observation_digest'])}</code></div>
<div>commit head: <code>{esc(run['commit_head'])}</code></div>
<div>injection watermark: {run['injection_watermark']} ms</div>
<div>snapshot pointer: v{run['current_snapshot_version']} · bound version current: {str(run['bound_version_is_current']).lower()}</div>
</div>
<footer>All timeout and recovery decisions use the injected logical clock only. No wall-clock reads are used for judgement.</footer>
</main></body></html>
"""

