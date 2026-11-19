from __future__ import annotations

import argparse
import json
import mimetypes
import platform
import re
import sys
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import numpy as np

from . import __version__
from .errors import BusinessError, ErrorCode
from .imaging import encode_png, make_reference_frame
from .service import PreviewService

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = PROJECT_ROOT / "static"

_JOB_CHUNK = re.compile(r"^/api/jobs/(?P<job_id>[^/]+)/chunk$")
_JOB_CHUNKS = re.compile(r"^/api/jobs/(?P<job_id>[^/]+)/chunks$")
_JOB_COMPLETE = re.compile(r"^/api/jobs/(?P<job_id>[^/]+)/complete$")
_JOB_READY = re.compile(r"^/api/jobs/(?P<job_id>[^/]+)/ready$")
_JOB_FRAME = re.compile(r"^/api/jobs/(?P<job_id>[^/]+)/frames/(?P<index>\d+)\.png$")
_JOB_VIDEO = re.compile(r"^/api/jobs/(?P<job_id>[^/]+)/video\.raw$")
_JOB_MANIFEST = re.compile(r"^/api/jobs/(?P<job_id>[^/]+)/manifest\.json$")
_JOB_LEASE = re.compile(r"^/api/jobs/(?P<job_id>[^/]+)/release$")


class LatentFlowHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], root: Path) -> None:
        self.project_root = root
        self.service = PreviewService(root)
        super().__init__(server_address, LatentFlowHandler)


