from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .canonical import canonical_json
from .demo import run_budget_conflict_demo, run_fault_attribution_demo
from .errors import DomainError, ErrorCode
from .models import Budget, ExecutionConfig, SchedulerConfig, ToleranceConfig
from .service import QConvService


def _error_payload(exc: DomainError) -> dict[str, Any]:
    return {"ok": False, "code": str(exc.code), "error": exc.as_dict()}


class Application:
    def __init__(
        self,
        service: QConvService,
        jobs_path: str | Path,
        budget: Budget,
        scheduler_config: SchedulerConfig,
        tolerance: ToleranceConfig,
    ) -> None:
        self.service = service
        self.jobs_path = Path(jobs_path)
        self.default_budget = budget
        self.default_scheduler = scheduler_config
        self.default_tolerance = tolerance
        self.jobs_text = self.jobs_path.read_text(encoding="utf-8")
        self.last_artifact: dict[str, Any] | None = None
        self.last_error: dict[str, Any] | None = None
        self.run_lock = threading.Lock()
        self.snapshot_meta: dict[str, Any] | None = None
        self._refresh_snapshot_meta()

    def _refresh_snapshot_meta(self) -> None:
        try:
            snapshot = self.service.store.load()
        except DomainError as exc:
            self.last_error = exc.as_dict()
            self.snapshot_meta = None
            return
        if snapshot is None:
            self.snapshot_meta = None
            return
        self.snapshot_meta = {
            "path": str(self.service.store.snapshot_path),
            "generation": snapshot["generation"],
            "previous_generation": snapshot["previous_generation"],
            "snapshot_checksum": snapshot["snapshot_checksum"],
            "component_versions": {
                name: wrapper["version"]
                for name, wrapper in snapshot["components"].items()
            },
            "component_checksums": {
                name: wrapper["checksum"]
                for name, wrapper in snapshot["components"].items()
            },
        }

    def state(self) -> dict[str, Any]:
        artifact = self.last_artifact
        jobs = artifact["jobs"] if artifact else self._records_quietly()
        return {
            "status": artifact["status"] if artifact else "idle",
            "code": artifact["code"] if artifact else (
                self.last_error["code"] if self.last_error else "OK"
            ),
            "jobs": jobs,
            "jobs_text": self.jobs_text,
            "budget": self.default_budget.to_dict(),
            "scheduler_config": self.default_scheduler.to_dict(),
            "tolerance": self.default_tolerance.to_dict(),
            "artifact": artifact,
            "recent_errors": list(self.service.recent_errors),
            "snapshot": self.snapshot_meta,
            "last_error": self.last_error,
        }

    def _records_quietly(self) -> list[dict[str, Any]]:
        records = []
        for line in self.jobs_text.splitlines():
            if line.strip():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    return []
                if isinstance(value, dict):
                    records.append(value)
        return records

    def _parse_jobs(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for line_no, line in enumerate(self.jobs_text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DomainError(
                    ErrorCode.E_INVALID_JOB_FIELD,
                    f"JSONL line {line_no} is invalid: {exc.msg}",
                    field="jobs_text",
                ) from exc
            if not isinstance(value, dict):
                raise DomainError(
                    ErrorCode.E_INVALID_JOB_FIELD,
                    f"JSONL line {line_no} must contain an object",
                    field="jobs_text",
                )
            records.append(value)
        return records

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.run_lock.acquire(blocking=False):
            raise DomainError(
                ErrorCode.E_STAGE_BUSY,
                "another interactive run owns the stage",
            )
        try:
            mode = str(payload.get("mode", "normal"))
            if mode == "budget-conflict":
                artifact = run_budget_conflict_demo(self.service)
                self.jobs_text = "\n".join(
                    canonical_json(job) for job in artifact["jobs"]
                ) + "\n"
            elif mode == "fault-attribution":
                artifact = run_fault_attribution_demo(self.service)
                self.jobs_text = "\n".join(
                    canonical_json(job) for job in artifact["jobs"]
                ) + "\n"
            else:
                jobs_text = payload.get("jobs_text")
                if isinstance(jobs_text, str):
                    self.jobs_text = jobs_text
                records = self._parse_jobs()
                budget = Budget(
                    mac_budget=int(
                        payload.get("mac_budget", self.default_budget.mac_budget)
                    ),
                    memory_budget_bytes=int(
                        payload.get(
                            "memory_budget_bytes",
                            self.default_budget.memory_budget_bytes,
                        )
                    ),
                    worker_slots=int(
                        payload.get(
                            "worker_slots", self.default_budget.worker_slots
                        )
                    ),
                )
                scheduler = SchedulerConfig(
                    max_workers_per_task=int(
                        payload.get(
                            "max_workers_per_task",
                            self.default_scheduler.max_workers_per_task,
                        )
                    ),
                    overflow_protection=self.default_scheduler.overflow_protection,
                    max_segment_terms=self.default_scheduler.max_segment_terms,
                    allow_coarse_reduced_reduction=(
                        self.default_scheduler.allow_coarse_reduced_reduction
                    ),
                )
                tolerance = ToleranceConfig(
                    atol=float(payload.get("atol", self.default_tolerance.atol)),
                    rtol=float(payload.get("rtol", self.default_tolerance.rtol)),
                )
                self.default_budget = budget
                self.default_scheduler = scheduler
                self.default_tolerance = tolerance
                artifact = self.service.run_records(
                    records,
                    budget,
                    scheduler,
                    tolerance,
                    ExecutionConfig(max_processes=budget.worker_slots),
                )
            self.default_budget = Budget.from_dict(artifact["budget"])
            self.default_scheduler = SchedulerConfig.from_dict(
                artifact.get("scheduler_config")
            )
            self.default_tolerance = ToleranceConfig.from_dict(
                artifact.get("tolerance")
            )
            self.last_artifact = artifact
            self.last_error = artifact.get("snapshot_error")
            self._refresh_snapshot_meta()
            return {
                "ok": artifact["status"]
                not in {"validation_failed", "inconsistent"}
                and not bool(artifact.get("snapshot_error")),
                "code": artifact["code"],
                "state": self.state(),
            }
        except DomainError as exc:
            self.last_error = exc.as_dict()
            self.service._remember(exc.as_issue())
            return {
                "ok": False,
                "code": str(exc.code),
                "error": exc.as_dict(),
                "state": self.state(),
            }
        finally:
            self.run_lock.release()

    def clean(self) -> dict[str, Any]:
        if not self.run_lock.acquire(blocking=False):
            raise DomainError(ErrorCode.E_STAGE_BUSY, "a run is currently active")
        try:
            result = self.service.clean()
            self.last_artifact = None
            self.last_error = None
            self.snapshot_meta = None
            return {"ok": True, "cleaned": result, "state": self.state()}
        finally:
            self.run_lock.release()

    def snapshot(self) -> dict[str, Any]:
        try:
            value = self.service.load_snapshot()
        except DomainError as exc:
            return {"ok": False, "code": str(exc.code), "error": exc.as_dict()}
        if value is None:
            return {"ok": False, "code": ErrorCode.E_NOT_FOUND.value}
        return {"ok": True, "snapshot": value}


class Handler(BaseHTTPRequestHandler):
    server_version = "QConvLab/0.1"

    @property
    def app(self) -> Application:
        return self.server.app  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, text: str) -> None:
        data = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 4_000_000:
            raise DomainError(ErrorCode.E_INVALID_JOB_FIELD, "request body is too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DomainError(ErrorCode.E_INVALID_JOB_FIELD, "request JSON is invalid") from exc
        if not isinstance(value, dict):
            raise DomainError(ErrorCode.E_INVALID_JOB_FIELD, "request JSON must be an object")
        return value

    def do_GET(self) -> None:
        try:
            if self.path in {"/", "/index.html"}:
                self._send_html(HTML)
            elif self.path == "/api/state":
                self._send_json(HTTPStatus.OK, self.app.state())
            elif self.path == "/api/snapshot":
                result = self.app.snapshot()
                self._send_json(
                    HTTPStatus.OK if result["ok"] else HTTPStatus.CONFLICT, result
                )
            elif self.path == "/healthz":
                self._send_json(HTTPStatus.OK, {"ok": True})
            else:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "code": ErrorCode.E_NOT_FOUND.value},
                )
        except DomainError as exc:
            self._send_json(HTTPStatus.CONFLICT, _error_payload(exc))

    def do_POST(self) -> None:
        try:
            payload = self._read_json()
            if self.path == "/api/run":
                result = self.app.run(payload)
                status = HTTPStatus.OK
                if not result["ok"]:
                    code = result.get("code")
                    status = (
                        HTTPStatus.CONFLICT
                        if code in {
                            ErrorCode.E_STAGE_BUSY.value,
                            ErrorCode.E_SNAPSHOT_CORRUPT.value,
                        }
                        else HTTPStatus.UNPROCESSABLE_ENTITY
                    )
                self._send_json(status, result)
            elif self.path == "/api/clean":
                self._send_json(HTTPStatus.OK, self.app.clean())
            else:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "code": ErrorCode.E_NOT_FOUND.value},
                )
        except DomainError as exc:
            status = (
                HTTPStatus.CONFLICT
                if exc.code == ErrorCode.E_STAGE_BUSY
                else HTTPStatus.UNPROCESSABLE_ENTITY
            )
            self._send_json(status, _error_payload(exc))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[qconv-web] {self.address_string()} {fmt % args}")


