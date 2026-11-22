from __future__ import annotations

import json
import mimetypes
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .config import WorkbenchConfig
from .demo import PROJECT_ROOT, run_demo
from .errors import BusinessError, E_IO, E_NO_CHECKPOINT, E_STAGE_BUSY
from .pipeline import Workbench


STATIC_DIR = Path(__file__).resolve().parent / "static"


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


class WorkbenchHandler(BaseHTTPRequestHandler):
    server_version = "QuantTraceLab/1.0"
    artifacts_dir = PROJECT_ROOT / "artifacts"
    workbench_state = PROJECT_ROOT / "runtime" / "workbench-state"
    _action_lock = threading.Lock()

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[http] {self.address_string()} {format % args}")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: Any) -> None:
        self._send(status, _json_bytes(value), "application/json; charset=utf-8")

    def _dashboard(self) -> dict[str, Any] | None:
        path = self.artifacts_dir / "dashboard.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._serve_static("index.html")
            return
        if parsed.path == "/api/health":
            self._json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "dashboard_available": (self.artifacts_dir / "dashboard.json").exists(),
                },
            )
            return
        if parsed.path == "/api/dashboard":
            dashboard = self._dashboard()
            if dashboard is None:
                self._json(
                    HTTPStatus.NOT_FOUND,
                    {
                        "ok": False,
                        "code": "E_NO_DASHBOARD",
                        "message": "尚无诊断结果，请运行一次演示",
                    },
                )
            else:
                self._json(HTTPStatus.OK, dashboard)
            return
        if parsed.path.startswith("/static/"):
            self._serve_static(unquote(parsed.path[len("/static/") :]))
            return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "not found"})

    def _serve_static(self, relative: str) -> None:
        candidate = (STATIC_DIR / relative).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "message": "forbidden"})
            return
        if not candidate.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "asset not found"})
            return
        content_type, _ = mimetypes.guess_type(candidate.name)
        self._send(
            HTTPStatus.OK,
            candidate.read_bytes(),
            (content_type or "application/octet-stream")
            + ("; charset=utf-8" if content_type and content_type.startswith("text/") else ""),
        )

    def _read_request_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        self._read_request_body()
        try:
            if not self._action_lock.acquire(blocking=False):
                raise BusinessError(
                    E_STAGE_BUSY,
                    "工作台已有操作在执行",
                    stage="server",
                )
            try:
                if parsed.path == "/api/demo":
                    summary = run_demo(artifacts_dir=self.artifacts_dir)
                    dashboard = self._dashboard()
                    self._json(HTTPStatus.OK, {"ok": True, "summary": summary, "dashboard": dashboard})
                    return
                if parsed.path == "/api/run":
                    config = WorkbenchConfig(
                        state_dir=self.workbench_state,
                        cache_dir=PROJECT_ROOT / "runtime" / "workbench-cache",
                        temp_dir=PROJECT_ROOT / "runtime" / "workbench-tmp",
                        artifacts_dir=self.artifacts_dir,
                    )
                    result = Workbench(config).run(clean=True)
                    self._json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "mode": result["recovery"]["mode"],
                            "latest_version": result["progress"]["latest"]["version"],
                            "dashboard": self._dashboard(),
                        },
                    )
                    return
                if parsed.path == "/api/revision-demo":
                    self._revision_demo()
                    return
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "not found"})
            finally:
                self._action_lock.release()
        except BusinessError as exc:
            status = (
                HTTPStatus.CONFLICT
                if exc.code == E_STAGE_BUSY
                else HTTPStatus.NOT_FOUND
                if exc.code == E_NO_CHECKPOINT
                else HTTPStatus.BAD_REQUEST
            )
            self._json(status, {"ok": False, **exc.to_dict()})
        except Exception as exc:
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "code": "E_UNATTRIBUTED",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            )

    def _revision_demo(self) -> None:
        config = WorkbenchConfig(
            state_dir=self.workbench_state,
            cache_dir=PROJECT_ROOT / "runtime" / "workbench-cache",
            temp_dir=PROJECT_ROOT / "runtime" / "workbench-tmp",
            artifacts_dir=self.artifacts_dir,
        )
        workbench = Workbench(config)
        latest = workbench.store.latest(required=False)
        if latest is None:
            Workbench(config).run(clean=True)
            workbench = Workbench(config)
        quant = workbench.store.stage_snapshot("quantization")
        revision_by_id = {
            item["layer_id"]: int(item["weight_revision"])
            for item in (quant.payload.get("records", []) if quant else [])
        }
        entries = quant.payload.get("manifest_entries", []) if quant else []
        target_index = min(1, max(0, len(entries) - 1))
        if entries:
            target_id = str(entries[target_index]["layer_id"])
            next_revision = revision_by_id.get(target_id, 1) + 1
        else:
            next_revision = 2
        revision_config = config.clone(revision_overrides={target_index: next_revision})
        result = Workbench(revision_config).run(reuse_previous=True)
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "mode": result["recovery"]["mode"],
                "invalidation": result["execution"].get("invalidation"),
                "latest_version": result["progress"]["latest"]["version"],
                "dashboard": self._dashboard(),
            },
        )


def serve(*, host: str, port: int, artifacts_dir: Path) -> None:
    artifacts = Path(artifacts_dir).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    WorkbenchHandler.artifacts_dir = artifacts
    WorkbenchHandler.workbench_state = PROJECT_ROOT / "runtime" / "workbench-state"
    try:
        httpd = ThreadingHTTPServer((host, port), WorkbenchHandler)
    except OSError as exc:
        raise BusinessError(
            E_IO,
            f"无法监听 {host}:{port}，请更换端口",
            stage="server",
            details={"host": host, "port": port, "reason": str(exc)},
        ) from exc
    print(f"QuantTrace Lab: http://{host}:{port}")
    print("按 Ctrl+C 停止。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止...")
    finally:
        httpd.server_close()
