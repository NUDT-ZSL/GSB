"""Flask endpoints equivalent to the CLI subcommands.

POST /load   {"graph":..., "events":..., "config":...}
POST /run    {"slots": N, "windows": "LO:HI", "worker_id": "w1"}
POST /resume {}
POST /patch  {"graph": ...}
GET  /status
GET  /verify
"""
import json

from flask import Flask, jsonify, request

from . import loader, runner, verify as verify_mod, versioning
from .errors import BizError
from .planner import build_plan
from .status import build_status
from .store import Store


def create_app(db_path):
    app = Flask(__name__)

    def store():
        return Store(db_path)

    @app.errorhandler(BizError)
    def biz_error(exc):
        return jsonify({"error": exc.to_dict()}), 400

    @app.post("/load")
    def load():
        body = request.get_json(force=True)
        graph = loader.validate_graph(body["graph"])
        events = loader.validate_events(body["events"])
        config = loader.validate_config(body["config"])
        loader.cross_validate(graph, events)
        s = store()
        try:
            plan = build_plan(graph, events, config)
            s.load_artifacts(graph, events, config, plan["plan_summary"])
            return jsonify({"status": "loaded",
                            "plan_summary": plan["plan_summary"]})
        finally:
            s.close()

    def _run(resume):
        body = request.get_json(silent=True) or {}
        lo, hi = -2**62, 2**62
        if body.get("windows"):
            lo, hi = (int(x) for x in body["windows"].split(":"))
        s = store()
        try:
            stats = runner.run(s, worker_id=body.get("worker_id", "http"),
                               slots=body.get("slots"),
                               window_lo=lo, window_hi=hi, resume=resume)
            return jsonify({"status": "ok", "resumed": resume, **stats})
        finally:
            s.close()

    @app.post("/run")
    def run_ep():
        return _run(False)

    @app.post("/resume")
    def resume_ep():
        return _run(True)

    @app.post("/patch")
    def patch_ep():
        body = request.get_json(force=True)
        s = store()
        try:
            report = versioning.apply_patch(s, body["graph"])
            return jsonify(report)
        finally:
            s.close()

    @app.get("/status")
    def status_ep():
        s = store()
        try:
            return app.response_class(
                json.dumps(build_status(s), sort_keys=True, indent=2),
                mimetype="application/json")
        finally:
            s.close()

    @app.get("/verify")
    def verify_ep():
        s = store()
        try:
            report = verify_mod.verify(s)
            return jsonify(report), (200 if report["status"] == "ok" else 409)
        finally:
            s.close()

    return app
