from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .canonical import canonical_json
from .demo import run_demo
from .diagnostics import collect_error_scenarios
from .errors import ErrorCode
from .state import build_state


class AppContext:
    def __init__(self, project_root: Path) -> None:
        self.root = project_root.resolve()
        self.data_dir = self.root / "data"
        self.app_root = self.root / "runs" / "app"
        self.store_path = self.app_root / "snapshots.sqlite3"
        self.output_dir = self.app_root / "out"
        self.low_root = self.root / "runs" / "app_low"
        self.strict_root = self.root / "runs" / "app_strict"
        self.invalid_root = self.root / "runs" / "app_invalid"
        self.action_lock = threading.Lock()

    @property
    def static_dir(self) -> Path:
        return self.root / "poscascade" / "static"

    def reset(self, directory: Path) -> None:
        resolved = directory.resolve()
        runs_root = (self.root / "runs").resolve()
        if resolved != runs_root and runs_root not in resolved.parents:
            raise ValueError(f"refusing to reset outside runs: {resolved}")
        if resolved.exists():
            shutil.rmtree(resolved)

    def run_cli(
        self,
        *,
        store_path: Path,
        output_dir: Path,
        dict_path: Path,
        labels_path: Path,
        config_path: Path,
        extra: list[str] | None = None,
    ) -> dict[str, Any]:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(self.root) + (
            os.pathsep + environment["PYTHONPATH"]
            if environment.get("PYTHONPATH")
            else ""
        )
        command = [
            sys.executable,
            "-m",
            "poscascade.cli",
            "run",
            "--input",
            str(self.data_dir / "lines.jsonl"),
            "--dict",
            str(dict_path),
            "--labels",
            str(labels_path),
            "--config",
            str(config_path),
            "--store",
            str(store_path),
            "--out",
            str(output_dir),
        ]
        if extra:
            command.extend(extra)
        process = subprocess.run(
            command,
            cwd=self.root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        return {
            "returncode": process.returncode,
            "stdout_tail": process.stdout[-2500:],
            "stderr_tail": process.stderr[-2500:],
        }

    def state(self) -> dict[str, Any]:
        state = build_state(
            store_path=self.store_path,
            output_dir=self.output_dir,
            data_dir=self.data_dir,
        )
        demo_report_path = self.root / "runs" / "demo" / "demo_report.json"
        if demo_report_path.exists():
            try:
                state["demo_report"] = json.loads(
                    demo_report_path.read_text(encoding="utf-8")
                )
            except ValueError:
                state["demo_report"] = None
        return state

    def results(self) -> dict[str, Any]:
        annotations_path = self.output_dir / "annotations.jsonl"
        summary_path = self.output_dir / "summary.json"
        annotations: list[dict[str, Any]] = []
        summary: dict[str, Any] | None = None
        if annotations_path.exists():
            for raw_line in annotations_path.read_text(encoding="utf-8").splitlines():
                if raw_line.strip():
                    annotations.append(json.loads(raw_line))
        if summary_path.exists():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        return {
            "ok": True,
            "code": ErrorCode.OK,
            "annotations": annotations,
            "summary": summary,
        }

    def action(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        v1_dict = self.data_dir / "dict_v1.json"
        v1_labels = self.data_dir / "labels_v1.json"
        v2_dict = self.data_dir / "dict_v2.json"
        v2_labels = self.data_dir / "labels_v2.json"
        default_config = self.data_dir / "config_default.json"

        if action == "reset":
            self.reset(self.app_root)
            return {"ok": True, "action": action, "state": self.state()}

        if action == "run_full":
            self.reset(self.app_root)
            process = self.run_cli(
                store_path=self.store_path,
                output_dir=self.output_dir,
                dict_path=v1_dict,
                labels_path=v1_labels,
                config_path=default_config,
            )
            return self._action_result(action, process, self.state())

        if action == "crash":
            stage = payload.get("stage", "segment")
            if stage not in {"validate", "segment", "tag", "summarize"}:
                stage = "segment"
            self.reset(self.app_root)
            process = self.run_cli(
                store_path=self.store_path,
                output_dir=self.output_dir,
                dict_path=v1_dict,
                labels_path=v1_labels,
                config_path=default_config,
                extra=["--crash-after", stage],
            )
            return self._action_result(action, process, self.state())

        if action == "resume":
            process = self.run_cli(
                store_path=self.store_path,
                output_dir=self.output_dir,
                dict_path=v1_dict,
                labels_path=v1_labels,
                config_path=default_config,
            )
            return self._action_result(action, process, self.state())

        if action == "upgrade":
            if not self.store_path.exists() or not self.state().get("active_generation"):
                self.reset(self.app_root)
                self.run_cli(
                    store_path=self.store_path,
                    output_dir=self.output_dir,
                    dict_path=v1_dict,
                    labels_path=v1_labels,
                    config_path=default_config,
                )
            process = self.run_cli(
                store_path=self.store_path,
                output_dir=self.output_dir,
                dict_path=v2_dict,
                labels_path=v2_labels,
                config_path=default_config,
            )
            return self._action_result(action, process, self.state())

        if action == "low_budget":
            self.reset(self.low_root)
            process = self.run_cli(
                store_path=self.low_root / "snapshots.sqlite3",
                output_dir=self.low_root / "out",
                dict_path=v1_dict,
                labels_path=v1_labels,
                config_path=self.data_dir / "config_low_budget.json",
            )
            state = build_state(
                store_path=self.low_root / "snapshots.sqlite3",
                output_dir=self.low_root / "out",
                data_dir=self.data_dir,
            )
            return self._action_result(action, process, state)

        if action == "strict":
            self.reset(self.strict_root)
            process = self.run_cli(
                store_path=self.strict_root / "snapshots.sqlite3",
                output_dir=self.strict_root / "out",
                dict_path=v1_dict,
                labels_path=v1_labels,
                config_path=self.data_dir / "config_strict.json",
            )
            state = build_state(
                store_path=self.strict_root / "snapshots.sqlite3",
                output_dir=self.strict_root / "out",
                data_dir=self.data_dir,
            )
            return self._action_result(action, process, state)

        if action == "errors":
            report = collect_error_scenarios(self.data_dir)
            return {
                "ok": report["ok"],
                "action": action,
                "code": report["code"],
                "error_chain": report,
                "state": self.state(),
            }

        if action == "demo":
            report = run_demo(self.root)
            return {
                "ok": report["ok"],
                "action": action,
                "code": ErrorCode.OK if report["ok"] else ErrorCode.E_INCONSISTENT,
                "demo_report": report,
                "state": self.state(),
            }

        if action == "selfcheck":
            state = self.state()
            return {
                "ok": bool(state.get("ok")) and (
                    (state.get("selfcheck") or {}).get("code") == ErrorCode.OK
                    or state.get("selfcheck") is None
                ),
                "action": action,
                "code": (
                    (state.get("selfcheck") or {}).get("code", ErrorCode.OK)
                    if state.get("selfcheck") is not None
                    else ErrorCode.OK
                ),
                "state": state,
            }

        return {
            "ok": False,
            "action": action,
            "code": ErrorCode.E_INPUT_FORMAT,
            "message": f"未知操作: {action}",
            "state": self.state(),
        }

    def _action_result(
        self,
        action: str,
        process: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        errors_path = self.output_dir / "errors.json"
        if errors_path.exists():
            try:
                loaded = json.loads(errors_path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    errors = loaded
            except ValueError:
                errors = []
        code = state.get("selfcheck", {}).get("code") if state.get("selfcheck") else None
        if errors:
            code = errors[0].get("code")
        if code is None:
            if process["returncode"] == 137:
                code = ErrorCode.E_PROCESS_TERMINATED
            else:
                code = ErrorCode.OK if process["returncode"] == 0 else ErrorCode.E_UNATTRIBUTED
        success = process["returncode"] == 0 and code == ErrorCode.OK
        return {
            "ok": success,
            "action": action,
            "code": code,
            "process": process,
            "errors": errors,
            "state": state,
        }


def _json_response(handler: BaseHTTPRequestHandler, status: int, value: Any) -> None:
    body = (canonical_json(value) + "\n").encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _file_response(handler: BaseHTTPRequestHandler, path: Path) -> None:
    if not path.is_file():
        handler.send_error(HTTPStatus.NOT_FOUND)
        return
    body = path.read_bytes()
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if path.suffix == ".js":
        content_type = "application/javascript"
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type + "; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    handler.wfile.write(body)


def make_handler(context: AppContext) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CascadeLab/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            sys.stderr.write(
                "%s - - [local] %s\n" % (self.address_string(), format % args)
            )

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                _file_response(self, context.static_dir / "index.html")
                return
            if parsed.path.startswith("/static/"):
                relative = parsed.path.removeprefix("/static/")
                candidate = (context.static_dir / relative).resolve()
                if context.static_dir.resolve() not in candidate.parents:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                _file_response(self, candidate)
                return
            if parsed.path == "/api/state":
                query = parse_qs(parsed.query)
                if query.get("workspace", ["app"])[0] == "low":
                    state = build_state(
                        store_path=context.low_root / "snapshots.sqlite3",
                        output_dir=context.low_root / "out",
                        data_dir=context.data_dir,
                    )
                    _json_response(self, HTTPStatus.OK, state)
                    return
                _json_response(self, HTTPStatus.OK, context.state())
                return
            if parsed.path == "/api/results":
                _json_response(self, HTTPStatus.OK, context.results())
                return
            if parsed.path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/api/action":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload, dict):
                    payload = {}
            except (ValueError, json.JSONDecodeError):
                _json_response(
                    self,
                    HTTPStatus.BAD_REQUEST,
                    {
                        "ok": False,
                        "code": ErrorCode.E_INPUT_FORMAT,
                        "message": "请求体必须是 JSON 对象",
                    },
                )
                return
            action = str(payload.get("action", ""))
            if not context.action_lock.acquire(blocking=False):
                _json_response(
                    self,
                    HTTPStatus.CONFLICT,
                    {
                        "ok": False,
                        "action": action,
                        "code": ErrorCode.E_STAGE_BUSY,
                        "message": "已有操作正在提交，后到请求不等待",
                    },
                )
                return
            try:
                result = context.action(action, payload)
                _json_response(
                    self,
                    HTTPStatus.OK if result.get("ok") else HTTPStatus.UNPROCESSABLE_ENTITY,
                    result,
                )
            except Exception as exc:
                _json_response(
                    self,
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "ok": False,
                        "action": action,
                        "code": ErrorCode.E_UNATTRIBUTED,
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                )
            finally:
                context.action_lock.release()

    return Handler


def serve(*, host: str, port: int, project_root: str | Path) -> None:
    context = AppContext(Path(project_root))
    context.data_dir.mkdir(parents=True, exist_ok=True)
    handler = make_handler(context)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"CascadeLab running at http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
