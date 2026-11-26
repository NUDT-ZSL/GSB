"""Dependency-free local web application (stdlib HTTP server + JSON API)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

import numpy as np

from .errors import KFError


ERROR_STATUS = {
    "E_STAGE_BUSY": 409,
    "E_RESOURCE_LIMIT": 429,
    "E_SNAPSHOT_ROLLBACK": 409,
    "E_BLOCK_MISMATCH": 422,
    "E_INCONSISTENT": 422,
    "E_CLIP_SHAPE": 400,
    "E_NOT_FOUND": 404,
    "E_BAD_REQUEST": 400,
    "E_CHECKSUM_MISMATCH": 500,
}


def encode_bmp(pixels: np.ndarray) -> bytes:
    """Encode an HxWx3 uint8 array as a 24-bit BMP (browser friendly)."""
    arr = np.asarray(pixels, dtype=np.uint8)
    if arr.ndim == 2:
        arr = np.repeat(arr[:, :, None], 3, axis=2)
    if arr.shape[2] == 1:
        arr = np.repeat(arr, 3, axis=2)
    arr = arr[:, :, :3]
    height, width, _ = arr.shape
    bgr = arr[:, :, ::-1]
    row_bytes = width * 3
    padding = (4 - row_bytes % 4) % 4
    stride = row_bytes + padding
    pixel_blob = bytearray()
    pad_bytes = b"\x00" * padding
    for row in range(height - 1, -1, -1):
        pixel_blob.extend(np.ascontiguousarray(bgr[row]).tobytes())
        pixel_blob.extend(pad_bytes)
    pixel_size = len(pixel_blob)
    file_size = 14 + 40 + pixel_size
    header = bytearray()
    header += b"BM"
    header += file_size.to_bytes(4, "little")
    header += (0).to_bytes(4, "little")
    header += (54).to_bytes(4, "little")
    info = bytearray(40)
    info[0:4] = (40).to_bytes(4, "little")
    info[4:8] = width.to_bytes(4, "little", signed=True)
    info[8:12] = height.to_bytes(4, "little", signed=True)
    info[12:14] = (1).to_bytes(2, "little")
    info[14:16] = (24).to_bytes(2, "little")
    info[20:24] = pixel_size.to_bytes(4, "little")
    info[24:28] = (2835).to_bytes(4, "little")
    info[28:32] = (2835).to_bytes(4, "little")
    info[32:36] = (2835).to_bytes(4, "little")
    info[36:40] = (0).to_bytes(4, "little")
    return bytes(header) + bytes(info) + bytes(pixel_blob)


def _frame_bmp(studio: Any, clip_id: str, ordinal: int) -> bytes:
    task = studio.require_task(clip_id)
    manifest = studio.snapshots.current_manifest(clip_id)
    content_hash = None
    if manifest:
        for keyframe in manifest.get("keyframes", []):
            if int(keyframe["ordinal"]) == ordinal:
                content_hash = keyframe["content_hash"]
                break
    if content_hash is None:
        ready = {row["ordinal"]: row["content_hash"] for row in studio.db.list_ready(clip_id)}
        content_hash = ready.get(ordinal)
    if content_hash is None:
        raise KFError("E_NOT_FOUND", f"keyframe ordinal {ordinal} is not ready for {clip_id}")
    pixels = studio._load_pixels(content_hash, tuple(task.frame_shape))
    return encode_bmp(pixels)


def create_handler(studio: Any):
    web_index = Path(__file__).resolve().parent.parent / "web" / "index.html"

    class Handler(BaseHTTPRequestHandler):
        server_version = "KFDiffStudio/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_bmp(self, data: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "image/bmp")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise KFError("E_BAD_REQUEST", f"invalid JSON body: {exc}")

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            try:
                if parsed.path in ("/", "/index.html"):
                    html = web_index.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html)
                elif parsed.path == "/api/state":
                    self._send_json({"ok": True, "state": studio.dashboard()})
                elif parsed.path == "/api/query":
                    self._send_json({"ok": True, "query": studio.query_ready(query["clip_id"])})
                elif parsed.path == "/api/plan":
                    self._send_json({"ok": True, "plan": studio.build_plan(query["clip_id"])})
                elif parsed.path == "/api/consistency":
                    verdict = studio.adjudicate_consistency(
                        query["clip_id"], raise_on_mismatch=False
                    )
                    self._send_json({"ok": True, "verdict": verdict})
                elif parsed.path == "/api/frame":
                    bmp = _frame_bmp(studio, query["clip_id"], int(query["ordinal"]))
                    self._send_bmp(bmp)
                elif parsed.path == "/api/snapshots":
                    self._send_json({"ok": True,
                                     "versions": studio.snapshots.list_versions(query["clip_id"])})
                else:
                    self._send_json({"ok": False, "error": {"code": "E_NOT_FOUND"}}, 404)
            except KFError as exc:
                self._send_json(exc.to_envelope(), ERROR_STATUS.get(exc.code, 400))
            except KeyError as exc:
                self._send_json({"ok": False, "error": {"code": "E_BAD_REQUEST",
                                                         "message": f"missing parameter {exc}"}}, 400)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                body = self._read_body()
                if parsed.path == "/api/generate":
                    if body.get("all"):
                        results = [studio.generate(cid) for cid in sorted(studio.tasks)]
                        self._send_json({"ok": True, "results": _summaries(results)})
                    else:
                        result = studio.generate(body["clip_id"],
                                                 parallel=not body.get("serial", False))
                        self._send_json({"ok": True, "result": _summary(result)})
                elif parsed.path == "/api/revise":
                    applied = studio.apply_revision_event(body["event_id"])
                    clip_id = applied["event"]["clip_id"]
                    result = studio.generate(clip_id) if body.get("generate", True) else None
                    audit = studio.cross_clip_cache_audit(clip_id) if result else None
                    self._send_json({"ok": True, "applied": applied,
                                     "result": _summary(result) if result else None,
                                     "audit": audit})
                elif parsed.path == "/api/selfcheck":
                    from .selfcheck import run_selfcheck

                    report = run_selfcheck(studio, raise_on_error=False)
                    self._send_json({"ok": True, "report": report})
                elif parsed.path == "/api/rollback":
                    studio.snapshots.request_rollback(body["clip_id"], int(body["version"]))
                else:
                    self._send_json({"ok": False, "error": {"code": "E_NOT_FOUND"}}, 404)
            except KFError as exc:
                self._send_json(exc.to_envelope(), ERROR_STATUS.get(exc.code, 400))
            except KeyError as exc:
                self._send_json({"ok": False, "error": {"code": "E_BAD_REQUEST",
                                                         "message": f"missing parameter {exc}"}}, 400)

    return Handler


def _summary(result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if result is None:
        return None
    return {
        "clip_id": result["clip_id"],
        "snapshot_version": result["snapshot_version"],
        "first_block_timing": result.get("first_block_timing"),
        "propagation": result.get("propagation"),
        "plan_summary": {
            "selected_block_grid": result["plan"]["selected_block_grid"],
            "selected_strength": result["plan"]["selected_strength"],
            "status": result["plan"]["status"],
        },
        "degradations": result.get("degradations", []),
    }


def _summaries(results):
    return [_summary(r) for r in results]


def serve(studio: Any, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), create_handler(studio))
    url = f"http://{host}:{port}/"
    print(f"kfdiff studio serving on {url} (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
