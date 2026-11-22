from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, request

from .errors import BusinessError
from .manager import CheckpointManager


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Checkpoint Lab</title>
  <style>
    :root { color-scheme: light dark; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
    body { max-width: 1120px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
    h1 { font-size: 1.4rem; margin-bottom: .25rem; }
    .muted { opacity: .65; }
    button { margin: .25rem .5rem .5rem 0; padding: .45rem .75rem; cursor: pointer; }
    pre { padding: 1rem; overflow: auto; border: 1px solid #8886; border-radius: 8px; max-height: 70vh; }
    .row { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem; }
  </style>
</head>
<body>
  <h1>Deterministic Checkpoint Lab</h1>
  <div class="muted">只读诊断：status / verify / restore。服务仅监听回环地址。</div>
  <div class="row">
    <button onclick="load('/api/status')">status</button>
    <button onclick="load('/api/verify')">verify</button>
    <button onclick="load('/api/restore')">restore</button>
  </div>
  <pre id="output">点击按钮加载诊断结果。</pre>
  <script>
    async function load(path) {
      const out = document.getElementById('output');
      out.textContent = 'loading...';
      try {
        const response = await fetch(path, {headers: {'Accept': 'application/json'}});
        const body = await response.json();
        out.textContent = JSON.stringify(body, null, 2);
      } catch (error) {
        out.textContent = String(error);
      }
    }
    load('/api/status');
  </script>
</body>
</html>
"""


def _version_arg() -> int | None:
    raw = request.args.get("version")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise BusinessError(
            "CHECKPOINT_NOT_FOUND",
            "version 参数必须是整数",
            {"version": raw},
        ) from exc


def create_app(db_path: str | Path, store_path: str | Path) -> Flask:
    app = Flask(__name__)
    app.json.sort_keys = True
    manager = CheckpointManager(db_path, store_path)

    @app.get("/")
    def index():
        return PAGE

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "service": "ckptlab", "read_only": True})

    @app.get("/api/status")
    def status():
        return jsonify(manager.status(_version_arg()))

    @app.get("/api/verify")
    def verify():
        return jsonify(manager.verify(_version_arg()))

    @app.get("/api/restore")
    def restore():
        return jsonify(manager.restore(_version_arg()))

    @app.errorhandler(BusinessError)
    def handle_business_error(exc: BusinessError):
        response = jsonify(exc.as_dict())
        response.status_code = 409 if exc.code in {
            "VERSION_CONFLICT",
            "VERSION_ROLLBACK",
            "CLOCK_ROLLBACK",
            "BUILD_INCOMPLETE",
        } else 400
        return response

    return app
