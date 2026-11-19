"""Dependency-free local web UI."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import AppError
from .service import WorkbenchService


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EventGraph Annotation Studio</title>
<style>
:root{--bg:#0b1020;--panel:#131b31;--panel2:#18233f;--text:#dce7ff;--muted:#8fa3c7;--line:#2a3a61;--accent:#67e8f9;--ok:#52d49b;--warn:#ffcc66;--bad:#ff7b8a}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 15% 0%,#17264a 0,var(--bg) 38%);color:var(--text);font:14px/1.55 "Segoe UI",ui-sans-serif,system-ui,sans-serif}
header{padding:24px 30px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:18px;align-items:flex-end;flex-wrap:wrap}
h1{font-size:22px;margin:0;letter-spacing:.3px}h1 span{color:var(--accent)}.sub{color:var(--muted);margin-top:4px}
main{padding:20px 30px 50px;display:grid;grid-template-columns:minmax(0,1.7fr) minmax(330px,.9fr);gap:18px;max-width:1500px;margin:auto}
.panel{background:linear-gradient(160deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:12px;padding:16px;min-width:0}
.panel h2{font-size:14px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);margin:0 0 12px}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
.metric{background:#0d1529;border:1px solid var(--line);border-radius:9px;padding:11px}.metric b{display:block;font-size:19px;color:var(--accent)}.metric small{color:var(--muted)}
button,select,textarea{font:inherit}button{background:#203158;color:var(--text);border:1px solid #355184;border-radius:8px;padding:8px 13px;cursor:pointer}button:hover{border-color:var(--accent)}button.primary{background:#164e63;border-color:var(--accent)}button.danger{background:#4c1d2a;border-color:#9f3a50}
button:disabled{opacity:.45;cursor:wait}.toolbar{display:flex;gap:8px;flex-wrap:wrap}
.tabs{display:flex;gap:6px;margin-bottom:10px}.tabs button.active{background:#164e63;border-color:var(--accent);color:white}
pre{white-space:pre-wrap;word-break:break-word;background:#080d19;border:1px solid var(--line);border-radius:9px;padding:13px;max-height:430px;overflow:auto;margin:0;font:12px/1.5 Consolas,"Cascadia Mono",monospace}
.split{display:grid;grid-template-columns:1fr 1fr;gap:14px}.list{max-height:250px;overflow:auto;border:1px solid var(--line);border-radius:8px}
.row{padding:8px 10px;border-bottom:1px solid #20304f;display:flex;justify-content:space-between;gap:10px;align-items:center}.row:last-child{border-bottom:0}.tag{font:11px/1.4 Consolas,monospace;padding:2px 7px;border-radius:999px;background:#213255;color:var(--accent)}.tag.ok{background:#103c2d;color:var(--ok)}.tag.bad{background:#491d28;color:var(--bad)}.tag.warn{background:#473817;color:var(--warn)}
.muted{color:var(--muted)}.mono{font-family:Consolas,"Cascadia Mono",monospace}.stack{display:grid;gap:14px}.field{display:grid;gap:6px}.field label{color:var(--muted);font-size:12px}select,textarea{width:100%;background:#080d19;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:9px}textarea{min-height:145px;font:12px/1.5 Consolas,monospace;resize:vertical}
#notice{padding:10px 12px;border-radius:8px;border:1px solid var(--line);background:#0b1427;margin-bottom:12px;min-height:40px}.error{color:var(--bad)}a{color:var(--accent)}
@media(max-width:980px){main{grid-template-columns:1fr}.grid{grid-template-columns:1fr 1fr}.split{grid-template-columns:1fr}}
</style>
</head>
<body>
<header>
  <div><h1>EventGraph <span>Annotation Studio</span></h1><div class="sub">本地确定性关系/事件抽取标注工作台 · 无外部服务</div></div>
  <div class="toolbar"><button class="primary" id="demoBtn">运行完整演示</button><button id="resetBtn">重置编辑状态</button></div>
</header>
<main>
  <section class="stack">
    <div class="panel">
      <h2>状态概览</h2>
      <div id="notice">正在加载本地状态...</div>
      <div class="grid" id="metrics"></div>
    </div>
    <div class="panel">
      <h2>格式视图</h2>
      <div class="tabs" id="tabs"><button data-view="v1">v1</button><button data-view="v2" class="active">v2</button><button data-view="line">行式</button></div>
      <pre id="viewBox"></pre>
    </div>
    <div class="panel">
      <h2>事件图</h2>
      <div class="split"><div><div class="muted">节点</div><pre id="nodesBox"></pre></div><div><div class="muted">边</div><pre id="edgesBox"></pre></div></div>
    </div>
    <div class="panel">
      <h2>迁移往返报告</h2>
      <pre id="migrationBox">尚未运行演示。</pre>
    </div>
  </section>
  <aside class="stack">
    <div class="panel">
      <h2>编辑操作</h2>
      <div class="field"><label>操作</label><select id="opSelect"></select></div>
      <div class="field"><label>JSON payload</label><textarea id="payloadBox"></textarea></div>
      <div class="toolbar" style="margin-top:8px"><button class="primary" id="applyBtn">应用事务</button></div>
    </div>
    <div class="panel"><h2>审计日志</h2><div class="list" id="auditList"></div></div>
    <div class="panel"><h2>最近业务错误码</h2><div class="list" id="errorList"></div></div>
    <div class="panel"><h2>受控集合</h2><pre id="ontologyBox"></pre></div>
  </aside>
</main>
<script>
const operations={
 add_entity:{doc_id:"doc-1",id:"e8",type:"DATE",start:2,end:7,text:"二〇二四年",canonical_id:"date:2024"},
 delete_entity:{entity_id:"e7"},
 add_argument:{id:"a8",trigger_id:"t1",entity_id:"e7",role:"time"},
 delete_argument:{argument_id:"a7"},
 change_argument_role:{argument_id:"a7",role:"time"},
 split_event:{event_id:"evt-hire-1",trigger_ids:["t2"],new_event_id:"evt-split"},
 merge_events:{event_ids:["evt-hire-1","evt-split"],new_event_id:"evt-merged"}
};
let activeView="v2", latestState=null;
const $=id=>document.getElementById(id);
function esc(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function pretty(x){return JSON.stringify(x,null,2)}
function showNotice(text,cls=""){const n=$("notice");n.className=cls;n.textContent=text}
function render(){
  const s=latestState;if(!s)return;
  $("metrics").innerHTML=[
    ["格式版本",s.current_format,""],
    ["受控集合",`v${s.ontology.version}`,""],
    ["图节点",s.graph_stats.nodes,""],
    ["图边",s.graph_stats.edges,""],
    ["业务错误",s.recent_errors.length,s.recent_errors.length?"bad":"ok"]
  ].map(([k,v,c])=>`<div class="metric"><b>${esc(v)}</b><small>${esc(k)}</small></div>`).join("");
  const content=s.view_content;
  $("viewBox").textContent=typeof content==="string"?content:pretty(content);
  $("nodesBox").textContent=pretty(s.graph_view.nodes);
  $("edgesBox").textContent=pretty(s.graph_view.edges);
  $("migrationBox").textContent=s.migration_report?pretty(s.migration_report):"尚未运行演示。";
  $("ontologyBox").textContent=pretty(s.ontology);
  $("auditList").innerHTML=s.audit.length?s.audit.map(e=>`<div class="row"><span>#${e.seq} ${esc(e.operation)}</span><span class="tag ${e.status==="applied"?"ok":"bad"}">${esc(e.status)}</span></div>`).join(""):'<div class="row muted">暂无编辑</div>';
  $("errorList").innerHTML=s.recent_errors.length?s.recent_errors.map(e=>`<div class="row"><span class="mono">${esc(e.code)}</span><span class="tag bad">${esc((e.message||"").slice(0,34))}</span></div>`).join(""):'<div class="row muted">无业务错误</div>';
  document.querySelectorAll("#tabs button").forEach(b=>b.classList.toggle("active",b.dataset.view===activeView));
  showNotice(s.view_warning||`已加载 ${s.sample_id} · ${s.current_format} 视图 · 受控集合 v${s.ontology.version}`,"");
}
async function load(view=activeView){
  try{const r=await fetch(`/api/state?view=${view}`);const j=await r.json();if(!r.ok)throw new Error(j.error?.message||r.statusText);activeView=view;latestState=j.state;render();}
  catch(e){showNotice(String(e),"error")}
}
async function post(url,body){const r=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})});const j=await r.json();if(!r.ok)throw new Error(j.error?.message||j.detail||r.statusText);return j;}
$("tabs").onclick=e=>{if(e.target.dataset.view)load(e.target.dataset.view)};
$("demoBtn").onclick=async()=>{const b=$("demoBtn");b.disabled=true;showNotice("正在执行迁移、行式往返、编辑审计与受控集合收缩...");try{const j=await post("/api/demo");latestState=j.state;render();showNotice(`演示完成 · ${j.status} · ${j.state.recent_business_error_codes.join(", ")||"无错误"}`);}catch(e){showNotice(String(e),"error")}finally{b.disabled=false}};
$("resetBtn").onclick=async()=>{try{const j=await post("/api/reset");latestState=j.state;render();showNotice("已重置为固定种子样本")}catch(e){showNotice(String(e),"error")}};
$("applyBtn").onclick=async()=>{try{const payload=JSON.parse($("payloadBox").value);const j=await post("/api/edit",{operation:$("opSelect").value,payload});latestState=j.state;render();showNotice(j.status==="applied"?"编辑已提交并写入审计日志":`编辑被阻断: ${j.error.code}`,j.status==="applied"?"":"error")}catch(e){showNotice(String(e),"error")}};
$("opSelect").innerHTML=Object.keys(operations).map(k=>`<option>${k}</option>`).join("");
$("opSelect").onchange=()=>{$("payloadBox").value=pretty(operations[$("opSelect").value])};
$("payloadBox").value=pretty(operations.add_entity);
load("v2");
</script>
</body></html>"""


