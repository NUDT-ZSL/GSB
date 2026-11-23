from __future__ import annotations

import json
import mimetypes
from dataclasses import replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .cache import probe_cache_writable
from .errors import MatForgeError, fail
from .hashing import digest_obj
from .plan import BlockPlan
from .png import encode_rgba_png
from .presets import get_preset, preset_names, preset_summaries
from .studio import MaterialStudio

STATIC_ROOT = Path(__file__).with_name("static")


def _json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    try:
        value = json.loads(handler.rfile.read(length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        fail("E_REQUEST_INVALID", "请求 JSON 无法解析", reason=str(error))
    if not isinstance(value, dict):
        fail("E_REQUEST_INVALID", "请求体必须是 JSON 对象")
    return value


def _int(body: dict[str, Any], name: str, default: int) -> int:
    value = body.get(name, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        fail("E_REQUEST_INVALID", f"{name} 必须是整数", field=name)


def _float(body: dict[str, Any], name: str, default: float) -> float:
    value = body.get(name, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        fail("E_REQUEST_INVALID", f"{name} 必须是数字", field=name)


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], studio: MaterialStudio):
        super().__init__(address, StudioHandler)
        self.studio = studio


class StudioHandler(BaseHTTPRequestHandler):
    server: StudioServer

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep the terminal clean; operational errors remain visible in the UI.
        return

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, value: Any) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, error: MatForgeError) -> None:
        status = HTTPStatus.BAD_REQUEST
        if error.code == "E_STAGE_BUSY":
            status = HTTPStatus.CONFLICT
        elif error.code == "E_BLOCK_NOT_FOUND":
            status = HTTPStatus.NOT_FOUND
        self._json(status, error.to_dict())

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            payload = (STATIC_ROOT / "index.html").read_bytes()
            self._send(HTTPStatus.OK, payload, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/state":
            self._json(HTTPStatus.OK, self.server.studio.state())
            return
        if parsed.path == "/api/presets":
            self._json(HTTPStatus.OK, {"ok": True, "presets": preset_summaries()})
            return
        if parsed.path == "/api/texture.png":
            try:
                rgba, manifest = self.server.studio.capture_rgba()
                png = encode_rgba_png(rgba)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-MatForge-Manifest-Digest", digest_obj(manifest))
                self.end_headers()
                self.wfile.write(png)
            except MatForgeError as error:
                self._error(error)
            return
        candidate = (STATIC_ROOT / parsed.path.lstrip("/")).resolve()
        if STATIC_ROOT.resolve() in candidate.parents and candidate.is_file():
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            self._send(HTTPStatus.OK, candidate.read_bytes(), content_type)
            return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "code": "E_NOT_FOUND", "message": "资源不存在"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            body = _json_body(self)
            if parsed.path == "/api/preview":
                result = self.server.studio.preview(
                    _int(body, "block_id", 0),
                    budget_ms=_float(body, "budget_ms", self.server.studio.limits.preview_latency_budget_ms),
                )
                self._json(HTTPStatus.OK, {"ok": True, **result, "state": self.server.studio.state()})
            elif parsed.path == "/api/remaining":
                result = self.server.studio.request_remaining(
                    parallelism=_int(body, "parallelism", self.server.studio.limits.max_parallel_slices),
                    allow_degrade=bool(body.get("allow_degrade", True)),
                )
                self._json(HTTPStatus.OK, {**result, "state": self.server.studio.state()})
            elif parsed.path == "/api/preview-many":
                block_ids = body.get("block_ids", [])
                if not isinstance(block_ids, list):
                    fail("E_REQUEST_INVALID", "block_ids 必须为数组")
                result = self.server.studio.preview_many(
                    [int(value) for value in block_ids],
                    parallelism=_int(body, "parallelism", 2),
                    allow_degrade=bool(body.get("allow_degrade", False)),
                    budget_ms=_float(body, "budget_ms", self.server.studio.limits.preview_latency_budget_ms),
                )
                self._json(HTTPStatus.OK, {**result, "state": self.server.studio.state()})
            elif parsed.path == "/api/verify":
                self._json(HTTPStatus.OK, self.server.studio.verify())
            elif parsed.path == "/api/selfcheck":
                self._json(HTTPStatus.OK, self.server.studio.selfcheck())
            elif parsed.path == "/api/replay":
                self._json(HTTPStatus.OK, self.server.studio.replay_slice(_int(body, "block_id", 0)))
            elif parsed.path == "/api/stale":
                self._json(HTTPStatus.OK, self.server.studio.inspect_stale(_int(body, "block_id", 0)))
            elif parsed.path == "/api/revision":
                studio = self.server.studio
                before = studio.params
                revision = _int(body, "revision", before.revision + 1)
                changes: dict[str, Any] = {}
                if "base_color" in body:
                    color = body["base_color"]
                    if not isinstance(color, list) or len(color) != 3:
                        fail("E_REQUEST_INVALID", "base_color 必须为三个整数")
                    changes["base_color"] = tuple(int(value) for value in color)
                for name in ("roughness", "roughness_contrast", "normal_strength", "saturation", "seam_width", "seam_strength"):
                    if name in body:
                        changes[name] = _float(body, name, float(getattr(before, name)))
                if "name" in body:
                    changes["name"] = str(body["name"])
                if "noise" in body:
                    noise = body["noise"]
                    if not isinstance(noise, dict):
                        fail("E_REQUEST_INVALID", "noise 必须为对象")
                    noise_changes = {}
                    if "octaves" in noise:
                        noise_changes["octaves"] = int(noise["octaves"])
                    for name in ("base_frequency", "lacunarity", "persistence", "phase_x", "phase_y"):
                        if name in noise:
                            noise_changes[name] = float(noise[name])
                    changes["noise"] = replace(before.noise, **noise_changes)
                params = before.with_revision(revision, **changes)
                report = studio.commit_plan(
                    params,
                    studio.plan,
                    generate_suffix=bool(body.get("generate_suffix", True)),
                    parallelism=_int(body, "parallelism", 2),
                    allow_degrade=bool(body.get("allow_degrade", True)),
                )
                self._json(HTTPStatus.OK, {**report, "state": studio.state()})
            elif parsed.path == "/api/preset":
                studio = self.server.studio
                name = str(body.get("name", ""))
                if name not in preset_names():
                    fail("E_PRESET_NOT_FOUND", "材质预设不存在", preset=name, available=preset_names())
                revision = _int(body, "revision", studio.params.revision + 1)
                if revision <= studio.params.revision:
                    fail(
                        "E_REVISION_NOT_MONOTONIC",
                        "切换预设需要更高 revision",
                        current_revision=studio.params.revision,
                        incoming_revision=revision,
                    )
                params = replace(get_preset(name), revision=revision)
                report = studio.commit_plan(
                    params,
                    studio.plan,
                    generate_suffix=bool(body.get("generate_suffix", True)),
                    parallelism=_int(body, "parallelism", 2),
                    allow_degrade=bool(body.get("allow_degrade", True)),
                )
                self._json(HTTPStatus.OK, {**report, "state": studio.state()})
            elif parsed.path == "/api/plan":
                studio = self.server.studio
                plan = BlockPlan(
                    width=_int(body, "width", studio.plan.width),
                    height=_int(body, "height", studio.plan.height),
                    tile_width=_int(body, "tile_width", studio.plan.tile_width),
                    tile_height=_int(body, "tile_height", studio.plan.tile_height),
                )
                params = studio.params
                if "revision" in body:
                    revision = _int(body, "revision", params.revision + 1)
                    params = params.with_revision(revision)
                report = studio.commit_plan(
                    params,
                    plan,
                    generate_suffix=bool(body.get("generate_suffix", False)),
                    parallelism=_int(body, "parallelism", 2),
                    allow_degrade=bool(body.get("allow_degrade", True)),
                )
                self._json(HTTPStatus.OK, {**report, "state": studio.state()})
            elif parsed.path == "/api/cache-probe":
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "probe": probe_cache_writable(self.server.studio.cache.root),
                        "snapshot": self.server.studio.snapshot_store.inspect_file(),
                    },
                )
            else:
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "code": "E_NOT_FOUND", "message": "接口不存在"})
        except MatForgeError as error:
            self.server.studio.last_error = {**error.to_dict(), "at_stage": self.server.studio.stage}
            self._error(error)
        except Exception as error:  # noqa: BLE001  # pragma: no cover - defensive HTTP boundary
            wrapped = MatForgeError("E_INTERNAL", "服务内部错误", {"reason": repr(error)})
            self.server.studio.last_error = wrapped.to_dict()
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, wrapped.to_dict())


def serve(studio: MaterialStudio, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = StudioServer((host, port), studio)
    print(f"MatForge local UI: http://{host}:{port}")
    print(f"Cache root: {studio.cache.root}")
    print(f"Snapshot: {studio.snapshot_store.path}")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

