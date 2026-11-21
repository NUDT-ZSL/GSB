from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .bundle import bundle_summary, load_bundle, write_bundle, write_revision_bundle
from .config import LabConfig
from .demo import DemoRunner
from .errors import DeployError, E_SNAPSHOT_ROLLBACK, STAGES
from .faults import FaultInjector
from .pipeline import Pipeline
from .store import SnapshotStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_INDEX = Path(__file__).resolve().parent / "static" / "index.html"


def _delete_sqlite(path: Path) -> None:
    for candidate in path.parent.glob(path.name + "*"):
        if candidate.is_file():
            candidate.unlink()


class AppContext:
    def __init__(
        self,
        *,
        bundle_path: str | Path,
        db_path: str | Path,
        config: LabConfig,
    ) -> None:
        self.bundle_path = Path(bundle_path)
        self.db_path = Path(db_path)
        self.config = config
        self.work_dir = self.db_path.parent / "web_demo"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.last_operation: dict[str, Any] | None = None
        self._ensure_baseline()

    def _ensure_baseline(self) -> None:
        if not (self.bundle_path / "manifest.jsonl").exists():
            write_bundle(
                self.bundle_path,
                calib_samples=self.config.calib_samples,
                force=True,
            )
        store = SnapshotStore(self.db_path)
        latest = store.latest_generation()
        if latest is not None:
            persisted_root = latest["manifest"].get("bundle_root")
            if persisted_root:
                candidate = Path(persisted_root)
                if (candidate / "manifest.jsonl").exists():
                    self.bundle_path = candidate
        if latest is None:
            try:
                result = Pipeline(
                    self.bundle_path,
                    self.db_path,
                    config=self.config,
                    force_new=True,
                ).run()
                self.last_operation = {
                    "kind": "startup_baseline",
                    "ok": True,
                    "generation": result["generation"],
                    "progress_version": result["progress_version"],
                    "prediction_digest": result["prediction_digest"],
                }
            except DeployError as error:
                self.last_operation = {
                    "kind": "startup_baseline",
                    "ok": False,
                    "error": error.to_record(),
                }

    def _pipeline(self, *, faults: FaultInjector | None = None, force_new: bool = False, **kwargs: Any) -> Pipeline:
        return Pipeline(
            self.bundle_path,
            self.db_path,
            config=self.config,
            faults=faults,
            force_new=force_new,
            **kwargs,
        )

    def state(self) -> dict[str, Any]:
        with self._lock:
            pipeline = self._pipeline()
            state = pipeline.state()
        demo_report = self.work_dir / "demo_report.json"
        state["demo_report"] = (
            json.loads(demo_report.read_text(encoding="utf-8"))
            if demo_report.exists()
            else None
        )
        state["last_operation"] = self.last_operation
        state["capabilities"] = {
            "stages": list(STAGES),
            "injection_kinds": {
                "model_validation": ["truncate_weight", "truncate_manifest"],
                "quantization": [
                    "unknown_quant_dtype",
                    "invalid_scale_hint",
                    "invalid_zero_point",
                ],
                "calibration": ["calib_sample_mismatch", "calib_truncated"],
                "inference": ["input_dim_mismatch"],
            },
        }
        return state

    def run(self) -> dict[str, Any]:
        with self._lock:
            try:
                result = self._pipeline().run()
                self.last_operation = {
                    "kind": "run",
                    "ok": True,
                    "generation": result["generation"],
                    "progress_version": result["progress_version"],
                    "prediction_digest": result["prediction_digest"],
                    "recovery": result["recovery"],
                }
                return {"ok": True, "result": result}
            except DeployError as error:
                self.last_operation = {
                    "kind": "run",
                    "ok": False,
                    "error": error.to_record(),
                }
                return {"ok": False, "error": error.to_record()}

    def demo(self) -> dict[str, Any]:
        with self._lock:
            try:
                report = DemoRunner(
                    bundle_path=self.bundle_path,
                    work_dir=self.work_dir,
                    config=self.config,
                ).run()
                self.last_operation = {
                    "kind": "demo",
                    "ok": report["ok"],
                    "summary": {
                        "crash_exit": report["crash_recovery"]["forced_exit_code"],
                        "prediction_json_byte_equal": report["crash_recovery"][
                            "prediction_bytes_equal"
                        ],
                        "affect_set": report["revision_update"]["affected_layers"],
                        "reuse_set": report["revision_update"]["reused_layers"],
                        "degradations": report["budget_demo"][
                            "degradation_categories"
                        ],
                    },
                }
                return {"ok": report["ok"], "report": report}
            except Exception as exc:
                error = {
                    "code": getattr(exc, "code", type(exc).__name__),
                    "message": str(exc),
                }
                self.last_operation = {"kind": "demo", "ok": False, "error": error}
                return {"ok": False, "error": error}

    def revision(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            bundle = load_bundle(self.bundle_path)
            layers = bundle.layer_by_id
            layer_id = str(payload.get("layer_id") or bundle.layers[len(bundle.layers) // 2].layer_id)
            if layer_id not in layers:
                return self._fail(
                    "revision",
                    DeployError(
                        "E_UNKNOWN_LAYER",
                        "model_validation",
                        f"unknown layer {layer_id}",
                        layer_id=layer_id,
                    ),
                )
            old_revision = layers[layer_id].weight_revision
            requested = int(payload.get("weight_revision") or old_revision + 1)
            if requested <= old_revision:
                return self._fail(
                    "revision",
                    DeployError(
                        E_SNAPSHOT_ROLLBACK,
                        "model_validation",
                        f"weight_revision must increase above {old_revision}",
                        layer_id=layer_id,
                        field="weight_revision",
                    ),
                )
            generation = self._pipeline().store.latest_version()
            out = (
                self.db_path.parent
                / "web_bundles"
                / f"v{generation}_{layer_id}_r{requested}"
            )
            try:
                revision_bundle = write_revision_bundle(
                    self.bundle_path,
                    out,
                    layer_id=layer_id,
                    weight_revision=requested,
                )
                result = Pipeline(
                    revision_bundle.root,
                    self.db_path,
                    config=self.config,
                ).run()
                consistency = Pipeline(
                    revision_bundle.root,
                    self.db_path,
                    config=self.config,
                ).verify_consistency()
                self.bundle_path = revision_bundle.root
                self.last_operation = {
                    "kind": "revision",
                    "ok": True,
                    "layer_id": layer_id,
                    "old_revision": old_revision,
                    "new_revision": requested,
                    "generation": result["generation"],
                    "progress_version": result["progress_version"],
                    "invalidation": result["invalidation"],
                    "consistent": consistency["ok"],
                }
                return {"ok": True, "result": result, "consistency": consistency}
            except DeployError as error:
                return self._fail("revision", error)

    def calibrate(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            bundle = load_bundle(self.bundle_path)
            layer_id = str(payload.get("layer_id") or bundle.layers[1].layer_id)
            if layer_id not in bundle.layer_by_id:
                return self._fail(
                    "calibrate",
                    DeployError(
                        "E_UNKNOWN_LAYER",
                        "calibration",
                        f"unknown layer {layer_id}",
                        layer_id=layer_id,
                    ),
                )
            latest = self._pipeline().store.latest_generation()
            old_revision = (
                int(latest["calibration_config"]["revisions"].get(layer_id, 0))
                if latest
                else 0
            )
            requested = int(payload.get("revision") or old_revision + 1)
            if requested <= old_revision:
                return self._fail(
                    "calibrate",
                    DeployError(
                        E_SNAPSHOT_ROLLBACK,
                        "calibration",
                        f"calibration revision must increase above {old_revision}",
                        layer_id=layer_id,
                        field="calibration_revision",
                    ),
                )
            update: dict[str, Any] = {"revision": requested}
            if payload.get("seed") is not None:
                update["seed"] = int(payload["seed"])
            try:
                pipeline = self._pipeline(calibration_updates={layer_id: update})
                result = pipeline.run()
                consistency = pipeline.verify_consistency()
                self.last_operation = {
                    "kind": "calibrate",
                    "ok": True,
                    "layer_id": layer_id,
                    "old_revision": old_revision,
                    "new_revision": requested,
                    "generation": result["generation"],
                    "progress_version": result["progress_version"],
                    "invalidation": result["invalidation"],
                    "consistent": consistency["ok"],
                }
                return {"ok": True, "result": result, "consistency": consistency}
            except DeployError as error:
                return self._fail("calibrate", error)

    def inject(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            stage = str(payload.get("stage") or "quantization")
            kind = str(payload.get("kind") or "unknown_quant_dtype")
            layer_id = payload.get("layer_id")
            inject_db = self.db_path.parent / "web_inject.sqlite"
            _delete_sqlite(inject_db)
            pipeline: Pipeline | None = None
            try:
                faults = FaultInjector.build(
                    [{"stage": stage, "kind": kind, "layer_id": layer_id}]
                )
                pipeline = Pipeline(
                    self.bundle_path,
                    inject_db,
                    config=self.config,
                    faults=faults,
                    force_new=True,
                )
                pipeline.run()
                operation = {
                    "kind": "inject",
                    "ok": True,
                    "code": None,
                    "stage": stage,
                    "message": "injection did not trigger for the selected target",
                }
                return {"ok": True, "operation": operation}
            except DeployError as error:
                errors = pipeline.store.recent_errors(5) if pipeline else []
                operation = {
                    "kind": "inject",
                    "ok": False,
                    "error": error.to_record(),
                    "recent_errors": errors,
                    "latest_committed_version": (
                        pipeline.store.latest_version() if pipeline else 0
                    ),
                    "unattributed_count": (
                        pipeline.store.unattributed_count() if pipeline else 0
                    ),
                }
                self.last_operation = operation
                return {"ok": False, "operation": operation}

    def concurrency_probe(self) -> dict[str, Any]:
        with self._lock:
            store = SnapshotStore(self.db_path)
            latest = store.latest_generation()
            if latest is None:
                return self._fail(
                    "concurrency",
                    DeployError(
                        "E_INCONSISTENT",
                        "model_validation",
                        "baseline generation is required",
                    ),
                )
            manifest = latest["manifest"]
            layer_ids = [layer["layer_id"] for layer in manifest["layers"]]
            artifacts = [
                {
                    "layer_id": layer_id,
                    "cache_key": f"busy-probe-{layer_id}",
                    "payload": "{}",
                    "disposition": "recomputed",
                }
                for layer_id in layer_ids
            ]
            observed: dict[str, Any]
            with store.writer_guard():
                try:
                    store.commit_stage(
                        generation=latest["generation"],
                        stage="model_validation",
                        completed_layers=layer_ids,
                        artifacts=artifacts,
                        metadata={},
                        manifest=manifest,
                        calibration_config=latest["calibration_config"],
                        invalidation=latest["invalidation"],
                    )
                    observed = {"code": "UNEXPECTED_SUCCESS"}
                except DeployError as error:
                    observed = error.to_record()
            self.last_operation = {
                "kind": "concurrency",
                "ok": observed.get("code") == "E_STAGE_BUSY",
                "observed": observed,
            }
            return {"ok": True, "observed": observed}

    def _fail(self, kind: str, error: DeployError) -> dict[str, Any]:
        operation = {"kind": kind, "ok": False, "error": error.to_record()}
        self.last_operation = operation
        return {"ok": False, "operation": operation}


class Handler(BaseHTTPRequestHandler):
    server_version = "DeployLab/1.0"
    context: AppContext

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[deploylab] {self.address_string()} {fmt % args}")

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        if length > 2_000_000:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._send_bytes(
                    STATIC_INDEX.read_bytes(),
                    "text/html; charset=utf-8",
                )
            elif path == "/api/state":
                self._send_json(self.context.state())
            elif path == "/api/health":
                self._send_json({"ok": True, "service": "deploylab"})
            else:
                self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_json(
                {"ok": False, "error": {"code": type(exc).__name__, "message": str(exc)}},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/run":
                result = self.context.run()
            elif path == "/api/demo":
                result = self.context.demo()
            elif path == "/api/revision":
                result = self.context.revision(payload)
            elif path == "/api/calibrate":
                result = self.context.calibrate(payload)
            elif path == "/api/inject":
                result = self.context.inject(payload)
            elif path == "/api/concurrency":
                result = self.context.concurrency_probe()
            else:
                self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(result)
        except Exception as exc:
            self._send_json(
                {"ok": False, "error": {"code": type(exc).__name__, "message": str(exc)}},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    bundle_path: str | Path,
    db_path: str | Path,
    config: LabConfig,
) -> None:
    context = AppContext(
        bundle_path=bundle_path,
        db_path=db_path,
        config=config,
    )
    handler = type("DeployLabHandler", (Handler,), {"context": context})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"DeployLab UI: http://{host}:{port}")
    print(f"bundle={context.bundle_path} db={context.db_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

