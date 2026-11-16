"""Origin server simulator.

Serves deterministic sample content with version metadata (ETag +
Last-Modified). Supports fault injection ops driven by the orchestrator:
content updates (E_STALE) and reachability toggling (E_UPSTREAM).

Determinism: versions are logical counters; Last-Modified is derived from a
fixed base epoch plus the version counter, never from wall-clock time.
"""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import json
import os
import sys
import threading

from .errors import E_UPSTREAM
from .protocol import JsonLineServer

BASE_EPOCH = 1700000000  # fixed logical epoch for reproducible Last-Modified


def make_etag(path, version, body):
    digest = hashlib.sha1(f"{path}:{version}:{body}".encode("utf-8")).hexdigest()
    return f'"{digest[:16]}"'


def make_last_modified(version):
    return email.utils.formatdate(BASE_EPOCH + version * 60, usegmt=True)


class OriginState:
    def __init__(self, content_dir):
        self._lock = threading.Lock()
        self.reachable = True
        self.contents = {}
        self.versions = {}
        self.reports = []
        self.content_dir = content_dir
        if content_dir:
            for name in sorted(os.listdir(content_dir)):
                full = os.path.join(content_dir, name)
                if os.path.isfile(full):
                    with open(full, "r", encoding="utf-8") as handle:
                        self.contents["/" + name] = handle.read()
                    self.versions["/" + name] = 1

    def descriptor(self, path):
        version = self.versions[path]
        body = self.contents[path]
        return {
            "status": 200,
            "path": path,
            "body": body,
            "etag": make_etag(path, version, body),
            "last_modified": make_last_modified(version),
            "origin_version": version,
        }

    def handle(self, request):
        op = request.get("op")
        with self._lock:
            if op == "ping":
                return {"ok": True, "role": "origin"}
            if op == "set_reachable":
                self.reachable = bool(request.get("value"))
                return {"ok": True, "reachable": self.reachable}
            if op == "update":
                # Fault injection: origin content update (business code E_STALE).
                path = request["path"]
                body = request["content"]
                self.contents[path] = body
                self.versions[path] = self.versions.get(path, 0) + 1
                return {"ok": True, "origin_version": self.versions[path]}
            if op == "restore":
                # Snapshot restore: wholesale replacement of origin state.
                self.contents = dict(request["contents"])
                self.versions = dict(request["versions"])
                self.reachable = True
                return {"ok": True}
            if op == "state":
                return {
                    "ok": True,
                    "reachable": self.reachable,
                    "contents": dict(self.contents),
                    "versions": dict(self.versions),
                    "reports": list(self.reports),
                }
            if op == "report":
                self.reports.append(request.get("report"))
                return {"ok": True}
            if not self.reachable:
                # Simulated unreachable upstream: connection accepted then
                # dropped without a protocol answer.
                raise ConnectionAbortedError("origin unreachable (injected)")
            if op == "get":
                path = request.get("path")
                if path not in self.contents:
                    return {"ok": True, "status": 404, "path": path}
                return {"ok": True, **self.descriptor(path)}
            if op == "validate":
                path = request.get("path")
                if path not in self.contents:
                    return {"ok": True, "status": 404, "path": path}
                current = self.descriptor(path)
                if request.get("etag") == current["etag"]:
                    return {
                        "ok": True,
                        "status": 304,
                        "path": path,
                        "etag": current["etag"],
                        "last_modified": current["last_modified"],
                        "origin_version": current["origin_version"],
                    }
                return {"ok": True, **current}
            return {"ok": False, "code": "E_OP", "message": f"unknown op {op!r}"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="cdnlab origin simulator")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--content-dir", required=True)
    args = parser.parse_args(argv)

    state = OriginState(args.content_dir)
    server = JsonLineServer(args.port, state.handle, name="origin")
    try:
        port = server.start()
    except OSError as exc:
        print(json.dumps({"ok": False, "code": "E_PORT", "message": str(exc)}), flush=True)
        return 2
    print(json.dumps({"ok": True, "role": "origin", "port": port}), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
