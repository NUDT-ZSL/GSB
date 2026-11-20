"""可交互演示应用：实时字幕流处理引擎的本地 Web 控制台。

仅使用 Python 标准库。启动后访问 http://127.0.0.1:8765/
- 场景按钮触发确定性演示（含迟到回滚与超延迟）；
- 页面展示当前块序号、临时版本、可见字幕行、改写记录、收敛统计、
  最近业务错误码；
- /api/preview 支持并发预览；/api/commit 同一时刻只允许一个快照提交，
  冲突时后到者立即获得 E_STAGE_BUSY。
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from livecaption.engine import CaptionEngine
from livecaption.errors import BusinessError
from livecaption.scenarios import SCENARIOS
from livecaption.snapshots import SnapshotStore

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_DIR = os.path.join(BASE_DIR, "snapshots")
HOST, PORT = "127.0.0.1", int(os.environ.get("CAPTION_PORT", "8790"))


class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.store = SnapshotStore(SNAPSHOT_DIR)
        self.engine = None
        self.scenario = ""
        self.description = ""
        self.running = False
        self.errors = []
        self.commit_log = []
        self.snap_version = self._init_version()

    def _init_version(self) -> int:
        versions = [0]
        for cat in ("manifest", "policy", "lines"):
            meta = self.store.meta(cat)
            if meta is not None:
                versions.append(meta.version)
        return max(versions)


APP = AppState()


def _record_error(err: BusinessError) -> None:
    with APP.lock:
        APP.errors.append(err.to_dict())
        APP.errors = APP.errors[-20:]


def run_scenario(name: str) -> None:
    events, policy, description = SCENARIOS[name]()
    engine = CaptionEngine(policy, events[0].stream_id, strict=False)
    with APP.lock:
        APP.engine = engine
        APP.scenario = name
        APP.description = description
        APP.running = True
        APP.errors = []
    try:
        for ev in events:
            engine.ingest(ev)
            time.sleep(0.03)  # 仅 UI 节奏，不影响确定性结果
        engine.finish()
        with APP.lock:
            APP.snap_version += 1
            version = APP.snap_version
        manifest = "\n".join(json.dumps(e.to_dict(), ensure_ascii=False,
                                        sort_keys=True) for e in events)
        lines_payload = "\n".join(ln.text for ln in engine.visible_lines)
        APP.store.commit("manifest", version, manifest)
        APP.store.commit("policy", version, policy.canonical_json())
        APP.store.commit("lines", version, lines_payload)
        with APP.lock:
            APP.commit_log.append(f"committed snapshots v{version}")
    except BusinessError as err:
        _record_error(err)
    finally:
        with APP.lock:
            APP.running = False


def state_payload() -> dict:
    with APP.lock:
        engine = APP.engine
        errors = list(APP.errors)
        scenario, description = APP.scenario, APP.description
        running = APP.running
        commit_log = list(APP.commit_log[-5:])
    payload = {
        "scenario": scenario, "description": description, "running": running,
        "scenarios": sorted(SCENARIOS), "errors": errors,
        "commit_log": commit_log,
        "snapshots": {c: (APP.store.meta(c).__dict__ if APP.store.meta(c)
                          else None)
                      for c in ("manifest", "policy", "lines")},
    }
    if engine is not None:
        with APP.lock:
            stats = engine.stats()
            visible = [ln.text for ln in engine.visible_lines][
                -engine.policy.max_lines_per_screen:]
            rewrites = [{
                "version": r.version, "reason": r.reason,
                "chunk_seq": r.chunk_seq,
                "trigger": r.trigger_event.word,
                "replaced": list(r.replaced_lines),
                "new": [ln.text for ln in r.new_lines][-3:],
            } for r in engine.rewrite_log.records[-8:]]
            timings = [{
                "chunk_seq": t.chunk_seq, "events": t.event_count,
                "wait_ms": t.wait_ms, "chosen_wait_ms": t.chosen_wait_ms,
                "process_us": t.process_us,
            } for t in engine.latency.timings[-8:]]
            plans = [{"chunk_seq": p.chunk_seq,
                      "chosen_wait_ms": p.chosen_wait_ms,
                      "rationale": p.rationale}
                     for p in engine.latency.plans[-4:]]
            final_text = engine.final_result.text if engine.final_result else ""
            engine_errors = list(engine.errors)
        payload.update({
            "stats": stats, "visible_lines": visible, "rewrites": rewrites,
            "timings": timings, "plans": plans, "final_text": final_text,
            "errors": errors + engine_errors,
        })
    return payload


INDEX_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>实时字幕流控制台</title>
<style>
body{font-family:system-ui,Segoe UI,Microsoft YaHei,sans-serif;background:#10141a;
color:#e6e9ee;margin:0;padding:24px}
h1{font-size:20px} h2{font-size:15px;color:#8ab4f8;margin:14px 0 6px}
button{background:#1f6feb;color:#fff;border:0;border-radius:6px;padding:6px 12px;
margin:2px;cursor:pointer} button:hover{background:#388bfd}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.card{background:#171c24;border:1px solid #2a313c;border-radius:10px;padding:12px}
.caption{background:#000;border-radius:8px;padding:14px;min-height:70px;
font-size:20px;line-height:1.7;text-align:center}
.err{color:#ff7b72;font-family:monospace;font-size:12px}
.ok{color:#3fb950}.mono{font-family:monospace;font-size:12px;white-space:pre-wrap}
table{border-collapse:collapse;font-size:12px;font-family:monospace}
td,th{border:1px solid #2a313c;padding:2px 8px}
.stat{display:inline-block;margin:0 14px 4px 0;font-size:13px}
.stat b{color:#8ab4f8}
</style></head><body>
<h1>实时字幕流处理控制台 <span id="running"></span></h1>
<div id="buttons"></div>
<div class="grid">
<div>
  <div class="card"><h2>屏幕字幕（可见行）</h2>
    <div class="caption" id="captions"></div>
    <div id="stats"></div></div>
  <div class="card"><h2>改写记录（版本化）</h2><div id="rewrites" class="mono"></div></div>
  <div class="card"><h2>最近业务错误码</h2><div id="errors"></div></div>
</div>
<div>
  <div class="card"><h2>块延迟计时与等待裁决</h2><div id="timings"></div>
    <div id="plans" class="mono"></div></div>
  <div class="card"><h2>收敛统计 / 定稿</h2><div id="final" class="mono"></div></div>
  <div class="card"><h2>快照</h2><div id="snaps" class="mono"></div>
    <button onclick="commitRace()">并发提交演示（E_STAGE_BUSY）</button>
    <div id="commits" class="mono"></div></div>
</div></div>
<script>
async function api(p, m){const r=await fetch(p,{method:m||'GET'});return r.json();}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;');}
async function refresh(){
  const s = await api('/api/state');
  document.getElementById('running').textContent = s.running?'running':'';
  document.getElementById('buttons').innerHTML =
    s.scenarios.map(n=>`<button onclick="run('${n}')">${n}</button>`).join('') +
    ` <span class="mono">${esc(s.description||'')}</span>`;
  document.getElementById('captions').innerHTML =
    (s.visible_lines||[]).map(l=>`<div>${esc(l)}</div>`).join('')
    || '<span style="color:#556">waiting...</span>';
  if(s.stats){const t=s.stats;
    document.getElementById('stats').innerHTML =
      `<span class="stat">当前块 <b>${t.current_chunk_seq}</b></span>`+
      `<span class="stat">临时版本 <b>v${t.temp_version}</b></span>`+
      `<span class="stat">已收事件 <b>${t.received_events}</b></span>`+
      `<span class="stat">定稿块 <b>${t.finalized_chunks}/${t.total_chunks}</b></span>`+
      `<span class="stat">改写总数 <b>${t.rewrite_total}</b></span>`+
      `<span class="stat">驻留快照 <b>${t.resident_snapshots}</b></span>`+
      (t.degraded.length?`<span class="stat err">降级 ${t.degraded}</span>`:'')+
      (t.converged===true?`<span class="stat ok">收敛 OK</span>`:'');
    document.getElementById('final').textContent =
      (t.finished?`块数=${t.finalized_chunks} 改写=${t.rewrite_total} 收敛=${t.converged}\n`:'')
      + (s.final_text||'');
  }
  document.getElementById('rewrites').textContent =
    (s.rewrites||[]).map(r=>`v${r.version} [${r.reason}] 块${r.chunk_seq} 触发词=${r.trigger}\n`+
      `  替换: ${JSON.stringify(r.replaced)}\n  新版: ${JSON.stringify(r.new)}`)
      .join('\\n') || '(none)';
  document.getElementById('errors').innerHTML =
    (s.errors||[]).map(e=>`<div class="err">${e.code} ${esc(e.message)}</div>`).join('')
    || '<span class="ok">无</span>';
  document.getElementById('timings').innerHTML =
    '<table><tr><th>块</th><th>事件数</th><th>等待ms</th><th>所选等待</th><th>处理us</th></tr>'+
    (s.timings||[]).map(t=>`<tr><td>${t.chunk_seq}</td><td>${t.events}</td>`+
      `<td>${t.wait_ms}</td><td>${t.chosen_wait_ms}</td><td>${t.process_us}</td></tr>`)
      .join('')+'</table>';
  document.getElementById('plans').textContent =
    (s.plans||[]).map(p=>`块${p.chunk_seq}: 等待${p.chosen_wait_ms}ms - ${p.rationale}`).join('\\n');
  document.getElementById('snaps').textContent =
    Object.entries(s.snapshots||{}).map(([k,v])=>v?`${k}: v${v.version} ${v.checksum.slice(0,12)}...`:`${k}: -`).join('\\n');
}
async function run(n){await api('/api/run?scenario='+n,'POST');refresh();}
async function commitRace(){
  const r = await api('/api/commit_race','POST');
  document.getElementById('commits').textContent = r.results.join('\\n');
  refresh();
}
setInterval(refresh, 400); refresh();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, status: int = 200) -> None:
        body = obj.encode("utf-8") if isinstance(obj, str) else \
            json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type",
                         "text/html; charset=utf-8" if isinstance(obj, str)
                         else "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send(INDEX_HTML)
        elif path == "/api/state":
            self._send(state_payload())
        elif path == "/api/preview":
            with APP.lock:
                engine = APP.engine
                lines = ([ln.text for ln in engine.visible_lines]
                         if engine else [])
            self._send({"preview": lines, "ts": time.time()})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/run":
            name = parse_qs(parsed.query).get("scenario", ["normal"])[0]
            if name not in SCENARIOS:
                self._send({"error": "unknown scenario"}, 400)
                return
            with APP.lock:
                running = APP.running
            if running:
                self._send({"error": "busy"}, 409)
                return
            threading.Thread(target=run_scenario, args=(name,),
                             daemon=True).start()
            self._send({"started": name})
        elif path == "/api/commit":
            with APP.lock:
                APP.snap_version += 1
                version = APP.snap_version
            try:
                meta = APP.store.commit("lines", version,
                                        f"manual commit v{version}")
                with APP.lock:
                    APP.commit_log.append(f"commit v{meta.version} ok")
                self._send({"ok": True, "version": meta.version})
            except BusinessError as err:
                _record_error(err)
                self._send(err.to_dict(), 409)
        elif path == "/api/commit_race":
            results = []
            lock = threading.Lock()

            def worker(i: int) -> None:
                try:
                    APP.store.commit("lines", 10_000 + i,
                                     f"race payload {i}")
                    with lock:
                        results.append(f"worker{i}: committed")
                except BusinessError as err:
                    with lock:
                        results.append(f"worker{i}: {err.code}")

            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            with APP.lock:
                APP.commit_log.extend(results)
            self._send({"results": sorted(results)})
        else:
            self._send({"error": "not found"}, 404)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"console: http://{HOST}:{PORT}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
