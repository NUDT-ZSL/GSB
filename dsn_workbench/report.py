from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .state import Ledger
from .util import atomic_write_text, read_json_file


def _safe(value: Any) -> str:
    if isinstance(value, str):
        return html.escape(value)
    return html.escape(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    )


def _status(report: dict[str, Any]) -> tuple[str, str]:
    if report.get("ok"):
        return "PASS", "pass"
    if report.get("simulated_crash"):
        return "INTERRUPTED", "warn"
    return "FAIL", "fail"


def build_diagnostic_html(
    report: dict[str, Any],
    *,
    ledger_events: list[dict[str, Any]] | None = None,
    outputs: list[dict[str, Any]] | None = None,
    title: str = "Domain Segmentation Diagnostics",
) -> str:
    status_label, status_class = _status(report)
    cache_plan = report.get("cache_plan", {})
    resources = report.get("resources", {})
    verification = report.get("parallel_verification", {})
    stage_rows = []
    for stage in report.get("stage_reports", []):
        stage_rows.append(
            "<tr>"
            f"<td>{_safe(stage.get('stage'))}</td>"
            f"<td>{_safe(stage.get('checkpoint_version'))}</td>"
            f"<td>{_safe(stage.get('checkpoint_reused'))}</td>"
            f"<td>{_safe(stage.get('cache_hits'))}</td>"
            f"<td>{_safe(stage.get('computed'))}</td>"
            f"<td>{_safe(stage.get('batches'))}</td>"
            "</tr>"
        )
    if not stage_rows:
        stage_rows.append(
            '<tr><td colspan="6" class="muted">No committed stage report</td></tr>'
        )

    token_rows = []
    for output in (outputs or [])[:20]:
        tokens = " ".join(
            f"<span class='token'>{_safe(token['text'])}"
            f"<small>{_safe(token['pos_tag'])}</small></span>"
            for token in output.get("tokens", [])
        )
        token_rows.append(
            "<tr>"
            f"<td>{_safe(output.get('sentence_id'))}</td>"
            f"<td class='tokens'>{tokens}</td>"
            f"<td><code>{_safe(output.get('checksum', ''))[:16]}</code></td>"
            "</tr>"
        )
    if not token_rows:
        token_rows.append(
            '<tr><td colspan="3" class="muted">No output tokens in this report</td></tr>'
        )

    ledger_rows = []
    for event in (ledger_events or [])[-40:]:
        ledger_rows.append(
            "<tr>"
            f"<td>{_safe(event.get('seq'))}</td>"
            f"<td>{_safe(event.get('kind'))}</td>"
            f"<td>{_safe(event.get('checkpoint_version'))}</td>"
            f"<td><code>{_safe(event.get('checksum', ''))[:16]}</code></td>"
            "</tr>"
        )
    if not ledger_rows:
        ledger_rows.append(
            '<tr><td colspan="4" class="muted">No ledger events</td></tr>'
        )

    metrics = {
        "sentence_count": report.get("sentence_count"),
        "output_sha256": report.get("output_sha256"),
        "parallel_serial_status": verification.get("status"),
        "serial_sha256": verification.get("serial_sha256"),
        "parallel_sha256": verification.get("parallel_sha256"),
        "affected_sentences": cache_plan.get("affected_sentences", []),
        "reuse_sentences": cache_plan.get("reuse_sentences", []),
        "resources": resources,
        "reference_metrics": report.get("reference_metrics"),
    }
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_safe(title)}</title>
<style>
:root {{ color-scheme: light dark; --line:#cbd5e1; --muted:#64748b;
  --pass:#047857; --fail:#b91c1c; --warn:#b45309; --panel:#f8fafc; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  color:#0f172a; background:#eef2f7; }}
header {{ padding:28px 36px; background:#0f172a; color:white; }}
header h1 {{ margin:0 0 6px; font-size:24px; }}
header p {{ margin:0; color:#cbd5e1; }}
main {{ max-width:1280px; margin:0 auto; padding:24px; }}
.badge {{ display:inline-block; border-radius:999px; padding:3px 12px;
  font-weight:700; letter-spacing:.04em; background:#e2e8f0; }}
.badge.pass {{ color:var(--pass); }} .badge.fail {{ color:var(--fail); }}
.badge.warn {{ color:var(--warn); }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:16px; }}
section {{ background:var(--panel); border:1px solid var(--line); border-radius:12px;
  padding:18px; margin:16px 0; box-shadow:0 1px 2px #0f172a12; }}
section h2 {{ margin:0 0 12px; font-size:17px; }}
table {{ width:100%; border-collapse:collapse; }}
th,td {{ border-bottom:1px solid var(--line); padding:7px 8px; text-align:left;
  vertical-align:top; }}
th {{ color:var(--muted); font-weight:600; }}
code {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }}
pre {{ white-space:pre-wrap; overflow-wrap:anywhere; background:#e8edf4;
  padding:12px; border-radius:8px; margin:0; }}
.token {{ display:inline-block; padding:5px 8px; margin:2px; border-radius:7px;
  background:white; border:1px solid var(--line); }}
.token small {{ display:block; color:var(--muted); font-size:10px; }}
.muted {{ color:var(--muted); }}
</style>
</head>
<body>
<header>
  <h1>{_safe(title)}</h1>
  <p>Run status <span class="badge {status_class}">{status_label}</span>
  &nbsp; domain workbench · deterministic pipeline diagnostics</p>
</header>
<main>
<div class="grid">
  <section><h2>Run</h2><pre>{_safe(metrics)}</pre></section>
  <section><h2>Parallel / Serial</h2><pre>{_safe(verification)}</pre></section>
</div>
<section><h2>Stage checkpoints and cache activity</h2>
<table><thead><tr><th>Stage</th><th>Version</th><th>Checkpoint reused</th>
<th>Cache hits</th><th>Computed</th><th>Batches</th></tr></thead>
<tbody>{''.join(stage_rows)}</tbody></table></section>
<section><h2>Directed invalidation</h2><pre>{_safe(cache_plan)}</pre></section>
<section><h2>Output tokens</h2>
<table><thead><tr><th>Sentence</th><th>Token / POS</th><th>Checksum</th></tr></thead>
<tbody>{''.join(token_rows)}</tbody></table></section>
<section><h2>Versioned ledger</h2>
<table><thead><tr><th>Seq</th><th>Event</th><th>Checkpoint version</th>
<th>Checksum</th></tr></thead><tbody>{''.join(ledger_rows)}</tbody></table></section>
</main>
</body>
</html>
"""


def write_diagnostic_report(
    state_dir: str | Path,
    output_path: str | Path,
    *,
    report: dict[str, Any] | None = None,
    outputs: list[dict[str, Any]] | None = None,
    title: str = "Domain Segmentation Diagnostics",
) -> Path:
    state = Path(state_dir)
    if report is None:
        report_path = state / "last_run.json"
        if report_path.exists():
            report = read_json_file(report_path)
        else:
            report = {
                "ok": False,
                "reason": "last_run.json not found",
                "cache_plan": {},
                "resources": {},
                "parallel_verification": {},
            }
    ledger_events = Ledger(state).events(80)
    rendered = build_diagnostic_html(
        report,
        ledger_events=ledger_events,
        outputs=outputs,
        title=title,
    )
    target = Path(output_path)
    atomic_write_text(target, rendered)
    return target

