"""Loopback-only HTTP diagnostic view served with the standard library."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import RhythmEngine
from .generator import generate_project
from .persistence import load_snapshot


def _grade_style(grade):
    return {
        "PERFECT": "#1a7f37",
        "GOOD": "#2f6fdb",
        "OFFSET": "#b26a00",
        "MISS": "#b42318",
    }.get(grade, "#667085")


def _fmt(value):
    return "n/a" if value is None else f"{value:.4f}"


def render_html(project, report, consistency):
    results = report["results"]
    stats = report["stats"]
    segments = project["timeline"]
    width_ms = project["legal_time_range_ms"]["end_ms"]

    segment_bars = "".join(
        f'<div class="seg" style="left:{seg["start_ms"] / width_ms * 100:.4f}%;'
        f'width:{(seg["end_ms"] - seg["start_ms"]) / width_ms * 100:.4f}%">'
        f'{seg["name"]}<br><span>{seg["id"]}</span></div>'
        for seg in segments
    )

    rows = []
    for result in results:
        corrected = result["corrected_click_ms"]
        left = (corrected / width_ms * 100) if corrected is not None else 0.0
        color = _grade_style(result["grade"])
        deviation = (
            "n/a"
            if result["deviation_ms"] is None
            else f'{result["deviation_ms"]:.3f} ms'
        )
        correction = (
            "n/a"
            if result["correction_ms"] is None
            else f'{result["correction_ms"]:.4f} ms'
        )
        beat = (
            "n/a"
            if result["beat_ms"] is None
            else f'#{result["beat_number"]} @ {result["beat_ms"]:.1f}'
        )
        issues = "".join(
            f'<li class="{item["severity"]}">{item["code"]}</li>'
            for item in result["validation"]["issues"]
        ) or '<li class="ok">no issues</li>'
        rows.append(
            f"""
        <tr>
          <td>{result['event_id']}</td>
          <td>{result['listener_id']}</td>
          <td>{result['language']} / {result['channel']}</td>
          <td class="timeline-cell">
            <div class="track">{segment_bars}
              <div class="hit" style="left:{left:.4f}%;background:{color}"
                   title="{result['status']} {result['grade'] or ''}"></div>
            </div>
          </td>
          <td>{beat}</td>
          <td><span class="badge" style="background:{color}">{
                result['grade'] or result['status']
            }</span></td>
          <td>{deviation}</td>
          <td>{correction}</td>
          <td><ul class="issues">{issues}</ul></td>
        </tr>"""
        )

    language_cards = "".join(
        f'<li><b>{row["language"]}</b>: accuracy '
        f'{(row["accuracy_ratio"] or 0) * 100:.1f}%, MAD '
        f'{(row["mean_abs_deviation_ms"] or 0):.2f} ms'
        f'{" <mark>FLAGGED: " + ",".join(row["flag_reasons"]) + "</mark>" if row["flagged"] else ""}</li>'
        for row in stats["languages"]
    )

    issue_rows = "".join(
        f'<li class="{item["severity"]}"><b>{item["code"]}</b> - '
        f'{item.get("group_key") or item.get("language") or item.get("event_id", "")} '
        f'- {item["message"]}</li>'
        for item in consistency["issues"]
    ) or '<li class="ok">No alignment inconsistency detected.</li>'

    threshold = consistency["thresholds"]["consistency_threshold_ms"]
    channel_rows = []
    for key, payload in sorted(consistency["by_channel"].items()):
        delta = payload["delta_from_language_median_ms"]
        row_class = "warn" if delta is not None and abs(delta) > threshold else ""
        channel_rows.append(
            f'<tr class="{row_class}"><td>{key}</td>'
            f'<td>{payload["event_count"]}</td>'
            f'<td>{_fmt(payload["median_correction_ms"])}</td>'
            f'<td>{_fmt(delta)}</td></tr>'
        )
    language_rows = []
    for language, payload in sorted(consistency["by_language"].items()):
        delta = payload["delta_from_global_median_ms"]
        row_class = "warn" if abs(delta) > threshold else ""
        language_rows.append(
            f'<tr class="{row_class}"><td>{language}</td>'
            f'<td>{payload["event_count"]}</td>'
            f'<td>{_fmt(payload["median_correction_ms"])}</td>'
            f'<td>{_fmt(delta)}</td></tr>'
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Rhythm Kernel Diagnostic View</title>
<style>
body {{ font-family: sans-serif; margin: 24px; color: #1d2939; }}
h1 {{ font-size: 20px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
th, td {{ border: 1px solid #d0d5dd; padding: 6px 8px; text-align: left;
         vertical-align: middle; }}
th {{ background: #f2f4f7; position: sticky; top: 0; }}
.timeline-cell {{ min-width: 360px; }}
.track {{ position: relative; height: 46px; background: #f9fafb;
         border: 1px solid #eaecf0; }}
.seg {{ position: absolute; top: 0; bottom: 0; font-size: 10px;
       padding: 2px; color: #344054; border-right: 1px solid #cbd5e1;
       box-sizing: border-box; overflow: hidden; }}
.seg span {{ color: #667085; }}
.hit {{ position: absolute; top: -2px; width: 8px; height: 50px;
       border-radius: 2px; transform: translateX(-4px); }}
.badge {{ color: white; padding: 2px 8px; border-radius: 10px;
         font-size: 11px; }}
.issues {{ margin: 0; padding-left: 14px; }}
.error {{ color: #b42318; }}
.warning {{ color: #b26a00; }}
.ok {{ color: #1a7f37; list-style: none; margin-left: -14px; }}
mark {{ background: #fef3c7; padding: 1px 4px; }}
.mini {{ width: auto; margin: 10px 0; font-size: 11px; }}
.mini caption {{ text-align: left; font-weight: bold; padding: 4px 0; }}
tr.warn td {{ background: #fef3c7; }}
.panel {{ background: #f8fafc; border: 1px solid #e4e7ec; padding: 12px 16px;
         margin: 12px 0; border-radius: 8px; }}
</style>
</head>
<body>
<h1>多语种节奏挑战 · 判定内核诊断视图</h1>
<div class="panel">
  <b>Project:</b> {project['project_id']} ·
  <b>Revision:</b> {report['state_revision']} ·
  <b>Events:</b> {stats['overall']['event_count']} ·
  <b>Judged:</b> {stats['overall']['judged_count']} ·
  <b>Overall accuracy:</b> {stats['overall']['accuracy_ratio'] * 100:.2f}%
  <ul>{language_cards}</ul>
</div>
<div class="panel">
  <b>跨语种时间对齐一致性校验</b> (ok={consistency['ok']},
  issues={consistency['issue_count']})
  <ul>{issue_rows}</ul>
  <table class="mini">
    <caption>通道校正中位数 vs 语种中位数（阈值
      {consistency['thresholds']['consistency_threshold_ms']} ms）</caption>
    <thead><tr><th>语种|通道</th><th>事件数</th><th>校正中位数(ms)</th>
      <th>相对语种中位数偏差(ms)</th></tr></thead>
    <tbody>{channel_rows}</tbody>
  </table>
  <table class="mini">
    <caption>语种校正中位数 vs 全局中位数
      ({consistency['global_median_correction_ms']:.4f} ms)</caption>
    <thead><tr><th>语种</th><th>事件数</th><th>校正中位数(ms)</th>
      <th>相对全局中位数偏差(ms)</th></tr></thead>
    <tbody>{language_rows}</tbody>
  </table>
</div>
<table>
<thead><tr>
  <th>Event</th><th>Listener</th><th>Lang/Channel</th><th>Timeline</th>
  <th>Beat</th><th>Grade</th><th>Deviation</th><th>Correction</th>
  <th>Validation</th>
</tr></thead>
<tbody>
{''.join(rows)}
</tbody>
</table>
</body>
</html>"""