def serve(
    app: Application,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    server.app = app  # type: ignore[attr-defined]
    print(f"QConv Budget Lab running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QConv Budget Lab</title>
<style>
:root {
  --bg:#0c1117; --panel:#151d27; --panel2:#1b2633; --line:#293849;
  --text:#e7eef7; --muted:#8da2b8; --accent:#50d3a7; --accent2:#78a9ff;
  --warn:#ffcb6b; --bad:#ff7b86; --good:#6ee7b7;
}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(circle at 20% 0%,#172538 0,var(--bg) 42%);color:var(--text);
font-family:"Segoe UI",system-ui,-apple-system,sans-serif;font-size:14px}
header{padding:28px 34px 18px;display:flex;align-items:flex-end;justify-content:space-between;gap:20px;
border-bottom:1px solid var(--line);background:rgba(10,15,22,.65);backdrop-filter:blur(8px)}
h1{margin:0;font-size:25px;letter-spacing:.2px}
header p{margin:5px 0 0;color:var(--muted)}
.status{padding:8px 13px;border-radius:999px;border:1px solid var(--line);background:var(--panel);
font-family:ui-monospace,Consolas,monospace;white-space:nowrap}
.status.ok{border-color:#245b4a;color:var(--good)}
.status.bad{border-color:#633039;color:var(--bad)}
.status.warn{border-color:#5c4b25;color:var(--warn)}
main{padding:22px 34px 60px;max-width:1800px;margin:0 auto}
.grid{display:grid;grid-template-columns:minmax(380px,1fr) minmax(520px,1.45fr);gap:18px}
.panel{background:linear-gradient(180deg,rgba(27,38,51,.94),rgba(20,29,39,.96));
border:1px solid var(--line);border-radius:14px;padding:17px 18px;margin-bottom:18px;
box-shadow:0 14px 34px rgba(0,0,0,.18)}
.panel h2{margin:0 0 13px;font-size:15px;letter-spacing:.4px;color:#f4f8fc}
.panel h2 span{font-weight:400;color:var(--muted);margin-left:8px;font-size:12px}
textarea{width:100%;min-height:250px;resize:vertical;background:#0d141d;color:#d8e6f5;
border:1px solid var(--line);border-radius:9px;padding:12px;font:12px/1.55 ui-monospace,Consolas,monospace}
input{width:100%;background:#0d141d;color:var(--text);border:1px solid var(--line);
border-radius:8px;padding:9px 10px;font-family:ui-monospace,Consolas,monospace}
label{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.7px;margin-bottom:5px}
.fields{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:13px 0}
.fields.two{grid-template-columns:repeat(2,1fr)}
.buttons{display:flex;flex-wrap:wrap;gap:9px;margin-top:12px}
button{cursor:pointer;border-radius:9px;padding:10px 14px;border:1px solid #34536e;
background:#1c3347;color:#dcecff;font-weight:600}
button:hover{background:#24435c}
button.primary{background:#1d6d57;border-color:#2c9d7c;color:#effff9}
button.primary:hover{background:#238469}
button.danger{background:#40232a;border-color:#6d3440;color:#ffdbe0}
button:disabled{opacity:.48;cursor:wait}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:8px 7px;border-bottom:1px solid #223141;vertical-align:top}
th{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.7px;font-weight:600}
td.mono,th.mono{font-family:ui-monospace,Consolas,monospace}
.badge{display:inline-block;border-radius:999px;padding:3px 8px;font:11px ui-monospace,Consolas,monospace;
background:#223142;color:#b9cde1;white-space:nowrap}
.badge.ok{background:#173c32;color:var(--good)}
.badge.bad{background:#46252b;color:var(--bad)}
.badge.warn{background:#44391f;color:var(--warn)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:10px}
.metric{background:#0f1822;border:1px solid #243547;border-radius:10px;padding:11px}
.metric b{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.7px}
.metric span{display:block;margin-top:5px;font:15px ui-monospace,Consolas,monospace;color:#e9f6ff}
.error-list{display:grid;gap:8px}
.error-item{border-left:3px solid var(--bad);background:#211820;padding:10px 12px;border-radius:0 9px 9px 0}
.error-item code{color:#ff9da6}
.subtle{color:var(--muted)}
.job-card{border:1px solid #26384a;border-radius:11px;padding:12px;margin-top:10px;background:#101923}
.job-card h3{margin:0 0 8px;font-size:13px;display:flex;justify-content:space-between;gap:8px}
.scroll{overflow:auto;max-width:100%}
pre{margin:0;white-space:pre-wrap;word-break:break-word;background:#0d141d;border:1px solid #213141;
border-radius:9px;padding:12px;color:#b7cfdf;font:11px/1.55 ui-monospace,Consolas,monospace;max-height:360px;overflow:auto}
.empty{color:var(--muted);padding:12px 0}
@media (max-width:1100px){.grid{grid-template-columns:1fr}header,main{padding-left:18px;padding-right:18px}}
</style>
</head>
<body>
<header>
  <div>
    <h1>QConv Budget Lab</h1>
    <p>int8 量化卷积 · 分块并行调度 · 三预算裁决 · 误差谱诊断</p>
  </div>
  <div id="run-status" class="status">IDLE</div>
</header>
<main>
  <div class="grid">
    <section class="panel">
      <h2>领域数据编辑 <span>JSONL · 每行一个算子任务</span></h2>
      <textarea id="jobs-text" spellcheck="false"></textarea>
      <div class="fields">
        <div><label>MAC budget</label><input id="mac-budget" type="number"></div>
        <div><label>Memory budget (B)</label><input id="memory-budget" type="number"></div>
        <div><label>Worker slots</label><input id="worker-slots" type="number"></div>
      </div>
      <div class="fields two">
        <div><label>Max workers / task</label><input id="max-workers" type="number"></div>
        <div><label>ATOL</label><input id="atol" type="number" step="0.001"></div>
      </div>
      <div class="buttons">
        <button id="run-normal" class="primary">执行编辑后的任务</button>
        <button id="run-budget">预算冲突演示</button>
        <button id="run-fault">异常归因演示</button>
        <button id="clean" class="danger">清理快照/缓存</button>
      </div>
      <p class="subtle" style="margin:12px 0 0">演示按钮使用内置确定性样本，不会访问网络。</p>
    </section>

    <section class="panel">
      <h2>运行摘要 <span id="summary-meta"></span></h2>
      <div id="summary" class="cards"></div>
      <div id="recent-errors" class="error-list" style="margin-top:13px"></div>
    </section>
  </div>

  <section class="panel">
    <h2>任务清单与准入结论</h2>
    <div class="scroll"><table id="jobs-table"></table></div>
  </section>

  <div class="grid">
    <section class="panel">
      <h2>调度计划</h2>
      <div class="scroll"><table id="plan-table"></table></div>
    </section>
    <section class="panel">
      <h2>预算账本</h2>
      <div id="ledger-metrics" class="cards"></div>
      <div class="scroll" style="margin-top:12px"><table id="ledger-table"></table></div>
    </section>
  </div>

  <section class="panel">
    <h2>量化误差谱 <span>float64 反量化卷积为参考真值</span></h2>
    <div id="spectrum"></div>
  </section>

  <div class="grid">
    <section class="panel">
      <h2>最近业务错误码</h2>
      <div id="errors" class="error-list"></div>
    </section>
    <section class="panel">
      <h2>版本化快照</h2>
      <div id="snapshot"></div>
    </section>
  </div>
</main>
<script>
const $ = id => document.getElementById(id);
let currentState = null;

function node(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined && text !== null) n.textContent = String(text);
  if (cls) n.className = cls;
  return n;
}
function clear(n){ while(n.firstChild) n.removeChild(n.firstChild); }
function badge(text, cls) { return node('span', text, 'badge ' + (cls || '')); }
function metric(label, value) {
  const d = node('div', null, 'metric');
  d.appendChild(node('b', label));
  d.appendChild(node('span', value));
  return d;
}
function table(target, headers, rows, monoCols = []) {
  clear(target);
  const thead = document.createElement('thead');
  const htr = document.createElement('tr');
  headers.forEach((h, i) => htr.appendChild(node('th', h, monoCols.includes(i) ? 'mono' : '')));
  thead.appendChild(htr);
  const tbody = document.createElement('tbody');
  if (!rows.length) {
    const tr = document.createElement('tr');
    const td = node('td', '暂无数据', 'empty'); td.colSpan = headers.length;
    tr.appendChild(td); tbody.appendChild(tr);
  }
  rows.forEach(row => {
    const tr = document.createElement('tr');
    row.forEach((cell, i) => {
      const td = document.createElement('td');
      if (cell instanceof HTMLElement) td.appendChild(cell);
      else td.textContent = cell === null || cell === undefined ? '—' : String(cell);
      if (monoCols.includes(i)) td.className = 'mono';
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  target.append(thead, tbody);
}
function statusClass(code, status) {
  if (status === 'ok' || status === 'idle') return 'ok';
  if (status === 'partial_failure' || status === 'budget_rejected' || status === 'no_admission' || status === 'ok_with_rejections') return 'warn';
  if (status === 'tolerance_exceeded' || status === 'snapshot_error') return 'warn';
  return 'bad';
}
function fmt(v, digits=6) {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'number') {
    if (!Number.isFinite(v)) return String(v);
    const a = Math.abs(v);
    if (a !== 0 && (a < 1e-4 || a >= 1e6)) return v.toExponential(3);
    return Number(v.toFixed(digits)).toString();
  }
  return String(v);
}
function setInputs(state) {
  $('jobs-text').value = state.jobs_text || '';
  $('mac-budget').value = state.budget.mac_budget;
  $('memory-budget').value = state.budget.memory_budget_bytes;
  $('worker-slots').value = state.budget.worker_slots;
  $('max-workers').value = state.scheduler_config.max_workers_per_task;
  $('atol').value = state.tolerance.atol;
}

function render(state) {
  currentState = state;
  setInputs(state);
  const a = state.artifact;
  const status = state.status || 'idle';
  const code = state.code || 'OK';
  $('run-status').textContent = code + ' · ' + status.toUpperCase();
  $('run-status').className = 'status ' + statusClass(code, status);
  $('summary-meta').textContent = a ? `run=${a.run_status} · snapshot=${a.snapshot ? 'v' + a.snapshot.generation : 'none'}` : '尚未执行';

  clear($('summary'));
  if (a) {
    $('summary').appendChild(metric('状态', a.status));
    $('summary').appendChild(metric('业务码', a.code));
    $('summary').appendChild(metric('准入任务', (a.plan?.admitted_job_ids || []).length));
    $('summary').appendChild(metric('Worker 槽位', a.plan?.worker_slots_used ?? 0));
    $('summary').appendChild(metric('Chunk 成功/失败', `${a.execution?.chunks_ok ?? 0}/${a.execution?.chunks_failed ?? 0}`));
    $('summary').appendChild(metric('自检', a.self_check?.ok ? 'PASS' : 'FAIL'));
  } else {
    $('summary').appendChild(metric('状态', 'idle'));
    $('summary').appendChild(metric('任务数', (state.jobs || []).length));
    $('summary').appendChild(metric('预算', `${state.budget.mac_budget} MAC`));
  }

  const decisionById = {};
  (a?.plan?.decisions || []).forEach(d => decisionById[d.job_id] = d);
  const jobs = a?.jobs || state.jobs || [];
  const rows = jobs.map(j => {
    const d = decisionById[j.job_id];
    const statusBadge = d ? badge(d.code, d.status === 'admitted' ? 'ok' : 'bad') : badge('—');
    return [
      j.job_id, JSON.stringify(j.input_shape), JSON.stringify(j.kernel_shape),
      String(j.quant_scale), j.zero_point, j.priority, statusBadge
    ];
  });
  table($('jobs-table'), ['Job ID','Input N,C,H,W','Kernel K,Ck,Kh,Kw','Scale','ZP','Priority','准入'], rows, [0,1,2]);

  const planRows = (a?.plan?.admitted || []).map(p => [
    p.job_id,
    p.accumulator.merge_mode,
    p.worker_count,
    `${p.tile_k}/${p.tile_oh}/${p.tile_ow}`,
    p.chunk_count,
    p.estimated_rounds,
    p.memory.resident_bytes,
    p.reduction.segmented ? 'yes' : 'no',
    p.tradeoffs.map(t => t.code).join(', ')
  ]);
  table($('plan-table'), ['Job','归约路径','Workers','Tile K/H/W','Chunks','Rounds','Memory B','分段','取舍记录'], planRows, [0,1,3,8]);

  clear($('ledger-metrics'));
  const ledger = a?.ledger;
  if (ledger) {
    $('ledger-metrics').appendChild(metric('MAC 使用/上限', `${ledger.usage.mac_committed} / ${ledger.limits.mac_budget}`));
    $('ledger-metrics').appendChild(metric('内存使用/上限', `${ledger.usage.memory_committed_bytes} / ${ledger.limits.memory_budget_bytes}`));
    $('ledger-metrics').appendChild(metric('Worker 使用/上限', `${ledger.usage.workers_committed} / ${ledger.limits.worker_slots}`));
    $('ledger-metrics').appendChild(metric('核对', ledger.reconciled ? 'PASS' : 'FAIL'));
  }
  const ledgerRows = (ledger?.entries || []).map(e => [
    e.job_id, badge(e.code, e.status === 'admitted' ? 'ok' : 'bad'),
    e.mac_debit, e.memory_debit_bytes, e.worker_debit
  ]);
  table($('ledger-table'), ['Job','结论','MAC 扣减','内存扣减','Worker 扣减'], ledgerRows, [0,2,3,4]);

  clear($('spectrum'));
  const spectra = a?.error_spectrum?.jobs || [];
  if (!spectra.length) $('spectrum').appendChild(node('div', '暂无误差谱（失败/被拒任务不会产生输出谱）', 'empty'));
  spectra.forEach(job => {
    const card = node('div', null, 'job-card');
    const title = node('h3');
    title.appendChild(node('span', job.job_id));
    title.appendChild(badge(job.tolerance_ok ? 'TOLERANCE OK' : 'E_TOLERANCE_EXCEEDED', job.tolerance_ok ? 'ok' : 'bad'));
    card.appendChild(title);
    const stageNames = {
      quantized_input:'量化输入', fixed_mac:'定点乘加',
      accumulator_reduction:'累加器归约', dequantized_output:'反量化输出'
    };
    const t = document.createElement('table');
    const rows = job.stages.map(s => [
      stageNames[s.stage] || s.stage,
      fmt(s.max_abs_error), fmt(s.max_relative_error),
      fmt(s.abs_error_quantiles.p99), fmt(s.amplification_vs_previous),
      s.stage === job.dominant_stage ? badge('主导', 'warn') : ''
    ]);
    card.appendChild(t);
    table(t, ['阶段','Max |Δ|','Max rel','P99 |Δ|','放大比','归因'], rows);
    const info = node('p', `主导来源: ${stageNames[job.dominant_stage] || job.dominant_stage} · 越界元素: ${job.violation_count}`, 'subtle');
    card.appendChild(info);
    if (job.violation_count) {
      const v = job.violations[0];
      card.appendChild(node('pre', `first_violation: ${JSON.stringify(v)}\n` + JSON.stringify(job.dominant_trace?.slice(0, 3), null, 2)));
    }
    $('spectrum').appendChild(card);
  });

  clear($('errors'));
  const errors = (a?.business_errors || []);
  if (!errors.length) $('errors').appendChild(node('div', '无业务错误', 'empty'));
  errors.slice(0, 20).forEach(e => {
    const item = node('div', null, 'error-item');
    item.appendChild(node('div', `${e.code}${e.job_id ? ' · ' + e.job_id : ''}${e.chunk_id !== null && e.chunk_id !== undefined ? ' · chunk ' + e.chunk_id : ''}${e.stage ? ' · ' + e.stage : ''}`));
    item.appendChild(node('div', e.message, 'subtle'));
    $('errors').appendChild(item);
  });

  clear($('recent-errors'));
  const recent = (state.recent_errors || []).slice(-4).reverse();
  recent.forEach(e => {
    const item = node('div', null, 'error-item');
    item.appendChild(node('div', `${e.code}${e.job_id ? ' · ' + e.job_id : ''}`));
    item.appendChild(node('div', e.message, 'subtle'));
    $('recent-errors').appendChild(item);
  });

  clear($('snapshot'));
  const snap = state.snapshot;
  if (snap) {
    $('snapshot').appendChild(node('pre', JSON.stringify(snap, null, 2)));
  } else {
    $('snapshot').appendChild(node('div', '尚未生成快照', 'empty'));
  }
}

async function loadState() {
  const r = await fetch('/api/state');
  const state = await r.json();
  render(state);
}
async function run(mode) {
  const buttons = document.querySelectorAll('button');
  buttons.forEach(b => b.disabled = true);
  try {
    const payload = {
      mode,
      jobs_text: $('jobs-text').value,
      mac_budget: Number($('mac-budget').value),
      memory_budget_bytes: Number($('memory-budget').value),
      worker_slots: Number($('worker-slots').value),
      max_workers_per_task: Number($('max-workers').value),
      atol: Number($('atol').value)
    };
    const r = await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    const result = await r.json();
    if (result.state) render(result.state);
    if (!result.ok) alert(result.code + ': ' + (result.error?.message || 'run failed'));
  } catch (e) {
    alert('run failed: ' + e);
  } finally {
    buttons.forEach(b => b.disabled = false);
  }
}
async function clean() {
  if (!confirm('确认清理版本化快照与缓存？')) return;
  const r = await fetch('/api/clean', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
  const result = await r.json();
  if (result.state) render(result.state);
}
$('run-normal').addEventListener('click', () => run('normal'));
$('run-budget').addEventListener('click', () => run('budget-conflict'));
$('run-fault').addEventListener('click', () => run('fault-attribution'));
$('clean').addEventListener('click', clean);
loadState().catch(e => { $('run-status').textContent = 'LOAD FAILED'; });
</script>
</body>
</html>
"""

