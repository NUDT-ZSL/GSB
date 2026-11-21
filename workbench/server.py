"""本地可视化诊断工作台（仅标准库 HTTP 服务）。

展示：格式版本视图、取舍路径、失效传播覆盖范围、追溯链与最近业务错误码；
支持一键运行含跨版本迁移与改写失效的演示。用法：
    python3 -m workbench.server [--port 8791]
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from asr_gov.demo import run_demo

_STATE = {"report": None, "lock": threading.Lock()}

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>ASR 转写数据治理工作台</title>
<style>
 body{font-family:system-ui,'Segoe UI',sans-serif;margin:24px;background:#f6f7f9;color:#1c2530}
 h1{font-size:20px} h2{font-size:15px;margin:18px 0 6px;color:#33475b}
 .card{background:#fff;border:1px solid #dde3ea;border-radius:8px;padding:12px 16px;margin-bottom:14px}
 pre{background:#0f1722;color:#d7e0ea;padding:10px;border-radius:6px;overflow:auto;font-size:12px;max-height:280px}
 button{background:#1f6feb;color:#fff;border:0;border-radius:6px;padding:8px 18px;font-size:14px;cursor:pointer}
 button:disabled{background:#9db4ce}
 .ok{color:#137333;font-weight:600}.bad{color:#b3261e;font-weight:600}
 table{border-collapse:collapse;font-size:12px} td,th{border:1px solid #d5dce4;padding:4px 10px}
 .tag{display:inline-block;background:#e8f0fe;color:#1a4fa0;border-radius:4px;padding:1px 8px;margin:2px;font-size:12px}
 .err{display:inline-block;background:#fde7e9;color:#b3261e;border-radius:4px;padding:1px 8px;margin:2px;font-size:12px}
</style>
</head>
<body>
<h1>ASR 转写数据治理 · 本地诊断工作台</h1>
<p><button id="run" onclick="runDemo()">运行演示（跨版本迁移 + 改写失效）</button>
<span id="status"></span></p>
<div id="root"><p>尚未运行演示。</p></div>
<script>
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function card(title,body){return `<div class="card"><h2>${title}</h2>${body}</div>`}
function render(r){
  const s=r.steps;let h='';
  h+=card('总体结论',`<div class="${r.ok?'ok':'bad'}">${r.ok?'全部验收项通过':'存在失败项'}</div>`);
  let fv='';for(const v of ['v1','v2','v3']){fv+=`<h2>${v} 书写形式</h2><pre>${esc(s.format_views[v])}</pre>`}
  fv+=`<div>跨格式裁决：<span class="${s.adjudication.equal?'ok':'bad'}">${s.adjudication.equal?'三版本切片集合/毫秒起止/文本完全一致':'不一致'}</span></div>`;
  fv+=`<div>语义漂移检测：<span class="err">${esc(s.semantic_drift.code)}</span> 不可还原字段 ${s.semantic_drift.unrecoverable_fields.map(x=>`<span class="tag">${x}</span>`).join('')} 受影响切片 ${s.semantic_drift.affected_slices.map(x=>`<span class="tag">${x}</span>`).join('')}</div>`;
  h+=card('格式版本视图',fv);
  const sm=s.adaptive.small,lg=s.adaptive.large;
  h+=card('取舍路径',`<table><tr><th>观测</th><th>路径</th><th>帧移</th><th>边界方法</th><th>比对模式</th><th>比较次数估计</th><th>理由</th></tr>
   <tr><td>小样本 (${sm.observation.num_slices} 切片/${sm.observation.num_frames} 帧)</td><td>${sm.path}</td><td>${sm.frame_shift_ms}ms</td><td>${sm.boundary_method}</td><td>${sm.comparison_mode}</td><td>${sm.estimated_comparisons}</td><td>${sm.reasons.join('; ')}</td></tr>
   <tr><td>大样本 (${lg.observation.num_slices} 切片/${lg.observation.num_frames} 帧)</td><td>${lg.path}</td><td>${lg.frame_shift_ms}ms</td><td>${lg.boundary_method}</td><td>${lg.comparison_mode}</td><td>${lg.estimated_comparisons}</td><td>${lg.reasons.join('; ')}</td></tr></table>
   <div>决策确定性：<span class="${s.adaptive.deterministic?'ok':'bad'}">${s.adaptive.deterministic}</span></div>`);
  const inv=s.invalidation;
  h+=card('失效传播覆盖范围',`<table><tr><th>事件</th><th>受影响切片</th></tr>
   <tr><td>修正 u2 标注</td><td>${inv.edit_slice_u2.affected_slices.join(', ')}（上游分帧复用: ${inv.edit_slice_u2.reused_framing}）</td></tr>
   <tr><td>词表升级 vocab-1 → vocab-2</td><td>${inv.vocab_bump.affected_slices.join(', ')}</td></tr>
   <tr><td>帧移 10ms → 20ms</td><td>${inv.frame_shift_change.affected_slices.join(', ')}</td></tr></table>
   <div>时间轴语义保持稳定：<span class="${inv.timeline_stable?'ok':'bad'}">${inv.timeline_stable}</span>；
   过期追溯拒绝：<span class="err">${esc(inv.lineage_stale_code)}</span>（依赖：${inv.stale_dependencies.join(', ')}）；
   重放节点：${inv.replay_nodes.join(' → ')}</div>`);
  let lin='';for(const sl of inv.lineage){lin+=`<h2>${esc(sl.utterance_id)} [${sl.start_ms}ms, ${sl.end_ms}ms] 校验=${sl.validation}</h2><table><tr><th>节点</th><th>缓存键</th></tr>`;
   for(const n of sl.lineage.node_path){lin+=`<tr><td>${n}</td><td><code>${esc(sl.lineage.cache_keys[n])}</code></td></tr>`}
   lin+=`</table><div>输入版本：${esc(JSON.stringify(sl.lineage.input_versions))}</div>`}
  h+=card('追溯链（最终切片）',lin);
  h+=card('最近业务错误码',r.errors.map(e=>`<span class="err">${e}</span>`).join(''));
  h+=card('快照与资源',`<pre>${esc(JSON.stringify({snapshots:s.snapshots,validation:s.validation},null,1))}</pre>`);
  document.getElementById('root').innerHTML=h;
}
async function runDemo(){
  const btn=document.getElementById('run');btn.disabled=true;
  document.getElementById('status').textContent=' 运行中…';
  try{const resp=await fetch('/api/demo',{method:'POST'});const r=await resp.json();render(r);
    document.getElementById('status').textContent=' 完成';}
  catch(e){document.getElementById('status').textContent=' 失败: '+e}
  finally{btn.disabled=false}
}
fetch('/api/state').then(r=>r.json()).then(r=>{if(r)render(r)}).catch(()=>{});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/state":
            with _STATE["lock"]:
                report = _STATE["report"]
            self._send(200, json.dumps(report, ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not_found"}))

    def do_POST(self):
        if self.path == "/api/demo":
            report = run_demo()
            with _STATE["lock"]:
                _STATE["report"] = report
            self._send(200, json.dumps(report, ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not_found"}))

    def log_message(self, fmt, *args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"工作台已启动: http://{args.host}:{args.port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