class DiagnosticState:
    def __init__(self, project, report):
        self.project = project
        self.report = report


def build_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def _send(self, body, content_type, status=200):
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                html = render_html(
                    state.project, state.report, state.report["consistency_report"]
                )
                self._send(html, "text/html; charset=utf-8")
            elif self.path == "/api/report":
                self._send(
                    json.dumps(state.report, ensure_ascii=False),
                    "application/json; charset=utf-8",
                )
            elif self.path == "/api/consistency":
                self._send(
                    json.dumps(
                        state.report["consistency_report"], ensure_ascii=False
                    ),
                    "application/json; charset=utf-8",
                )
            elif self.path == "/health":
                self._send("ok", "text/plain")
            else:
                self._send("not found", "text/plain", status=404)

    return Handler


def serve(project=None, report=None, host="127.0.0.1", port=8080,
          snapshot_path=None):
    if project is None:
        if snapshot_path is not None:
            snapshot = load_snapshot(snapshot_path)
            project = snapshot["project"]
            engine = RhythmEngine(project)
            report = {
                "consistency_report": snapshot["consistency_report"],
                "results": list(snapshot["results"].values()),
                "state_revision": snapshot["state_revision"],
                "stats": snapshot["stats"],
            }
        else:
            project = generate_project()
    if report is None:
        report = RhythmEngine(project).run_full()
    state = DiagnosticState(project, report)
    server = ThreadingHTTPServer((host, port), build_handler(state))
    print(f"diagnostic view on http://{host}:{port}/ (loopback only)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
