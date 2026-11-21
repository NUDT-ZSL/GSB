from __future__ import annotations

import json
import shutil
import threading
import webbrowser
from dataclasses import replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import EngineConfig, Injection
from .demo import _kill_worker, ensure_local_samples
from .engine import DiagnosticEngine
from .errors import E_STAGE_BUSY, BusinessError
from .manifest import ManifestView, load_manifest, write_manifest
from .selfcheck import run_consistency_checks
from .snapshots import SnapshotStore


STATIC_DIR = Path(__file__).resolve().parent / "static"


def _fresh(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


class WorkbenchService:
    def __init__(self, workspace: Path, runtime_dir: Path):
        self.workspace = Path(workspace)
        self.manifest_path, self.calibration_path = ensure_local_samples(self.workspace)
        self.runtime_dir = Path(runtime_dir)
        self.scenario_root = self.runtime_dir.parent / f"{self.runtime_dir.name}_scenarios"
        self.current_runtime = self.runtime_dir
        self.last_report: dict[str, Any] | None = None
        self.last_error: dict[str, Any] | None = None
        self.operation_lock = threading.Lock()

    def _guard(self) -> None:
        if not self.operation_lock.acquire(blocking=False):
            raise BusinessError(
                E_STAGE_BUSY,
                "another workbench operation is already running",
                stage="workbench",
                trace=["web:operation_guard"],
            )

    def _release(self) -> None:
        if self.operation_lock.locked():
            self.operation_lock.release()

    def _run(
        self,
        runtime: Path,
        *,
        manifest: Path | None = None,
        config: EngineConfig | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        engine = DiagnosticEngine(
            manifest_path=manifest or self.manifest_path,
            calibration_path=self.calibration_path,
            runtime_dir=runtime,
            config=config,
        )
        report = engine.run()
        return report, engine.latest_snapshot()

    def _view(self, snapshot: dict[str, Any] | None) -> dict[str, Any]:
        if snapshot is None:
            return {}
        diagnostics = snapshot.get("stages", {}).get("layer_inference", {}).get(
            "diagnostics", {}
        )
        try:
            config_data = (
                snapshot.get("stages", {})
                .get("model_package_validation", {})
                .get("config")
            )
            check_config = (
                EngineConfig.from_mapping(config_data)
                if config_data is not None
                else EngineConfig()
            )
            self_check = run_consistency_checks(snapshot, check_config)
        except BusinessError as exc:
            self_check = {"passed": False, "error": exc.as_dict()}
        stage_views = {}
        for stage_name, stage_data in snapshot.get("stages", {}).items():
            stage_views[stage_name] = {
                "completed_layers": stage_data.get("completed_layers", []),
                "affected_layers": stage_data.get("affected_layers", []),
                "reused_layers": stage_data.get("reused_layers", []),
                "rebuilt_layers": stage_data.get("rebuilt_layers", []),
                "checksum": stage_data.get("checksum"),
            }
        store = SnapshotStore(self.current_runtime)
        return {
            "version": snapshot.get("version"),
            "stage": snapshot.get("stage"),
            "model_tag": snapshot.get("model_tag"),
            "manifest_digest": snapshot.get("manifest_digest"),
            "config_digest": snapshot.get("config_digest"),
            "result_digest": snapshot.get("result_digest"),
            "layer_order": snapshot.get("layer_order", []),
            "degradations": snapshot.get("degradations", []),
            "injections": snapshot.get("injections", []),
            "stages": stage_views,
            "history": store.history(),
            "events": store.load_events(limit=30),
            "diagnostics": diagnostics,
            "final_prediction": snapshot.get("stages", {})
            .get("layer_inference", {})
            .get("final_prediction", []),
            "self_check": self_check,
            "runtime_dir": str(self.current_runtime),
        }

    def overview(self) -> dict[str, Any]:
        snapshot = None
        load_error = None
        try:
            snapshot = SnapshotStore(self.current_runtime).load_latest()
        except BusinessError as exc:
            load_error = exc.as_dict()
        view = self._view(snapshot)
        return {
            "ok": load_error is None,
            "error": load_error,
            "view": view,
            "last_report": self.last_report,
            "last_error": self.last_error,
            "runtime_dir": str(self.current_runtime),
            "available_samples": {
                "manifest": str(self.manifest_path),
                "calibration": str(self.calibration_path),
            },
        }

    def action(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._guard()
        try:
            action = str(payload.get("action", ""))
            self.last_error = None
            if action == "run":
                _fresh(self.runtime_dir)
                self.current_runtime = self.runtime_dir
                report, _ = self._run(self.runtime_dir)
                self.last_report = report
                return {"ok": True, "action": action, "report": report, "overview": self.overview()}
            if action == "inject":
                kind = str(payload.get("kind", "weight_truncate"))
                layer_id = str(payload.get("layer_id", "layer_1"))
                runtime = self.scenario_root / f"inject_{kind}_{layer_id}"
                _fresh(runtime)
                try:
                    self._run(
                        runtime,
                        config=EngineConfig(
                            injections=(Injection(kind, layer_id),)
                        ),
                    )
                    self.last_error = {
                        "raised": False,
                        "message": "注入未触发业务错误",
                    }
                except BusinessError as exc:
                    self.last_error = {"raised": True, "error": exc.as_dict()}
                return {
                    "ok": True,
                    "action": action,
                    "injection": {"kind": kind, "layer_id": layer_id},
                    "result": self.last_error,
                    "overview": self.overview(),
                }
            if action == "recover":
                baseline_runtime = self.scenario_root / "recovery_baseline"
                recovery_runtime = self.scenario_root / "recovery"
                _fresh(baseline_runtime)
                _fresh(recovery_runtime)
                baseline_report, baseline_snapshot = self._run(baseline_runtime)
                killed = _kill_worker(
                    self.manifest_path,
                    self.calibration_path,
                    recovery_runtime,
                    "layer_quantization",
                    1,
                )
                recovery_report, recovery_snapshot = self._run(recovery_runtime)
                equivalent = (
                    baseline_snapshot["result_digest"]
                    == recovery_snapshot["result_digest"]
                )
                self.current_runtime = recovery_runtime
                self.last_report = recovery_report
                return {
                    "ok": True,
                    "action": action,
                    "comparison": {
                        "kill_exit_code": killed.returncode,
                        "baseline_result_digest": baseline_snapshot["result_digest"],
                        "recovered_result_digest": recovery_snapshot["result_digest"],
                        "byte_equivalent_result": equivalent,
                        "recovery_report": recovery_report["recovery"],
                        "baseline_version": baseline_report["final_version"],
                    },
                    "overview": self.overview(),
                }
            if action == "update":
                update_runtime = self.scenario_root / "update"
                _fresh(update_runtime)
                baseline_report, baseline_snapshot = self._run(update_runtime)
                manifest_view = load_manifest(self.manifest_path)
                layers = list(manifest_view.layers)
                layer = layers[1]
                layers[1] = replace(
                    layer, weight_revision=layer.weight_revision + 1
                )
                updated_manifest = self.scenario_root / "model_manifest_v2.jsonl"
                write_manifest(
                    updated_manifest,
                    ManifestView.from_layers(manifest_view.model_tag, layers),
                )
                update_report, update_snapshot = self._run(
                    update_runtime, manifest=updated_manifest
                )
                self.current_runtime = update_runtime
                self.last_report = update_report
                quant_stage = update_snapshot["stages"]["layer_quantization"]
                inference_stage = update_snapshot["stages"]["layer_inference"]
                return {
                    "ok": True,
                    "action": action,
                    "comparison": {
                        "changed_layer": layer.layer_id,
                        "previous_revision": layer.weight_revision,
                        "current_revision": layer.weight_revision + 1,
                        "affected_layers": quant_stage["affected_layers"],
                        "reused_layers": quant_stage["reused_layers"],
                        "rebuilt_layers": quant_stage["rebuilt_layers"],
                        "inference_reused_layers": inference_stage["reused_layers"],
                        "baseline_result_digest": baseline_report["result_digest"],
                        "updated_result_digest": update_report["result_digest"],
                    },
                    "overview": self.overview(),
                }
            raise BusinessError(
                "E_INVALID_REQUEST",
                f"unknown workbench action: {action}",
                stage="workbench",
            )
        except BusinessError as exc:
            self.last_error = {"raised": True, "error": exc.as_dict()}
            return {
                "ok": False,
                "action": payload.get("action"),
                "result": self.last_error,
                "overview": self.overview(),
            }
        finally:
            self._release()


def _handler(service: WorkbenchService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "QuantDiag/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, value: Any, status: int = 200) -> None:
            payload = json.dumps(
                value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _send_file(self, path: Path) -> None:
            if not path.exists() or not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            payload = path.read_bytes()
            suffix = path.suffix.lower()
            content_type = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".json": "application/json; charset=utf-8",
            }.get(suffix, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/overview":
                self._send_json(service.overview())
                return
            if path == "/api/health":
                self._send_json({"ok": True, "service": "quant-diag-workbench"})
                return
            if path in {"/", "/index.html"}:
                self._send_file(STATIC_DIR / "index.html")
                return
            if path == "/app.js":
                self._send_file(STATIC_DIR / "app.js")
                return
            if path == "/styles.css":
                self._send_file(STATIC_DIR / "styles.css")
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/api/action":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("payload must be an object")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                self._send_json(
                    {"ok": False, "error": {"code": "E_INVALID_REQUEST", "message": str(exc)}},
                    status=400,
                )
                return
            result = service.action(payload)
            self._send_json(result, status=200)

    return Handler


def serve(
    *,
    workspace: Path,
    runtime_dir: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
) -> None:
    service = WorkbenchService(workspace, runtime_dir)
    server = ThreadingHTTPServer((host, port), _handler(service))
    address = f"http://{host}:{port}"
    print(f"Quant diagnostics workbench: {address}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(address)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
