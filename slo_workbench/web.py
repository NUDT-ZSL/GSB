"""Flask interactive query surface.

Endpoints (all JSON, stable key ordering):

* ``GET  /health``
* ``GET  /status``
* ``GET  /slos``
* ``GET  /windows/<slo_name>``
* ``GET  /window/<slo_name>/<int:window_no>?verify=1``
* ``GET  /burn/<slo_name>?at_window=<n>&verify=1``
* ``POST /resume``
* ``GET  /verify``
* ``GET  /errors``
"""

from __future__ import annotations

import os

from flask import Flask, jsonify, request

from .errors import BusinessError
from .engine import Engine
from .query import QueryService
from .repository import Repository
from .storage import Workspace, dumps_canonical, transaction


def create_app(workspace_path: str | None = None) -> Flask:
    workspace = Workspace(workspace_path or os.environ.get("SLO_WB_HOME", "./.slo_workbench"))
    workspace.bootstrap()
    app = Flask(__name__)
    app.config["WORKSPACE"] = workspace
    app.json.sort_keys = True

    @app.errorhandler(BusinessError)
    def handle_business_error(error: BusinessError):
        response = jsonify(error.to_dict())
        response.status_code = 422 if error.code not in ("E_NOT_FOUND",) else 404
        return response

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/status")
    def status():
        conn = workspace.connect()
        txn = transaction(conn)
        txn.__enter__()
        try:
            result = QueryService(Repository(conn)).status()
            txn.__exit__(None, None, None)
            return app.response_class(dumps_canonical(result), mimetype="application/json")
        except Exception:
            txn.__exit__(None, None, None)
            raise
        finally:
            conn.close()

    @app.get("/slos")
    def slos():
        conn = workspace.connect()
        try:
            with transaction(conn):
                payload = [slo.to_dict() for slo in Repository(conn).current_slos()]
            return jsonify(payload)
        finally:
            conn.close()

    @app.get("/windows/<slo_name>")
    def windows(slo_name: str):
        conn = workspace.connect()
        try:
            with transaction(conn):
                repo = Repository(conn)
                slo = repo.get_slo(slo_name)
                payload = repo.list_windows(slo_name, slo.version)
            return app.response_class(dumps_canonical(payload), mimetype="application/json")
        finally:
            conn.close()

    @app.get("/window/<slo_name>/<int:window_no>")
    def window(slo_name: str, window_no: int):
        verify = request.args.get("verify", "0") in ("1", "true", "yes")
        conn = workspace.connect()
        try:
            with transaction(conn):
                payload = QueryService(Repository(conn)).query_window(
                    slo_name, window_no, verify=verify
                )
            return app.response_class(dumps_canonical(payload), mimetype="application/json")
        finally:
            conn.close()

    @app.get("/burn/<slo_name>")
    def burn(slo_name: str):
        verify = request.args.get("verify", "0") in ("1", "true", "yes")
        at_window = request.args.get("at_window", type=int)
        conn = workspace.connect()
        try:
            with transaction(conn):
                payload = QueryService(Repository(conn)).query_burn(
                    slo_name, at_window=at_window, verify=verify
                )
            return app.response_class(dumps_canonical(payload), mimetype="application/json")
        finally:
            conn.close()

    @app.post("/resume")
    def resume():
        payload = Engine(workspace).resume()
        return app.response_class(dumps_canonical(payload), mimetype="application/json")

    @app.get("/verify")
    def verify():
        conn = workspace.connect()
        try:
            with transaction(conn):
                payload = QueryService(Repository(conn)).verify_all()
            return app.response_class(dumps_canonical(payload), mimetype="application/json")
        finally:
            conn.close()

    @app.get("/errors")
    def errors():
        conn = workspace.connect()
        try:
            with transaction(conn):
                payload = Repository(conn).list_errors()
            return app.response_class(dumps_canonical(payload), mimetype="application/json")
        finally:
            conn.close()

    return app


def main() -> None:
    app = create_app()
    app.run(host=os.environ.get("SLO_WB_HOST", "127.0.0.1"), port=int(os.environ.get("SLO_WB_PORT", "5000")))


if __name__ == "__main__":
    main()
