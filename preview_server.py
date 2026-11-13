#!/usr/bin/env python3
"""Loopback-only document preview service with a known traversal defect.

The service maps ``GET /preview/<name>`` to a file in a shared directory.

Modes
-----
``vulnerable``
    Reproduces the defect under test: the request path is URL-decoded **twice**
    and joined to the shared directory with no containment check.  A
    double-encoded relative path such as ``..%252fsecrets%252fmarker.txt``
    therefore reaches ``<share>/../secrets/marker.txt``.

``fixed``
    The repair: decode exactly once, reject residual percent-encoding,
    backslashes, drive/ADS syntax, control characters and dot segments, then
    resolve the path and require it to stay inside the shared directory.

``partial``
    Only used to self-test the validator's OR semantics: it rejects exactly
    the double-encoded slash form ``..%252f...`` while leaving every other
    equivalent encoding exploitable, i.e. an incomplete fix.

No external network access is required.  The listener binds to a loopback
address (127.0.0.1 by default).
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

PREVIEW_PREFIX = "/preview/"
HEALTH_PATH = "/healthz"
DEFAULT_PORT = 8765

#: Raw prefix blocked by the deliberately incomplete "partial" fix.  Only one
#: of the validator's equivalent encodings starts with this sequence.
PARTIAL_BLOCK_PREFIX = "..%252f"


class TargetRejected(Exception):
    """The fixed service refused a request path before touching the disk."""


def decode_for_vulnerable(raw_name: str) -> str:
    """Decode a request path the way the defective service does (twice)."""

    name = raw_name
    for _ in range(2):
        name = unquote(name)
    return name


def decode_for_fixed(raw_name: str) -> str:
    """Decode exactly once, then enforce a conservative lexical policy."""

    decoded = unquote(raw_name)
    if "%" in decoded:
        raise TargetRejected("residual percent-encoding after one decode")
    if "\\" in decoded:
        raise TargetRejected("backslash separators are not allowed")
    if ":" in decoded:
        raise TargetRejected("drive / alternate-data-stream syntax is not allowed")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in decoded):
        raise TargetRejected("control characters are not allowed")
    parts = decoded.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise TargetRejected("empty or relative dot segment")
    return decoded


def resolve_fixed_target(share_root: Path, raw_name: str) -> Path:
    """Return a file path proven to live inside ``share_root``.

    ``share_root`` must already be resolved.  Raises TargetRejected for any
    path that escapes the shared directory.
    """

    decoded = decode_for_fixed(raw_name)
    candidate = (share_root / Path(*decoded.split("/"))).resolve()
    try:
        candidate.relative_to(share_root)
    except ValueError as exc:
        raise TargetRejected("resolved path escapes the shared directory") from exc
    return candidate


def resolve_vulnerable_target(share_root: Path, raw_name: str) -> Path:
    """Return the path the defective service would open (no checks)."""

    return share_root / decode_for_vulnerable(raw_name)


class PreviewHandler(BaseHTTPRequestHandler):
    server_version = "PathPreview/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # Injected by main() before the server starts.
    share_root: Path = Path(".")
    mode: str = "vulnerable"
    delay: float = 0.0
    delay_every: int = 1
    quiet: bool = False
    _delay_lock = threading.Lock()
    _preview_count = 0

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        if not self.quiet:
            super().log_message(fmt, *args)

    def _send(self, status: HTTPStatus | int, body: bytes, content_type: str) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The validator may have timed out and closed the socket.
            pass

    def _deny(self, reason: str = "access denied") -> None:
        self._send(HTTPStatus.FORBIDDEN, f"403 {reason}\n".encode("utf-8"),
                   "text/plain; charset=utf-8")

    def _not_found(self) -> None:
        self._send(HTTPStatus.NOT_FOUND, b"404 not found\n",
                   "text/plain; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlsplit(self.path)
        path = parsed.path

        if path == HEALTH_PATH:
            self._send(HTTPStatus.OK, b"ok\n", "text/plain; charset=utf-8")
            return

        if not path.startswith(PREVIEW_PREFIX):
            self._send(HTTPStatus.NOT_FOUND, b"404 unknown endpoint\n",
                       "text/plain; charset=utf-8")
            return

        raw_name = path[len(PREVIEW_PREFIX):]

        # Simulate a slow backend.  --delay-every N delays only every Nth
        # preview request (e.g. 2 = alternating fast/slow), which proves the
        # verifier records a timeout and still completes the other probes.
        if self.delay > 0:
            with PreviewHandler._delay_lock:
                PreviewHandler._preview_count += 1
                slow = (PreviewHandler._preview_count % self.delay_every) == 0
            if slow:
                time.sleep(self.delay)

        try:
            if self.mode == "partial" and raw_name.startswith(PARTIAL_BLOCK_PREFIX):
                self._deny("blocked by partial fix")
                return

            if self.mode == "fixed":
                try:
                    target = resolve_fixed_target(self.share_root, raw_name)
                except TargetRejected as exc:
                    self._deny(str(exc))
                    return
            elif self.mode in ("vulnerable", "partial"):
                target = resolve_vulnerable_target(self.share_root, raw_name)
            else:  # pragma: no cover - guarded by argparse choices
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, b"bad mode\n",
                           "text/plain; charset=utf-8")
                return

            if not target.is_file():
                self._not_found()
                return

            try:
                body = target.read_bytes()
            except OSError:
                self._not_found()
                return

            content_type = "application/octet-stream"
            if target.suffix.lower() in {".txt", ".md", ".json", ".log", ".csv"}:
                content_type = "text/plain; charset=utf-8"
            self._send(HTTPStatus.OK, body, content_type)
        except (OSError, ValueError):
            # Never leak internal details or tracebacks to the client.
            self._not_found()


class PreviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="preview_server.py",
        description="Run the local document preview service (loopback only).",
    )
    parser.add_argument("--share-dir", required=True, type=Path,
                        help="directory whose documents may be previewed")
    parser.add_argument("--mode", choices=("vulnerable", "fixed", "partial"),
                        default="vulnerable",
                        help="vulnerable defect (default), repaired service, or "
                             "deliberately incomplete fix for validator self-test")
    parser.add_argument("--host", default="127.0.0.1",
                        choices=("127.0.0.1", "localhost"),
                        help="loopback bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"TCP port; 0 picks a free port (default: {DEFAULT_PORT})")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="delay selected preview responses by N seconds "
                             "(timeout simulation)")
    parser.add_argument("--delay-every", type=int, default=1,
                        help="with --delay, slow down every Nth preview request "
                             "(default: 1 = every request)")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress per-request access logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("refusing non-loopback bind address; use 127.0.0.1")
    if args.delay < 0:
        parser.error("--delay must be >= 0")
    if args.delay_every < 1:
        parser.error("--delay-every must be >= 1")
    if not (0 <= args.port <= 65535):
        parser.error("--port must be in 0..65535")

    share = args.share_dir.expanduser().resolve()
    if not share.is_dir():
        print(f"error: shared directory does not exist: {share}", file=sys.stderr)
        return 2

    PreviewHandler.share_root = share
    PreviewHandler.mode = args.mode
    PreviewHandler.delay = args.delay
    PreviewHandler.delay_every = args.delay_every
    PreviewHandler.quiet = args.quiet

    try:
        server = PreviewServer((args.host, args.port), PreviewHandler)
    except OSError as exc:
        print(f"error: cannot bind {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 2

    host, port = server.server_address[:2]
    print(f"PREVIEW_SERVER listening url=http://{host}:{port} "
          f"mode={args.mode} share_dir={share}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
