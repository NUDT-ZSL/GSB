from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import numpy as np

from .config import AuditConfig
from .incremental import IncrementalManager
from .pipeline import AuditPipeline
from .query import QueryIndex
from .rules import (
    build_default_v1_rules,
    migrate_v1_to_v2,
    write_v1_rules,
    write_v2_sharded,
)
from .sample_generator import write_sample_bundle


HTML_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>多说话人换流点审计演示</title>
<style>
:root { color-scheme: light; font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
body { margin: 0; background: #f6f7fb; color: #172033; }
header { padding: 20px 28px; background: #111827; color: white; }
header h1 { margin: 0 0 6px; font-size: 22px; }
header p { margin: 0; color: #b8c2d6; font-size: 13px; }
main { padding: 20px 28px 60px; max-width: 1280px; margin: 0 auto; }
.grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.card { background: white; border: 1px solid #e5e8f0; border-radius: 12px; padding: 16px; box-shadow: 0 1px 2px rgba(16,24,40,.04); }
.card.wide { grid-column: 1 / -1; }
h2 { font-size: 16px; margin: 0 0 12px; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th, td { border-bottom: 1px solid #edf0f5; padding: 7px 8px; text-align: left; vertical-align: top; }
th { background: #fafbfe; color: #5b6577; font-weight: 600; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
pre { background: #0f172a; color: #dbeafe; padding: 12px; border-radius: 8px; overflow: auto; max-height: 360px; font-size: 11px; }
button, input { border-radius: 8px; border: 1px solid #ccd3e0; padding: 8px 10px; font-size: 13px; }
button { background: #2563eb; color: white; border-color: #2563eb; cursor: pointer; margin-right: 8px; }
button.secondary { background: white; color: #1f2937; }
button.danger { background: #b42318; border-color: #b42318; }
.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
.pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 700; }
.ok { background: #dcfce7; color: #166534; }
.bad { background: #fee4e2; color: #991b1b; }
.warn { background: #fef3c7; color: #92400e; }
.muted { color: #667085; font-size: 12px; }
</style>
</head>
<body>
<header>
  <h1>多说话人会议音频 · 换流点检测与双版本账本</h1>
  <p>离线确定性样本 / 16 kHz mono PCM / 20 ms 帧长 / 10 ms 帧移 / 内容寻址增量恢复</p>
</header>
<main>
  <section class="card wide">
    <h2>交互操作与实时查询</h2>
    <div class="row">
      <label>毫秒点 <input id="ms" value="12120" /></label>
      <button onclick="queryMs()">查询时间点</button>
      <label>段编号 <input id="seg" value="SEG-0006" /></label>
      <button class="secondary" onclick="querySeg()">查询语音段</button>
    </div>
    <div class="row">
      <button onclick="update('truth')">修正 SEG-0005 真值</button>
      <button onclick="update('rules')">提升规则版本</button>
      <button onclick="update('audio')">到达音频修订</button>
      <button class="danger" onclick="resetDemo()">重置演示</button>
      <span id="status" class="muted"></span>
    </div>
    <pre id="query">查询结果将显示在这里。</pre>
  </section>

  <div class="grid" style="margin-top:16px">
    <section class="card wide"><h2>换流点事件与业务码</h2><div id="events"></div></section>
    <section class="card"><h2>语音段与稳定身份</h2><div id="segments"></div></section>
    <section class="card"><h2>自检 / 诊断 / 缓存</h2><pre id="health"></pre></section>
    <section class="card"><h2>v1 扁平行规则视图</h2><pre id="v1"></pre></section>
    <section class="card"><h2>v2 分片 + SQLite 规则视图</h2><pre id="v2"></pre></section>
    <section class="card wide"><h2>增量失效与重放集合</h2><pre id="incremental"></pre></section>
  </div>
</main>
<script>
function pill(ok, yes='通过', no='失败') {
  return `<span class="pill ${ok ? 'ok' : 'bad'}">${ok ? yes : no}</span>`;
}
function table(rows, columns) {
  return `<table><thead><tr>${columns.map(c => `<th>${c[0]}</th>`).join('')}</tr></thead><tbody>` +
    rows.map(r => `<tr>${columns.map(c => `<td>${c[2] ? c[2](r[c[1]], r) : (r[c[1]] ?? '')}</td>`).join('')}</tr>`).join('') +
    `</tbody></table>`;
}
async function api(path, options) {
  const response = await fetch(path, options || {});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}
async function loadState() {
  const s = await api('/api/state');
  document.getElementById('status').innerHTML =
    `自检 ${pill(s.self_check_ok)} 规则迁移 ${pill(s.rule_equivalent)} 快照版本 v${s.audit_version}`;
  document.getElementById('events').innerHTML = table(s.events, [
    ['ID','event_id'], ['时间 ms','at_ms'], ['类型','anomaly_type'], ['业务码','business_code'],
    ['说话人','speaker_id'], ['前静音','preceding_silence_ms'], ['能量比','energy_ratio_q']
  ]);
  document.getElementById('segments').innerHTML = table(s.segments, [
    ['段','segment_id'], ['起','start_ms'], ['止','end_ms'], ['说话人','speaker_id'],
    ['状态','identity_status'], ['音高 Hz','pitch_hz']
  ]);
  document.getElementById('health').textContent = JSON.stringify(s.health, null, 2);
  document.getElementById('v1').textContent = JSON.stringify(s.v1_rules, null, 2);
  document.getElementById('v2').textContent = JSON.stringify(s.v2_rules_meta, null, 2);
  document.getElementById('incremental').textContent = JSON.stringify(s.incremental_report || {note:'尚未执行增量更新'}, null, 2);
}
async function queryMs() {
  const ms = document.getElementById('ms').value;
  document.getElementById('query').textContent = JSON.stringify(await api('/api/query?ms=' + encodeURIComponent(ms)), null, 2);
}
async function querySeg() {
  const seg = document.getElementById('seg').value;
  document.getElementById('query').textContent = JSON.stringify(await api('/api/query?segment=' + encodeURIComponent(seg)), null, 2);
}
async function update(action) {
  document.getElementById('status').textContent = '处理中…';
  try {
    const data = await api('/api/update', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action})});
    document.getElementById('query').textContent = JSON.stringify(data.report || data, null, 2);
    await loadState();
  } catch (error) {
    document.getElementById('query').textContent = String(error);
  }
}
async function resetDemo() {
  await api('/api/reset', {method:'POST'});
  await loadState();
}
loadState();
setInterval(loadState, 5000);
</script>
</body>
</html>
"""


class DemoContext:
    def __init__(self, workspace: str | Path):
        self.config = AuditConfig()
        self.pipeline = AuditPipeline(workspace, self.config)
        if not self.pipeline.last_result_path.exists():
            self.pipeline.run_full("web-base")
        self.manager = IncrementalManager(self.pipeline)
        self.lock = threading.Lock()

    def state(self) -> dict[str, Any]:
        result = self.pipeline.load_last_result()
        speaker_by_segment = {
            segment_id: assignment.get("speaker_id")
            for segment_id, assignment in result.get("assignments", {}).items()
        }
        events = []
        for turn in result["classified_turns"]:
            segment = next(
                (
                    segment
                    for segment in result["segments"]
                    if int(segment["start_ms"]) == int(turn["at_ms"])
                ),
                None,
            )
            events.append(
                {
                    **turn,
                    "speaker_id": speaker_by_segment.get(segment["segment_id"])
                    if segment
                    else None,
                }
            )
        segments = []
        for segment in result["segments"]:
            assignment = result["assignments"].get(segment["segment_id"], {})
            segments.append(
                {
                    **segment,
                    "speaker_id": assignment.get("speaker_id"),
                    "identity_status": assignment.get("status"),
                    "pitch_hz": segment["speaker_feature"].get("pitch_hz"),
                }
            )
        return {
            "self_check_ok": result["self_check"]["ok"],
            "rule_equivalent": result["rule_view_check"]["equivalent"],
            "audit_version": (result.get("snapshots", {}).get("audit_report") or {}).get(
                "version"
            ),
            "events": events,
            "segments": segments,
            "health": {
                "self_check": result["self_check"],
                "diagnostic": result.get("diagnostic"),
                "ledger": result.get("ledger"),
                "cache_report": result.get("cache_report"),
            },
            "v1_rules": result["v1_rules"],
            "v2_rules_meta": {
                "rule_version": result["v2_rules"].get("rule_version"),
                "generation": "v2",
                "time_base": "absolute_ms",
                "sqlite": "rules/v2/rules_v2.sqlite",
                "rule_count": len(result["v2_rules"].get("rules", [])),
                "rules": result["v2_rules"].get("rules", []),
                "migration_report": result["v2_rules"].get("migration_report"),
            },
            "incremental_report": result.get("incremental_report"),
        }

    def query(self, params: dict[str, list[str]]) -> dict[str, Any]:
        result = self.pipeline.load_last_result()
        index = QueryIndex(result, self.config)
        if "ms" in params:
            return index.at_ms(params["ms"][0])
        if "segment" in params:
            return index.segment(params["segment"][0])
        raise ValueError("missing ms or segment")

    def update(self, action: str) -> dict[str, Any]:
        with self.lock:
            if action == "truth":
                return self.manager.apply_truth_correction(
                    "SEG-0005", "SPK_B", "web-truth-" + _suffix()
                )
            if action == "rules":
                return self.manager.promote_rules(
                    "v2.1.0-web",
                    {"rapid_switch_ms": 800, "silence_long_ms": 1800},
                    "web-rules-" + _suffix(),
                )
            if action == "audio":
                pcm, _ = self.pipeline.ensure_sample()
                revised = pcm.copy()
                start = 15_000 * self.config.samples_per_ms
                revised[start:] = np.clip(
                    np.rint(revised[start:].astype(np.float64) * 0.82),
                    -32768,
                    32767,
                ).astype(np.int16)
                return self.manager.apply_audio_revision(
                    revised, "web-audio-" + _suffix(), "web deterministic gain revision"
                )
            raise ValueError(f"unknown action: {action}")

    def reset(self) -> dict[str, Any]:
        with self.lock:
            write_sample_bundle(self.pipeline.sample_dir, self.config)
            v1_doc = build_default_v1_rules(self.config)
            v2_doc, _ = migrate_v1_to_v2(v1_doc)
            write_v1_rules(v1_doc, self.pipeline.rules_v1_dir / "rules.json")
            write_v2_sharded(v2_doc, self.pipeline.rules_v2_dir)
            from .canonical import atomic_write_json

            atomic_write_json(
                self.pipeline.rules_v2_dir / "rules_v2_full.json", v2_doc
            )
            result = self.pipeline.run_full(
                "web-reset", v1_doc=v1_doc, v2_doc=v2_doc
            )
            return {"ok": result["self_check"]["ok"]}


def _suffix() -> str:
    from .canonical import content_key

    return content_key(str(Path.cwd()) + str(threading.get_ident()))[:8]


def make_handler(context: DemoContext):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    body = HTML_PAGE.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif parsed.path == "/api/state":
                    self._json(200, context.state())
                elif parsed.path == "/api/query":
                    self._json(200, context.query(parse_qs(parsed.query)))
                else:
                    self._json(404, {"error": "not found"})
            except Exception as exc:  # noqa: BLE001
                self._json(400, {"error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                if parsed.path == "/api/update":
                    self._json(200, {"report": context.update(str(payload.get("action")))})
                elif parsed.path == "/api/reset":
                    self._json(200, context.reset())
                else:
                    self._json(404, {"error": "not found"})
            except Exception as exc:  # noqa: BLE001
                self._json(400, {"error": str(exc)})

    return Handler


def serve(workspace: str | Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    context = DemoContext(workspace)
    server = ThreadingHTTPServer((host, port), make_handler(context))
    print(f"Meeting audit demo: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
