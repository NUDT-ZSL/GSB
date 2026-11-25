from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import Flask, jsonify

from .storage import Storage


def create_app(db_path: str | Path) -> Flask:
    app = Flask(__name__)
    db_path = Path(db_path)

    @app.get("/")
    def index() -> str:
        html = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>GSB 诊断工作台</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:2rem;background:#0b1020;color:#e5e7eb}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.card{background:#111827;border:1px solid #243044;border-radius:12px;padding:1rem 1.25rem;margin-bottom:1rem}
.healthy{color:#34d399}.suspect{color:#fbbf24}.lost{color:#f87171}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #243044;padding:.45rem;text-align:left}
</style></head><body>
<h1>服务注册与健康探测诊断工作台</h1>
<div class="card">数据库：<code>%s</code> · <a href="/api/status" style="color:#93c5fd">/api/status</a></div>
<div id="app" class="card">加载中...</div>
<script>
fetch('/api/status').then(r=>r.json()).then(data=>{
 const rows=(data.instances||[]).map(x=>`<tr><td>${x.instance_id}</td><td class="${x.health_status}">${x.health_status}</td><td>${x.consecutive_failures}</td><td>${x.rule_generation}</td><td>${x.committed_through_seq}</td><td>${x.clock_watermark_ms}</td></tr>`).join('');
 document.getElementById('app').innerHTML=`<p>ledger <code>${data.ledger_hash||''}</code></p><table><thead><tr><th>实例</th><th>健康</th><th>连续失败</th><th>规则代</th><th>提交序号</th><th>时钟水位</th></tr></thead><tbody>${rows}</tbody></table>`;
}).catch(e=>{document.getElementById('app').textContent='读取账本失败：'+e});
</script></body></html>"""
        return html.replace("<code>%s</code>", f"<code>{db_path}</code>")

    @app.get("/api/status")
    def api_status() -> Any:
        with Storage(db_path) as store:
            ledger = store.get_ledger()
            if ledger is None:
                return jsonify({"ok": False, "error": {"code": "E_LEDGER_EMPTY"}}), 404
            return app.response_class(json.dumps(ledger, ensure_ascii=False, sort_keys=True), mimetype="application/json")

    @app.get("/api/versions")
    def api_versions() -> Any:
        with Storage(db_path) as store:
            return jsonify({"ok": True, "versions": store.list_ledger_versions()})

    return app
