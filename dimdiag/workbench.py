"""Local interactive workbench using only the Python standard library."""

from __future__ import annotations

import copy
import json
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import load_config, validate_config
from .engine import DiagnosticEngine
from .errors import E_CONFIG_INVALID, BusinessError
from .serialization import canonical_json_bytes


METHODS = ("pca", "random_projection", "pca_random_projection")


class WorkbenchState:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.base_dir = config_path.parent
        self.workspace_root = (
            config_path.parent.parent
            if config_path.parent.name == "configs"
            else config_path.parent
        )
        self.engine = DiagnosticEngine(
            self.workspace_root,
            base_dir=self.base_dir,
        )
        self.lock = threading.RLock()
        self.base_config = load_config(config_path)

    def state(self) -> dict[str, Any]:
        with self.lock:
            return self.engine.inspect()

    def _apply_overrides(self, overrides: dict[str, Any]) -> dict[str, Any]:
        config = copy.deepcopy(self.base_config)
        method = overrides.get("method")
        if method is not None:
            config["method"] = method
        precision_mode = overrides.get("precision_mode")
        if precision_mode is not None:
            config["precision"]["mode"] = precision_mode
        fixed_scale = overrides.get("fixed_scale")
        if fixed_scale is not None:
            config["precision"]["fixed_scale"] = int(fixed_scale)
        threshold_strategy = overrides.get("threshold_strategy")
        if threshold_strategy is not None:
            config["evaluation"]["threshold_strategy"] = threshold_strategy
        target_dim = overrides.get("target_dim")
        if target_dim is not None:
            config["target_dim"] = int(target_dim)
        standardize = overrides.get("standardize")
        if standardize is not None:
            config["standardize"] = bool(standardize)
        return validate_config(config)

    def run(self, overrides: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            config = self._apply_overrides(overrides)
            if overrides.get("reset_before_run"):
                self.engine.store.reset("workbench_reset_before_run")
            outcome = self.engine.run(config, base_dir=self.base_dir, commit=True)
            return {
                "ok": True,
                "run": outcome.response,
                "state": self.engine.inspect(),
            }

    def run_all(self, overrides: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.engine.store.reset("workbench_demo")
            runs: list[dict[str, Any]] = []
            for method in METHODS:
                current = dict(overrides)
                current["method"] = method
                config = self._apply_overrides(current)
                try:
                    outcome = self.engine.run(
                        config,
                        base_dir=self.base_dir,
                        commit=True,
                    )
                    runs.append(
                        {
                            "method": method,
                            "ok": True,
                            "snapshot_version": outcome.snapshot["snapshot_version"],
                            "coordinate_checksum": outcome.observed.coordinate_checksum(),
                            "selfcheck_passed": outcome.selfcheck["overall_passed"],
                        }
                    )
                except BusinessError as error:
                    runs.append(
                        {
                            "method": method,
                            "ok": False,
                            "error": error.to_dict()["error"],
                        }
                    )
            return {
                "ok": all(run["ok"] for run in runs),
                "runs": runs,
                "state": self.engine.inspect(),
            }

    def selfcheck(self) -> dict[str, Any]:
        with self.lock:
            result = self.engine.selfcheck_current()
            result["state"] = self.engine.inspect()
            return result

    def reset(self) -> dict[str, Any]:
        with self.lock:
            archived = self.engine.store.reset("workbench_reset")
            return {
                "ok": True,
                "archived": archived,
                "state": self.engine.inspect(),
            }


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: WorkbenchState):
        super().__init__(address, WorkbenchHandler)
        self.state = state


class WorkbenchHandler(BaseHTTPRequestHandler):
    server: WorkbenchServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = canonical_json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self) -> None:
        path = Path(__file__).with_name("static") / "index.html"
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        if length > 1024 * 1024:
            raise BusinessError(
                E_CONFIG_INVALID,
                "request body is too large",
                {"field": "body", "limit_bytes": 1024 * 1024},
            )
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BusinessError(
                E_CONFIG_INVALID,
                "request body is not valid JSON",
                {"field": "body", "reason": str(exc)},
            ) from exc
        if not isinstance(value, dict):
            raise BusinessError(
                E_CONFIG_INVALID,
                "request body must be an object",
                {"field": "body"},
            )
        return value

    def do_GET(self) -> None:
        try:
            if self.path in ("/", "/index.html"):
                self._send_html()
                return
            if self.path == "/api/state":
                self._send_json(self.server.state.state())
                return
            if self.path == "/api/health":
                self._send_json({"ok": True, "service": "dimdiag-workbench"})
                return
            self._send_json(
                {"ok": False, "error": {"code": "E_NOT_FOUND", "message": "not found"}},
                HTTPStatus.NOT_FOUND,
            )
        except BusinessError as error:
            self._send_json(error.to_dict())
        except Exception as exc:
            self._send_json(
                {
                    "ok": False,
                    "error": {
                        "code": "E_WORKBENCH",
                        "message": str(exc),
                        "details": {},
                    },
                },
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_POST(self) -> None:
        try:
            payload = self._read_json_body()
            if self.path == "/api/run":
                self._send_json(self.server.state.run(payload))
                return
            if self.path == "/api/demo":
                self._send_json(self.server.state.run_all(payload))
                return
            if self.path == "/api/selfcheck":
                self._send_json(self.server.state.selfcheck())
                return
            if self.path == "/api/reset":
                self._send_json(self.server.state.reset())
                return
            self._send_json(
                {"ok": False, "error": {"code": "E_NOT_FOUND", "message": "not found"}},
                HTTPStatus.NOT_FOUND,
            )
        except BusinessError as error:
            self._send_json(error.to_dict())
        except Exception as exc:
            self._send_json(
                {
                    "ok": False,
                    "error": {
                        "code": "E_WORKBENCH",
                        "message": str(exc),
                        "details": {},
                    },
                },
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def make_server(
    config_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> WorkbenchServer:
    path = Path(config_path).resolve()
    if not path.exists():
        raise BusinessError(
            E_CONFIG_INVALID,
            "workbench configuration does not exist",
            {"field": "config", "path": str(path)},
        )
    state = WorkbenchState(path)
    return WorkbenchServer((host, int(port)), state)


def serve(
    *,
    config_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
) -> None:
    server = make_server(config_path, host=host, port=port)
    actual_host, actual_port = server.server_address[:2]
    url = f"http://{actual_host}:{actual_port}/"
    print(f"DimDiag workbench: {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

