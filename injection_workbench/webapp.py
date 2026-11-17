"""本地 Flask 诊断工作台：汇点摘要、缺陷明细、规范链与规模模式。"""
from __future__ import annotations

import json
from pathlib import Path

from flask import Flask, render_template, jsonify, abort

from .checkpoints import CheckpointStore
from .config import Config
from .runner import run_batch


def create_app(samples_dir: str = "samples", report_path: str = "reports/report.json",
               checkpoint_dir: str = "checkpoints") -> Flask:
    app = Flask(__name__)
    samples = Path(samples_dir)
    report_target = Path(report_path)

    def _load_or_run() -> dict:
        if report_target.exists():
            return json.loads(report_target.read_text(encoding="utf-8"))
        manifests = sorted(samples.glob("*.json")) if samples.exists() else []
        config = Config()
        store = CheckpointStore(checkpoint_dir, config) if checkpoint_dir else None
        return run_batch(manifests, config, checkpoint_store=store, report_path=report_target)

    @app.get("/")
    def index():
        report = _load_or_run()
        return render_template("index.html", report=report)

    @app.get("/manifest/<int:position>")
    def manifest_detail(position: int):
        report = _load_or_run()
        manifests = report.get("manifests", [])
        if not 0 <= position < len(manifests):
            abort(404)
        return render_template(
            "manifest.html", report=report, item=manifests[position], position=position
        )

    @app.get("/api/report")
    def api_report():
        return jsonify(_load_or_run())

    @app.errorhandler(404)
    def not_found(_):
        return "未找到对应清单", 404

    return app
