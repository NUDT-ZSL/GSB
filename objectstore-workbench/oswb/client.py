"""Loopback client, bounded concurrency scheduler and upload workflows."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .chunker import build_manifest, read_chunk_bytes
from .errors import BusinessError, E_TRANSPORT
from .protocol import call


def mono_ms():
    """Monotonic millisecond clock used for all scheduling observations."""
    return time.monotonic_ns() // 1_000_000


class StorageClient:
    """Thin framed-RPC client for the loopback storage service."""

    def __init__(self, host="127.0.0.1", port=9101, timeout=30.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def request(self, header, payload=b""):
        try:
            response_header, response_payload = call(
                self.host, self.port, header, payload, timeout=self.timeout)
        except OSError as error:
            raise BusinessError(E_TRANSPORT, f"loopback call failed: {error}",
                                {"op": header.get("op")})
        code = response_header.get("code")
        if code != "OK":
            raise BusinessError(code, response_header.get("error", "business failure"),
                                response_header.get("detail", {}))
        return response_header["result"]

    def ping(self):
        return self.request({"op": "ping"})

    def create_session(self, object_key, manifest, labels=None, session_id=None,
                       expect_version=None):
        return self.request({"op": "create_session", "object_key": object_key,
                             "manifest": manifest, "labels": labels or {},
                             "session_id": session_id,
                             "expect_version": expect_version})

    def get_session(self, session_id):
        return self.request({"op": "get_session", "session_id": session_id})

    def list_sessions(self, state=None):
        return self.request({"op": "list_sessions", "state": state})["sessions"]

    def upload_chunk(self, session_id, index, data):
        return self.request({"op": "upload_chunk", "session_id": session_id,
                             "index": index}, data)

    def verify_session(self, session_id):
        return self.request({"op": "verify_session", "session_id": session_id})

    def commit_session(self, session_id, expect_version=None):
        return self.request({"op": "commit_session", "session_id": session_id,
                             "expect_version": expect_version})

    def abort_session(self, session_id):
        return self.request({"op": "abort_session", "session_id": session_id})

    def get_object(self, object_key):
        return self.request({"op": "get_object", "object_key": object_key})

    def list_versions(self, object_key):
        return self.request({"op": "list_versions", "object_key": object_key})["versions"]

    def get_version(self, object_key, version):
        return self.request({"op": "get_version", "object_key": object_key,
                             "version": version})

    def diff_versions(self, object_key, left_version, right_version):
        return self.request({"op": "diff_versions", "object_key": object_key,
                             "left_version": left_version,
                             "right_version": right_version})

    def audit(self):
        return self.request({"op": "audit"})

    def create_snapshot(self, snapshot_id=None):
        return self.request({"op": "create_snapshot", "snapshot_id": snapshot_id})

    def restore_snapshot(self, snapshot_id):
        return self.request({"op": "restore_snapshot", "snapshot_id": snapshot_id})

    def list_snapshots(self):
        return self.request({"op": "list_snapshots"})["snapshots"]

    def inject_fault(self, fault, **params):
        header = {"op": "inject_fault", "fault": fault}
        header.update(params)
        return self.request(header)
