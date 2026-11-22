from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

from eventgraph.errors import BusinessError
from eventgraph.service import WorkbenchService


ROOT = Path(__file__).resolve().parent


def _error_status(code: str) -> int:
    if code == "E_STAGE_BUSY":
        return 409
    if code == "E_RESOURCE_LIMIT":
        return 429
    if code in {"E_CONFIG_INVALID", "E_SPAN_INVALID"}:
        return 400
    if code in {
        "E_SNAPSHOT_ROLLBACK",
        "E_SNAPSHOT_CORRUPT",
        "E_CHAIN_DUP_MEMBER",
        "E_CHAIN_MISSING",
        "E_ARBITRATION_UNRESOLVED",
    }:
        return 409
    if code == "E_INCREMENTAL_DRIFT":
        return 500
    return 500


def create_app(
    *,
    clear_runtime: bool = True,
    service: WorkbenchService | None = None,
) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
    )
    app.json.ensure_ascii = False
    app.config["JSON_SORT_KEYS"] = False
    workbench = service or WorkbenchService(ROOT, clear_runtime=clear_runtime)
    app.extensions["workbench_service"] = workbench

    @app.get("/")
    def index() -> str:
        return render_template("dashboard.html")

    @app.get("/healthz")
    def healthz() -> Any:
        return jsonify(
            {
                "ok": True,
                "bind": "127.0.0.1",
                "runtime": str(workbench.runtime),
                "documents_processed": len(workbench.engine.documents),
            }
        )

    @app.get("/api/state")
    def api_state() -> Any:
        return jsonify(workbench.state_view())

    @app.post("/api/commit-next")
    def api_commit_next() -> Any:
        return jsonify(workbench.commit_next())

    @app.post("/api/demo/conflict")
    def api_demo_conflict() -> Any:
        return jsonify(workbench.demo_conflict())

    @app.post("/api/demo/degrade")
    def api_demo_degrade() -> Any:
        return jsonify(workbench.demo_degrade())

    @app.post("/api/demo/invalidate")
    def api_demo_invalidate() -> Any:
        payload = request.get_json(silent=True) or {}
        kwargs: dict[str, Any] = {
            "reason": str(payload.get("reason", "template_revision_demo")),
        }
        if payload.get("revise_document_id") is not None:
            kwargs["revise_document_id"] = str(payload["revise_document_id"])
        elif payload.get("threshold") is None and not payload.get("trust_updates"):
            kwargs["revise_document_id"] = "D06"
        if payload.get("threshold") is not None:
            kwargs["threshold"] = float(payload["threshold"])
        if payload.get("trust_updates") is not None:
            kwargs["trust_updates"] = {
                str(key): float(value)
                for key, value in payload["trust_updates"].items()
            }
        return jsonify(workbench.invalidate(**kwargs))

    @app.post("/api/probes/rollback")
    def api_probe_rollback() -> Any:
        workbench.snapshot_rollback_probe()
        return jsonify({"ok": True})

    @app.post("/api/probes/corruption")
    def api_probe_corruption() -> Any:
        workbench.snapshot_corruption_probe()
        return jsonify({"ok": True})

    @app.post("/api/probes/stage-busy")
    def api_probe_stage_busy() -> Any:
        workbench.stage_busy_probe()
        return jsonify({"ok": True})

    @app.post("/api/reset")
    def api_reset() -> Any:
        return jsonify(workbench.reset())

    @app.errorhandler(BusinessError)
    def handle_business_error(error: BusinessError) -> Any:
        entry = getattr(error, "_workbench_error_entry", None)
        if entry is None:
            entry = workbench.note_error(
                error,
                source=f"{request.method} {request.path}",
            )
        payload = {
            **error.as_dict(),
            "error_sequence": entry["sequence"],
            "recorded_at": entry["recorded_at"],
        }
        return jsonify(payload), _error_status(error.code)

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception) -> Any:
        business_error = BusinessError(
            "E_INTERNAL",
            f"未预期错误：{type(error).__name__}: {error}",
        )
        workbench.note_error(
            business_error,
            source=f"{request.method} {request.path}",
        )
        return jsonify(business_error.as_dict()), 500

    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("EVENTGRAPH_PORT", "5000"))
    print(f"EventGraph workbench: http://127.0.0.1:{port}")
    app.run(
        host="127.0.0.1",
        port=port,
        debug=False,
        threaded=True,
        use_reloader=False,
    )
