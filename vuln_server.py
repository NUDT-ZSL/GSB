"""Intentionally vulnerable upload service (for local security testing only).

Two deliberate defects, matching the task brief:

1. Path traversal: the client-supplied filename is joined directly onto the
   upload directory, so a name containing ``..`` segments escapes the
   ``uploads`` folder (e.g. ``../escape.txt``).
2. Image-type confusion: the "is this an image?" decision looks only at the
   trailing filename extension, so ``shell.php.jpg`` (a script hidden behind
   a double extension) is accepted and stored byte-for-byte.

Standard library only.  Bind to the loopback interface and run inside a
throwaway working directory.
"""

from __future__ import annotations

import argparse
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common import (
    DISGUISED_FILENAME,
    IMAGE_EXTENSIONS,
    ensure_dir,
    extension_of,
    json_response,
    parse_multipart,
)

UPLOAD_SUBDIR = "uploads"


class VulnerableUploadHandler(BaseHTTPRequestHandler):
    server_version = "VulnUpload/1.0"
    upload_dir: str = ""  # injected by make_server()

    # -- framework plumbing -------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:
        # Keep the console readable; full details still go to stderr.
        sys.stderr.write("[vuln] %s - %s\n" % (self.address_string(), fmt % args))

    def _fail(self, status: int, message: str) -> None:
        # Generic messages only -- never echo exception text or filesystem
        # paths back to the client.
        json_response(self, status, {"error": message})

    # -- routing ------------------------------------------------------------

    def do_GET(self) -> None:
        if self.path != "/" and not self.path.startswith("/?"):
            self._fail(404, "not found")
            return
        body = (
            b'{"service":"vulnerable-upload","upload_endpoint":"/upload",'
            b'"note":"deliberately insecure, loopback testing only"}'
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
                filename = part.get("filename")
                if not filename:
                    self._fail(400, "missing filename")
                    return
                result = self._save_unsafely(filename, part["data"])
                saved.append(result)
        except Exception:
            # Full detail to the local log only; the client gets a generic
            # 500 with no filesystem information.
            self.log_error("internal error while saving upload", exc_info=True)
            self._fail(500, "internal server error")
            return

        json_response(self, 200, {"saved": saved})

    def do_PUT(self) -> None:
        self._fail(405, "method not allowed")

    def do_DELETE(self) -> None:
        self._fail(405, "method not allowed")

    # -- the two vulnerable behaviours -------------------------------------

    def _save_unsafely(self, filename: str, data: bytes) -> dict:
        # DEFECT 1: no sanitisation at all.  os.path.join honors an absolute
        # or relative (..) path supplied by the client, so the write can land
        # outside the uploads directory.
        target_path = os.path.join(self.upload_dir, filename)
        parent = os.path.dirname(target_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target_path, "wb") as handle:
            handle.write(data)  # bytes stored exactly as received

        # DEFECT 2: image decision based on the trailing extension only;
        # content is never inspected, so a disguised script passes.
        is_image = extension_of(filename) in IMAGE_EXTENSIONS
        return {
            "filename": filename,
            "is_image": is_image,
            "size": len(data),
        }


def make_server(host: str, port: int, work_dir: str) -> ThreadingHTTPServer:
    upload_dir = os.path.join(work_dir, UPLOAD_SUBDIR)
    ensure_dir(upload_dir)
    handler = type(
        "BoundVulnHandler",
        (VulnerableUploadHandler,),
        {"upload_dir": upload_dir},
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.upload_dir = upload_dir
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vulnerable upload service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument(
        "--dir",
        default=os.path.join(os.getcwd(), "vuln_workdir"),
        help="working directory; files are stored under its uploads subfolder",
    )
    args = parser.parse_args(argv)

    ensure_dir(args.dir)
    httpd = make_server(args.host, args.port, args.dir)
    print(
        "VULNERABLE upload service on http://%s:%d (uploads under %s)"
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