class LatentFlowHandler(BaseHTTPRequestHandler):
    server: LatentFlowHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(
            f"[web] {self.address_string()} {fmt % args}\n"
        )

    def do_OPTIONS(self) -> None:
        self._send_empty(HTTPStatus.NO_CONTENT)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path in {"/", "/index.html"}:
                self._send_file(STATIC_ROOT / "index.html", "text/html; charset=utf-8")
                return
            if path == "/static/app.js":
                self._send_file(STATIC_ROOT / "app.js", "text/javascript; charset=utf-8")
                return
            if path == "/static/styles.css":
                self._send_file(STATIC_ROOT / "styles.css", "text/css; charset=utf-8")
                return
            if path == "/api/health":
                self._send_json(
                    {
                        "ok": True,
                        "app": "LatentFlow Studio",
                        "version": __version__,
                        "python": platform.python_version(),
                        "numpy": np.__version__,
                        "platform": platform.platform(),
                        "root": str(self.server.project_root),
                        "network": "disabled; all samples are local",
                    }
                )
                return
            if path == "/api/state":
                self._send_json(self.server.service.state())
                return
            if path == "/api/snapshots":
                self._send_json(
                    {
                        "ok": True,
                        "snapshots": self.server.service.snapshot_store.versions(),
                    }
                )
                return
            if path == "/api/snapshots/verify":
                snapshots = self.server.service.snapshot_store.load_all()
                self._send_json(
                    {
                        "ok": True,
                        "verified": {
                            category: {
                                "version": snapshot.version,
                                "checksum": snapshot.checksum,
                            }
                            for category, snapshot in snapshots.items()
                        },
                    }
                )
                return
            if path == "/api/sample-anchor.png":
                anchor = make_reference_frame(192, 108)
                self._send_bytes(
                    encode_png(anchor), "image/png", cache=False
                )
                return
            match = _JOB_FRAME.match(path)
            if match:
                frame = self.server.service.get_frame(
                    match.group("job_id"), int(match.group("index"))
                )
                self._send_bytes(encode_png(frame), "image/png", cache=False)
                return
            match = _JOB_READY.match(path)
            if match:
                job = self.server.service.get_job(match.group("job_id"))
                with job.lock:
                    ready_blocks = [
                        job.ready_blocks[index].metadata()
                        for index in sorted(job.ready_blocks)
                    ]
                    payload = {
                        "ok": True,
                        "job_id": job.job_id,
                        "status": job.status,
                        "block_size": job.plan.block_size,
                        "num_blocks": job.plan.num_blocks,
                        "ready_blocks": ready_blocks,
                        "ready_chunks": len(ready_blocks),
                        "ready_frames": job.ready_frames,
                        "total_frames": job.config.frames,
                        "first_block_actual_ms": job.first_block_actual_ms,
                        "in_flight_chunks": sorted(job.in_flight),
                        "snapshot_versions": {
                            "decode_params": job.decode_snapshot_version,
                            "chunk_plan": job.plan_snapshot_version,
                            "ready_blocks": job.ready_snapshot_version,
                        },
                    }
                self._send_json(payload)
                return
            match = _JOB_VIDEO.match(path)
            if match:
                data, metadata = self.server.service.get_video_bytes(
                    match.group("job_id")
                )
                self.send_response(HTTPStatus.OK)
                self._common_headers("application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="video.rgb"')
                self.send_header("X-Content-SHA256", metadata["sha256"])
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            match = _JOB_MANIFEST.match(path)
            if match:
                job_id = match.group("job_id")
                job = self.server.service.get_job(job_id)
                if job.whole is None:
                    raise BusinessError(
                        ErrorCode.E_INVALID_STATE,
                        "完整视频尚未就绪",
                        {"job_id": job_id},
                    )
                self._send_json(
                    {
                        "ok": True,
                        "job_id": job_id,
                        "video": job.whole.metadata(),
                        "consistency": job.consistency,
                        "anchor_validation": job.anchor_validation,
                    }
                )
                return
            self._send_error(
                BusinessError(
                    ErrorCode.E_NOT_FOUND, "路由不存在", {"path": path}
                ),
                HTTPStatus.NOT_FOUND,
            )
        except BusinessError as exc:
            self._handle_business_error(exc)
        except Exception as exc:  # pragma: no cover - defensive API boundary
            traceback.print_exc()
            self._send_json(
                {
                    "ok": False,
                    "error": {
                        "code": "E_INTERNAL",
                        "message": "内部错误",
                        "details": {"cause": str(exc)},
                    },
                },
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._read_json()
            if path == "/api/jobs":
                self._send_json(self.server.service.create_job(body), HTTPStatus.CREATED)
                return
            if path == "/api/demo/run":
                self._send_json(self.server.service.run_demo(body))
                return
            if path == "/api/reset":
                job_id = body.get("job_id") or self.server.service.active_job_id
                if not job_id:
                    self._send_json({"ok": True, "released": False})
                    return
                self._send_json(self.server.service.release(str(job_id)))
                return
            match = _JOB_CHUNK.match(path)
            if match:
                index = int(body.get("chunk_index", -1))
                self._send_json(
                    self.server.service.decode_chunk(match.group("job_id"), index)
                )
                return
            match = _JOB_CHUNKS.match(path)
            if match:
                indices = body.get("chunk_indices")
                if not isinstance(indices, list):
                    raise BusinessError(
                        ErrorCode.E_CONFIG_INVALID,
                        "chunk_indices 必须为数组",
                        {"field": "chunk_indices"},
                    )
                self._send_json(
                    self.server.service.decode_chunks(
                        match.group("job_id"), [int(index) for index in indices]
                    )
                )
                return
            match = _JOB_COMPLETE.match(path)
            if match:
                compare = body.get("compare_block_size")
                self._send_json(
                    self.server.service.complete_job(
                        match.group("job_id"),
                        None if compare is None else int(compare),
                    )
                )
                return
            match = _JOB_LEASE.match(path)
            if match:
                self._send_json(self.server.service.release(match.group("job_id")))
                return
            self._send_error(
                BusinessError(
                    ErrorCode.E_NOT_FOUND, "路由不存在", {"path": path}
                ),
                HTTPStatus.NOT_FOUND,
            )
        except BusinessError as exc:
            self._handle_business_error(exc)
        except (ValueError, TypeError) as exc:
            self._send_json(
                {
                    "ok": False,
                    "error": {
                        "code": ErrorCode.E_CONFIG_INVALID.value,
                        "message": "请求参数格式无效",
                        "details": {"cause": str(exc)},
                    },
                },
                HTTPStatus.BAD_REQUEST,
            )
        except Exception as exc:  # pragma: no cover - defensive API boundary
            traceback.print_exc()
            self._send_json(
                {
                    "ok": False,
                    "error": {
                        "code": "E_INTERNAL",
                        "message": "内部错误",
                        "details": {"cause": str(exc)},
                    },
                },
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2 * 1024 * 1024:
            raise BusinessError(
                ErrorCode.E_RESOURCE_LIMIT,
                "请求体超过 2 MiB",
                {"limit_bytes": 2 * 1024 * 1024, "actual_bytes": length},
            )
        if length == 0:
            return {}
        data = self.rfile.read(length)
        parsed = json.loads(data.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                "JSON 请求体必须为对象",
                {"actual_type": type(parsed).__name__},
            )
        return parsed

    def _handle_business_error(self, exc: BusinessError) -> None:
        self.server.service.record_external_error(exc)
        status = HTTPStatus.BAD_REQUEST
        if exc.code in {ErrorCode.E_STAGE_BUSY, ErrorCode.E_INVALID_STATE}:
            status = HTTPStatus.CONFLICT
        elif exc.code == ErrorCode.E_NOT_FOUND:
            status = HTTPStatus.NOT_FOUND
        elif exc.code == ErrorCode.E_LATENCY_BUDGET:
            status = HTTPStatus.REQUEST_TIMEOUT
        elif exc.code == ErrorCode.E_RESOURCE_LIMIT:
            status = HTTPStatus.TOO_MANY_REQUESTS
        self._send_error(exc, status)

    def _send_error(self, exc: BusinessError, status: HTTPStatus) -> None:
        self._send_json(exc.to_dict(), status)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8")
        self._send_bytes(data, "application/json; charset=utf-8", status=status, cache=False)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            raise BusinessError(
                ErrorCode.E_NOT_FOUND, "静态文件不存在", {"path": str(path)}
            )
        self._send_bytes(path.read_bytes(), content_type, cache=True)

    def _send_bytes(
        self,
        data: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        cache: bool = True,
    ) -> None:
        self.send_response(status)
        self._common_headers(content_type)
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _common_headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'",
        )

    def _send_empty(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self._common_headers("text/plain")
        self.send_header("Content-Length", "0")
        self.end_headers()


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    root: Path = PROJECT_ROOT,
) -> LatentFlowHTTPServer:
    return LatentFlowHTTPServer((host, port), root.resolve())


def main() -> int:
    parser = argparse.ArgumentParser(description="LatentFlow local studio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = create_server(args.host, args.port)
    print(f"LatentFlow Studio: http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
