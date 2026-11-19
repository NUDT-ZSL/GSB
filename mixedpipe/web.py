"""Dependency-free local interactive dashboard and JSON API."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import BusinessError
from .models import load_config, load_manifest
from .pipeline import Pipeline
from .samples import ensure_demo
from .snapshots import SnapshotStore
from .stage import ErrorJournal
from .validator import Validator


def _error_payload(error: BusinessError) -> dict[str, Any]:
    return {
        "ok": False,
        **error.to_dict(),
    }


def build_state(
    root: str | Path,
    manifest_path: str | Path,
    config_path: str | Path,
) -> dict[str, Any]:
    root_path = Path(root)
    manifest = Path(manifest_path)
    config = Path(config_path)
    store = SnapshotStore(root_path / "data" / "runtime" / "snapshots")
    journal = ErrorJournal(root_path / "data" / "runtime")
    state: dict[str, Any] = {
        "root": str(root_path),
        "manifest_path": str(manifest),
        "config_path": str(config),
        "manifest": [],
        "manifest_error": None,
        "plan": None,
        "ledger": None,
        "results": None,
        "snapshot_error": None,
        "snapshot_versions": {},
        "business_errors": journal.recent(30),
    }

    try:
        state["manifest"] = [task.to_dict() for task in load_manifest(manifest)]
    except BusinessError as exc:
        state["manifest_error"] = exc.to_dict()

    try:
        envelopes = store.load_all()
        state["snapshot_versions"] = {
            category: (
                {
                    "version": envelope.version,
                    "checksum": envelope.checksum,
                }
                if envelope is not None
                else None
            )
            for category, envelope in envelopes.items()
        }
        if envelopes["plan"] is not None:
            state["plan"] = envelopes["plan"].payload
        if envelopes["ledger"] is not None:
            state["ledger"] = envelopes["ledger"].payload
        if envelopes["results"] is not None:
            state["results"] = envelopes["results"].payload
    except BusinessError as exc:
        state["snapshot_error"] = exc.to_dict()

    plan = state["plan"]
    if isinstance(plan, dict):
        for decision in plan.get("decisions", []):
            if decision.get("decision") == "rejected":
                state["business_errors"].append(
                    {
                        "business_code": decision["business_code"],
                        "stage": decision["stage"],
                        "message": decision["reason"],
                        "details": {
                            "job_id": decision["job_id"],
                            "blocking_constraints": decision.get(
                                "blocking_constraints", []
                            ),
                        },
                        "source": "schedule_decision",
                    }
                )
    if isinstance(state["results"], dict):
        for result in state["results"].get("results", []):
            if result.get("status") == "error":
                state["business_errors"].append(
                    {
                        "business_code": result["business_code"],
                        "stage": result["stage"],
                        "message": result["message"],
                        "details": {
                            "job_id": result["job_id"],
                            **result.get("details", {}),
                        },
                        "source": "worker_result",
                    }
                )
    state["business_errors"] = state["business_errors"][-30:]
    return state


class DashboardHandler(BaseHTTPRequestHandler):
    root: Path
    manifest_path: Path
    config_path: Path

    def log_message(self, format_string: str, *args: Any) -> None:
        # Keep stdout quiet; errors are exposed in the business error panel.
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, text: str) -> None:
        raw = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_html(DASHBOARD_HTML)
            return
        if path == "/api/state":
            self._send_json(
                HTTPStatus.OK,
                build_state(self.root, self.manifest_path, self.config_path),
            )
            return
        if path == "/api/health":
            self._send_json(HTTPStatus.OK, {"ok": True, "service": "mixedpipe"})
            return
        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"ok": False, "business_code": "E_NOT_FOUND"},
        )

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/run":
            try:
                ensure_demo(self.root)
                tasks = load_manifest(self.manifest_path)
                config = load_config(self.config_path)
                result = Pipeline(self.root).run(tasks, config)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "stage_version": result["stage_version"],
                        "snapshots": result["snapshots"],
                        "totals": result["plan"]["totals"],
                        "rejection_counts": result["plan"]["rejection_counts"],
                    },
                )
            except BusinessError as exc:
                status = (
                    HTTPStatus.CONFLICT
                    if exc.code == "E_STAGE_BUSY"
                    else HTTPStatus.BAD_REQUEST
                )
                self._send_json(status, _error_payload(exc))
            except Exception as exc:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "ok": False,
                        "business_code": "E_DASHBOARD_INTERNAL",
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                )
            return
        if path == "/api/validate":
            try:
                result = Validator(
                    self.root,
                    self.manifest_path,
                    self.config_path,
                ).validate()
                self._send_json(HTTPStatus.OK, result)
            except BusinessError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, _error_payload(exc))
            return
        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"ok": False, "business_code": "E_NOT_FOUND"},
        )


def serve(
    root: str | Path,
    manifest_path: str | Path,
    config_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    handler = type(
        "BoundDashboardHandler",
        (DashboardHandler,),
        {
            "root": Path(root),
            "manifest_path": Path(manifest_path),
            "config_path": Path(config_path),
        },
    )
    server = ThreadingHTTPServer((host, port), handler)
    print(f"MixedPipe dashboard: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMixedPipe dashboard stopped")
    finally:
        server.server_close()


DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MixedPipe 本地调度控制台</title>
<style>
:root{color-scheme:light;--bg:#f4f6f8;--panel:#fff;--ink:#1f2933;--muted:#66788a;--line:#dbe2e8;--blue:#1769aa;--green:#16825d;--red:#b42318;--amber:#9a6700}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 ui-sans-serif,system-ui,"Segoe UI",sans-serif}
header{background:#102a43;color:#fff;padding:22px 28px}header h1{margin:0;font-size:22px}header p{margin:4px 0 0;color:#bcccdc}
main{max-width:1500px;margin:0 auto;padding:20px}.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:16px}
button{border:0;border-radius:7px;padding:9px 15px;background:var(--blue);color:#fff;font-weight:600;cursor:pointer}button.secondary{background:#fff;color:var(--ink);border:1px solid var(--line)}
button:disabled{opacity:.5;cursor:wait}.status{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}
.card,.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;box-shadow:0 1px 2px #102a4310}.card{padding:14px}.card .label{color:var(--muted);font-size:12px}.card .value{font-size:22px;font-weight:700;margin-top:3px}
.panel{margin-bottom:16px;overflow:hidden}.panel h2{font-size:15px;margin:0;padding:12px 14px;border-bottom:1px solid var(--line);background:#fafbfc}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #edf1f4;vertical-align:top}th{color:var(--muted);font-weight:600;background:#fcfdfe;position:sticky;top:0}
.scroll{max-height:430px;overflow:auto}code,.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}.badge{display:inline-block;padding:2px 7px;border-radius:99px;font-size:11px;font-weight:700}
.ok{background:#e7f6ef;color:var(--green)}.bad{background:#feeceb;color:var(--red)}.warn{background:#fff4d6;color:var(--amber)}.muted{color:var(--muted)}.empty{padding:14px;color:var(--muted)}
.checks{display:flex;flex-wrap:wrap;gap:8px;padding:12px}.check{padding:5px 9px;border-radius:6px;background:#e7f6ef;color:var(--green)}.check.bad{background:#feeceb;color:var(--red)}
@media(max-width:900px){.grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:560px){.grid{grid-template-columns:1fr}main{padding:12px}}
</style>
</head>
<body>
<header><h1>MixedPipe 本地调度控制台</h1><p>确定性训练/推理混合负载 · 三重预算 · 快照版本与串行基线复核</p></header>
<main>
<div class="toolbar">
  <button id="runBtn">运行演示</button>
  <button id="validateBtn" class="secondary">独立验证</button>
  <button id="refreshBtn" class="secondary">刷新状态</button>
  <span id="status" class="status">加载中…</span>
</div>
<div id="validation"></div>
<div class="grid" id="cards"></div>
<section class="panel"><h2>任务清单与调度裁决</h2><div class="scroll"><table id="tasks"></table></div></section>
<section class="panel"><h2>预算账本</h2><div id="ledger"></div></section>
<section class="panel"><h2>并行分组</h2><div id="groups"></div></section>
<section class="panel"><h2>任务结果摘要</h2><div class="scroll"><table id="results"></table></div></section>
<section class="panel"><h2>最近业务错误码</h2><div class="scroll"><table id="errors"></table></div></section>
</main>
<script>
const $=id=>document.getElementById(id);
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const badge=(ok,text)=>`<span class="badge ${ok?"ok":"bad"}">${esc(text)}</span>`;
function table(el,heads,rows,empty="暂无数据"){
  if(!rows.length){el.innerHTML=`<tbody><tr><td class="empty">${esc(empty)}</td></tr></tbody>`;return;}
  el.innerHTML=`<thead><tr>${heads.map(h=>`<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(c=>`<td>${c}</td>`).join("")}</tr>`).join("")}</tbody>`;
}
async function getState(){
  const r=await fetch("/api/state",{cache:"no-store"}); const s=await r.json();
  const plan=s.plan, ledger=s.ledger, results=s.results;
  const decisions=new Map((plan?.decisions||[]).map(d=>[d.job_id,d]));
  const resultMap=new Map((results?.results||[]).map(x=>[x.job_id,x]));
  const admitted=plan?.totals?.admitted??0, rejected=plan?.totals?.rejected??0;
  $("cards").innerHTML=[
    ["快照版本",plan?.stage_version??"—"],["准入 / 拒绝",`${admitted} / ${rejected}`],
    ["计算预算",plan?`${plan.totals.compute_units_used} / ${plan.totals.compute_units_limit}`:"—"],
    ["账本状态",ledger?.state??"—"]
  ].map(([l,v])=>`<div class="card"><div class="label">${esc(l)}</div><div class="value">${esc(v)}</div></div>`).join("");
  const rows=(s.manifest||[]).map(t=>{
    const d=decisions.get(t.job_id),x=resultMap.get(t.job_id);
    let decision=d?badge(d.decision==="admitted",d.decision==="admitted"?"准入":d.business_code):'<span class="muted">未调度</span>';
    if(d?.degraded) decision+=` <span class="badge warn">${esc(d.degrade_category)}</span>`;
    let result='<span class="muted">—</span>';
    if(x) result=x.status==="ok"?badge(true,"成功"):badge(false,`${x.business_code}@${x.stage}`);
    const est=d?.effective_estimate||d?.original_estimate;
    return [esc(t.job_id),esc(t.job_kind),esc(t.model_tag),esc(t.priority),decision,
      est?esc(est.compute_units):"—",est?esc(est.memory_mb):"—",result];
  });
  table($("tasks"),["job_id","类型","model_tag","优先级","裁决","计算单位","内存 MB","结果"],rows);
  if(ledger){
    const t=ledger.totals;
    $("ledger").innerHTML=`<div class="checks">
      <span class="check">计算净占用 ${esc(t.net_committed_compute_units)}</span>
      <span class="check">计算释放 ${esc(t.released_compute_units)}</span>
      <span class="check">预算 ${esc(ledger.limits.compute_units)}</span>
      <span class="check">内存净占用 ${esc(t.net_committed_memory_bytes)} B</span>
      <span class="check">内存上限 ${esc(ledger.limits.memory_bytes)} B</span>
      <span class="check">槽位峰值 ${esc(t.slot_peak)} / ${esc(t.slot_limit)}</span></div>`;
  }else $("ledger").innerHTML='<div class="empty">尚无账本快照</div>';
  const groupRows=(plan?.parallel_groups||[]).map(g=>[g.group_index,`<span class="mono">${g.job_ids.map(esc).join(" → ")}</span>`,g.slot_count,g.estimated_compute_units,g.estimated_memory_bytes]);
  table($("groups"),["#","job_ids","槽位","计算单位","内存 B"],groupRows);
  const resultRows=(results?.results||[]).map(x=>{
    if(x.status!=="ok") return [esc(x.job_id),badge(false,x.status),"—","—","—",badge(false,`${x.business_code} / ${x.stage}`)];
    const s=x.summary;
    return [esc(x.job_id),badge(true,"ok"),esc(s.loss_first),esc(s.loss_last),esc(s.grad_norm),`<span class="mono">${esc(s.prediction_checksum.slice(0,16))}…</span>`];
  });
  table($("results"),["job_id","状态","首损失","末损失","梯度范数","预测校验和"],resultRows);
  const errorRows=(s.business_errors||[]).slice().reverse().map(e=>[badge(false,e.business_code),esc(e.stage),esc(e.details?.job_id??"—"),esc(e.source),esc(e.message)]);
  table($("errors"),["业务码","阶段","job_id","来源","说明"],errorRows);
  const status=[];
  if(s.manifest_error) status.push(`清单错误 ${s.manifest_error.business_code}`);
  if(s.snapshot_error) status.push(`快照错误 ${s.snapshot_error.business_code}`);
  if(plan) status.push(`版本 ${plan.stage_version} · ${admitted} 准入 / ${rejected} 拒绝`);
  $("status").textContent=status.length?status.join(" · "):"尚未运行演示";
}
async function post(path,label){
  const btn=$(path.includes("run")?"runBtn":"validateBtn"); btn.disabled=true; $("status").textContent=label;
  try{
    const r=await fetch(path,{method:"POST"}); const data=await r.json();
    if(!data.ok && data.business_code) $("status").textContent=`${data.business_code}: ${data.message}`;
    else if(path.includes("validate")){
      $("validation").innerHTML=`<section class="panel"><h2>验证结果 ${data.ok?badge(true,"通过"):badge(false,data.business_code)}</h2><div class="checks">${(data.checks||[]).map(c=>`<span class="check ${c.ok?"":"bad"}">${esc(c.name)}</span>`).join("")}</div>${data.first_inconsistency?`<div class="empty mono">${esc(JSON.stringify(data.first_inconsistency))}</div>`:""}</section>`;
      $("status").textContent=data.ok?"验证通过":`验证失败 ${data.business_code}`;
    }else $("status").textContent=`演示完成 · 版本 ${data.stage_version} · 拒绝 ${JSON.stringify(data.rejection_counts)}`;
  }catch(e){$("status").textContent=`请求失败: ${e}`;}
  finally{btn.disabled=false; await getState();}
}
$("runBtn").onclick=()=>post("/api/run","演示运行中（spawn 进程池）…");
$("validateBtn").onclick=()=>post("/api/validate","串行基线验证中…");
$("refreshBtn").onclick=getState;
getState();
</script>
</body>
</html>
"""