class WorkbenchHandler(BaseHTTPRequestHandler):
    service: WorkbenchService
    server_version = "EventGraphStudio/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_html(self, text: str) -> None:
        payload = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_048_576:
            raise AppError(
                "E_RESOURCE_LIMIT",
                "request body exceeds 1 MiB",
                path="$",
                details={"limit": 1_048_576, "actual": length},
            )
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppError(
                "E_FORMAT_INVALID",
                f"invalid request JSON: {exc}",
                path="$",
            ) from exc
        if not isinstance(value, dict):
            raise AppError("E_FORMAT_INVALID", "request body must be an object", path="$")
        return value

    def _handle_error(self, exc: AppError) -> None:
        self._send_json(
            HTTPStatus.CONFLICT,
            {
                "status": "rejected",
                "error": exc.to_dict(),
            },
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(INDEX_HTML)
            return
        if parsed.path == "/api/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if parsed.path == "/api/state":
            view = parse_qs(parsed.query).get("view", ["v2"])[0]
            try:
                state = self.service.state(view)
            except AppError as exc:
                self._handle_error(exc)
                return
            self._send_json(HTTPStatus.OK, {"status": "ok", "state": state})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": {"code": "E_NOT_FOUND"}})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
            if parsed.path == "/api/demo":
                result = self.service.trigger_demo()
                self._send_json(HTTPStatus.OK, result)
                return
            if parsed.path == "/api/view":
                view = body.get("view", "v2")
                state = self.service.state(view)
                self._send_json(HTTPStatus.OK, {"status": "ok", "state": state})
                return
            if parsed.path == "/api/edit":
                operation = body.get("operation")
                payload = body.get("payload")
                if not isinstance(operation, str) or not isinstance(payload, dict):
                    raise AppError(
                        "E_SCHEMA_TYPE",
                        "edit requires operation and object payload",
                        path="$",
                    )
                result = self.service.apply_edit(operation, payload)
                self._send_json(HTTPStatus.OK, result)
                return
            if parsed.path == "/api/reset":
                self.service.reset()
                self._send_json(
                    HTTPStatus.OK,
                    {"status": "reset", "state": self.service.state()},
                )
                return
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": {"code": "E_NOT_FOUND"}},
            )
        except AppError as exc:
            self._handle_error(exc)


def run_server(
    root: Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    service = WorkbenchService(root)
    handler = type(
        "BoundWorkbenchHandler",
        (WorkbenchHandler,),
        {"service": service},
    )
    server = ThreadingHTTPServer((host, port), handler)
    print(f"EventGraph Annotation Studio: http://{host}:{port}")
    print("按 Ctrl+C 停止本地进程。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

