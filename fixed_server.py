"""Hardened upload service that fixes both defects of ``vuln_server``.

Defences (defence in depth, standard library only):

1. Filenames are rejected when they contain path separators, ``..`` segments,
   NUL bytes or other traversal tricks; the final write path is additionally
   verified to stay inside the ``uploads`` directory (containment check).
2. The image decision no longer trusts the trailing extension: every
   extension token is checked against an executable deny-list, the final
   extension must be on an image allow-list, and the actual content must
   start with a known image signature (magic bytes).
3. All client errors get an explicit 4xx code; unexpected failures get a
   generic 500 whose body never contains a server-side filesystem path.
"""

from __future__ import annotations

import argparse
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common import (
    EXECUTABLE_EXTENSIONS,
    IMAGE_EXTENSIONS,
    ensure_dir,
    extension_of,
    json_response,
    looks_like_image_bytes,
    parse_multipart,
)

UPLOAD_SUBDIR = "uploads"
MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MiB cap for this test service


class UnsafeFilename(ValueError):
    """Raised when a client-supplied filename fails sanitisation."""


def sanitize_filename(filename: str) -> str:
    """Return a safe basename or raise :class:`UnsafeFilename`."""
    if not filename or "\x00" in filename:
        raise UnsafeFilename("empty or NUL-containing filename")
    # Reject anything that looks like a path or a traversal attempt outright.
    if "/" in filename or "\\" in filename or filename.startswith("~"):
        raise UnsafeFilename("path separators or traversal segments not allowed")
    if ".." in filename.split("."):
        raise UnsafeFilename("traversal segments not allowed")
    if os.path.basename(filename) != filename:
        raise UnsafeFilename("nested paths are not allowed")
    safe = filename.strip()
    if not safe or safe in {".", ".."}:
        raise UnsafeFilename("invalid filename")
    # Defence in depth: strip any characters that are illegal on Windows.
    safe = safe.translate({ord(ch): None for ch in '<>:"|?*'})
    if not safe:
        raise UnsafeFilename("filename had no legal characters")
    return safe


def validate_image_name(filename: str) -> None:
    """Reject executable extensions anywhere in the name."""
    tokens = filename.split(".")
    if len(tokens) < 2:
        raise UnsafeFilename("an image extension is required")
    all_exts = [token.lower() for token in tokens[1:]]
    for ext in all_exts:
        if ext in EXECUTABLE_EXTENSIONS:
            raise UnsafeFilename("executable extension is not allowed")
    if extension_of(filename) not in IMAGE_EXTENSIONS:
        raise UnsafeFilename("only image uploads are accepted")


class FixedUploadHandler(BaseHTTPRequestHandler):
    server_version = "FixedUpload/1.0"
    upload_dir: str = ""

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[fixed] %s - %s\n" % (self.address_string(), fmt % args))

    def _fail(self, status: int, message: str) -> None:
        # Generic, path-free error bodies only.
        json_response(self, status, {"error": message})

    def do_GET(self) -> None:
        if self.path != "/" and not self.path.startswith("/?"):
            self._fail(404, "not found")
            return
        body = (
            b'{"service":"fixed-upload","upload_endpoint":"/upload",'
            b'"note":"sanitised filenames, extension allow-list, magic-byte check"}'
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path.rstrip("?/") != "/upload":
            self._fail(404, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._fail(400, "invalid Content-Length")
            return
        if length <= 0:
            self._fail(400, "empty request body")
            return
        if length > MAX_BODY_BYTES:
            self._fail(413, "upload too large")
            return
        body = self.rfile.read(length)
        try:
            parts = parse_multipart(
                body, self.headers.get("Content-Type", "")
            )
        except ValueError:
            self._fail(400, "malformed multipart request")
            return

        saved: list[dict] = []
        try:
            for part in parts:
                result = self._save_safely(part)
                saved.append(result)
        except UnsafeFilename as exc:
            self._fail(400, str(exc))
            return
        except Exception:
            self.log_error("internal error while saving upload", exc_info=True)
            self._fail(500, "internal server error")
            return

        json_response(self, 200, {"saved": saved})

    def do_PUT(self) -> None:
        self._fail(405, "method not allowed")

    def do_DELETE(self) -> None:
        self._fail(405, "method not allowed")

    # -- the hardened save path --------------------------------------------

    def _save_safely(self, part: dict) -> dict:
        raw_name = part.get("filename") or ""
        safe_name = sanitize_filename(raw_name)       # defence 1: name
        validate_image_name(safe_name)                # defence 2a: extensions

        data = part["data"]
        if not looks_like_image_bytes(data):          # defence 2b: content
            raise UnsafeFilename("content is not a recognised image")

        upload_root = os.path.realpath(self.upload_dir)
        target_path = os.path.realpath(os.path.join(upload_root, safe_name))
        # Containment check: the resolved path MUST remain inside uploads.
        if os.path.commonpath([upload_root, target_path]) != upload_root:
            raise UnsafeFilename("resolved path escapes the upload directory")

        ensure_dir(upload_root)
        with open(target_path, "wb") as handle:
            handle.write(data)  # bytes stored exactly as received

        return {
            "filename": safe_name,
            "is_image": True,
            "size": len(data),
        }


def make_server(host: str, port: int, work_dir: str) -> ThreadingHTTPServer:
    upload_dir = os.path.join(work_dir, UPLOAD_SUBDIR)
    ensure_dir(upload_dir)
    handler = type(
        "BoundFixedHandler",
        (FixedUploadHandler,),
        {"upload_dir": upload_dir},
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.upload_dir = upload_dir
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hardened upload service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument(
        "--dir",
        default=os.path.join(os.getcwd(), "fixed_workdir"),
        help="working directory; files are stored under its uploads subfolder",
    )
    args = parser.parse_args(argv)

    ensure_dir(args.dir)
    httpd = make_server(args.host, args.port, args.dir)
    print(
        "FIXED upload service on http://%s:%d (uploads under %s)"
        % (args.host, args.port, httpd.upload_dir)
    )
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
