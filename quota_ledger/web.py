from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

from .engine import LedgerEngine
from .errors import BusinessError, error
from .models import parse_bundles


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise error(
            "INPUT_FILE_UNREADABLE",
            f"无法读取样本: {path}",
            details={"path": str(path), "reason": str(exc)},
        ) from exc
    except json.JSONDecodeError as exc:
        raise error(
            "INVALID_JSON",
            f"样本不是有效 JSON: {path.name}",
            details={"path": str(path), "line": exc.lineno},
        ) from exc


def _load_demo(engine: LedgerEngine, samples_dir: Path, *, reset: bool) -> dict[str, Any]:
    tenants_doc = _read_json(samples_dir / "tenants.json")
    services_doc = _read_json(samples_dir / "services.json")
    applications_doc = _read_json(samples_dir / "applications.json")
    tenants, services, applications = parse_bundles(
        tenants_doc, services_doc, applications_doc
    )
    return engine.load_bundle(
        tenants,
        services,
        applications,
        reset=reset,
        at_ms=0,
    )


def _submit_round(
    engine: LedgerEngine,
    *,
    round_id: str,
    round_start_ms: int,
    round_budget_ms: int,
    workers: int,
) -> dict[str, Any]:
    staged = engine.list_staged_applications()
    status = engine.status()
    service_map = {service["name"]: service for service in status["services"]}
    ordered = sorted(
        staged, key=lambda row: (int(row["requested_at_ms"]), int(row["sequence"]))
    )
    selected: list[str] = []
    abandoned: list[tuple[str, str]] = []
    counts: dict[str, int] = {}
    for app in ordered:
        app_id = app["application_id"]
        requested_at = int(app["requested_at_ms"])
        if (
            requested_at < round_start_ms
            or requested_at - round_start_ms > round_budget_ms
        ):
            abandoned.append((app_id, "ROUND_BUDGET_EXHAUSTED"))
            continue
        limit = int(service_map[app["service"]]["requests_per_round"])
        used = counts.get(app["service"], 0)
        if used >= limit:
            abandoned.append((app_id, "SERVICE_ROUND_BATCH_LIMIT"))
            continue
        counts[app["service"]] = used + 1
        selected.append(app_id)

    abandon_results = [
        engine.abandon_application(
            app_id,
            reason_code=reason_code,
            at_ms=round_start_ms,
            round_id=round_id,
            round_start_ms=round_start_ms,
            round_budget_ms=round_budget_ms,
        )
        for app_id, reason_code in abandoned
    ]

    def run(app_id: str) -> dict[str, Any]:
        return engine.submit(
            app_id,
            now_ms=round_start_ms,
            round_id=round_id,
            round_start_ms=round_start_ms,
            round_budget_ms=round_budget_ms,
        )

    if workers <= 1:
        results = [run(app_id) for app_id in selected]
    else:
        with ThreadPoolExecutor(max_workers=min(workers, 16)) as pool:
            futures = [pool.submit(run, app_id) for app_id in selected]
            results = [future.result() for future in futures]
    return {
        "ok": True,
        "round_id": round_id,
        "round_start_ms": round_start_ms,
        "round_budget_ms": round_budget_ms,
        "workers": workers,
        "selected": selected,
        "abandoned": abandon_results,
        "results": results,
    }


def create_app(
    *,
    db_path: str | Path,
    samples_dir: str | Path = "samples",
    auto_load_demo: bool = True,
) -> Flask:
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False
    engine = LedgerEngine(db_path)
    engine.initialize()
    samples_path = Path(samples_dir).resolve()

    if auto_load_demo and not engine.status()["tenants"]:
        _load_demo(engine, samples_path, reset=True)

    @app.errorhandler(BusinessError)
    def handle_business_error(exc: BusinessError):
        return jsonify(exc.to_dict()), exc.http_status

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            db_path=str(Path(db_path).resolve()),
            samples_path=str(samples_path),
        )

    @app.get("/api/status")
    def api_status():
        return jsonify(engine.status())

    @app.get("/api/verify")
    def api_verify():
        result = engine.verify()
        return jsonify(result), 200 if result["ok"] else 409

    @app.post("/api/submit")
    def api_submit():
        payload = request.get_json(silent=True) or {}
        application_id = payload.get("application_id")
        if not isinstance(application_id, str) or not application_id:
            raise error("MISSING_FIELD", "application_id 缺失")
        result = engine.submit(
            application_id,
            now_ms=_optional_int(payload, "now_ms"),
            round_id=payload.get("round_id"),
            round_start_ms=_optional_int(payload, "round_start_ms"),
            round_budget_ms=_optional_int(payload, "round_budget_ms"),
        )
        return jsonify(result)

    @app.post("/api/submit-round")
    def api_submit_round():
        payload = request.get_json(silent=True) or {}
        round_id = payload.get("round_id", "web-round")
        round_start = _optional_int(payload, "round_start_ms")
        budget = _optional_int(payload, "round_budget_ms")
        workers = int(payload.get("workers", 1))
        if round_start is None or budget is None:
            staged = engine.list_staged_applications()
            round_start = (
                round_start
                if round_start is not None
                else min((int(a["requested_at_ms"]) for a in staged), default=0)
            )
            budget = budget if budget is not None else 2**62
        return jsonify(
            _submit_round(
                engine,
                round_id=str(round_id),
                round_start_ms=round_start,
                round_budget_ms=budget,
                workers=workers,
            )
        )

    @app.post("/api/release")
    def api_release():
        payload = request.get_json(silent=True) or {}
        application_id = payload.get("application_id")
        if not isinstance(application_id, str) or not application_id:
            raise error("MISSING_FIELD", "application_id 缺失")
        trigger = str(payload.get("trigger_type", "MANUAL")).upper()
        if trigger not in {"MANUAL", "CANCEL", "EXPIRY"}:
            raise error("INVALID_FIELD_VALUE", "trigger_type 非法")
        return jsonify(
            engine.release(
                application_id,
                at_ms=_optional_int(payload, "now_ms"),
                trigger_type=trigger,
            )
        )

    @app.post("/api/resume")
    def api_resume():
        payload = request.get_json(silent=True) or {}
        result = engine.resume(now_ms=_optional_int(payload, "now_ms"))
        return jsonify(result), 200 if result["ok"] else 409

    @app.post("/api/demo/reset")
    def api_demo_reset():
        return jsonify(_load_demo(engine, samples_path, reset=True))

    @app.post("/api/demo/run")
    def api_demo_run():
        load_result = _load_demo(engine, samples_path, reset=True)
        submit_result = _submit_round(
            engine,
            round_id="demo-round-1",
            round_start_ms=0,
            round_budget_ms=100,
            workers=1,
        )
        resume_200 = engine.resume(now_ms=200)
        resume_500 = engine.resume(now_ms=500)
        verify_result = engine.verify()
        return jsonify(
            {
                "ok": verify_result["ok"],
                "load": load_result,
                "submit": submit_result,
                "resume_at_200": resume_200,
                "resume_at_500": resume_500,
                "verify": verify_result,
                "status": engine.status(),
            }
        )

    return app


def _optional_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise error("INVALID_FIELD_TYPE", f"{key} 必须是整数")
    return value
