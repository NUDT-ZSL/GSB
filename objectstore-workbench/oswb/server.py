"""Loopback TCP storage service simulation process."""

import logging
import socket
import socketserver
import threading

from .errors import BusinessError
from .logging_utils import configure_logger, log_event
from .protocol import recv_frame, send_frame, validate_port
from .storage import Storage


class _ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._active_requests = set()
        self._requests_lock = threading.Lock()

    def register_request(self, request):
        with self._requests_lock:
            self._active_requests.add(request)

    def unregister_request(self, request):
        with self._requests_lock:
            self._active_requests.discard(request)

    def close_active_requests(self):
        with self._requests_lock:
            requests = list(self._active_requests)
        for request in requests:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                request.close()
            except OSError:
                pass


class StorageService:
    """Owns a :class:`Storage` instance and dispatches framed requests."""

    def __init__(self, base_dir, log_dir=None, echo=False):
        self.logger = configure_logger("oswb.server", log_dir=log_dir,
                                       level=logging.INFO, echo=echo)
        self.storage = Storage(base_dir, logger=self.logger)
        self._server = None
        self._thread = None

    def dispatch(self, header, payload):
        op = header.get("op")
        handler = getattr(self, f"_op_{op}", None)
        if handler is None:
            raise BusinessError("E_PARAM", f"unknown op: {op}")
        return handler(header, payload)

    def _op_ping(self, header, payload):
        return {"pong": True, "db": self.storage.db_path}

    def _op_create_session(self, header, payload):
        return self.storage.create_session(
            header["object_key"], header["manifest"],
            labels=header.get("labels"), session_id=header.get("session_id"),
            expect_version=header.get("expect_version"),
        )

    def _op_get_session(self, header, payload):
        return self.storage.get_session(header["session_id"])

    def _op_list_sessions(self, header, payload):
        return {"sessions": self.storage.list_sessions(header.get("state"))}

    def _op_upload_chunk(self, header, payload):
        return self.storage.upload_chunk(header["session_id"], header["index"], payload)

    def _op_verify_session(self, header, payload):
        return self.storage.verify_session(header["session_id"])

    def _op_commit_session(self, header, payload):
        return self.storage.commit_session(
            header["session_id"], expect_version=header.get("expect_version"))

    def _op_abort_session(self, header, payload):
        return self.storage.abort_session(header["session_id"])

    def _op_get_object(self, header, payload):
        return self.storage.get_object(header["object_key"])

    def _op_list_versions(self, header, payload):
        return {"versions": self.storage.list_versions(header["object_key"])}

    def _op_get_version(self, header, payload):
        return self.storage.get_version(header["object_key"], header["version"])

    def _op_diff_versions(self, header, payload):
        return self.storage.diff_versions(
            header["object_key"], header["left_version"], header["right_version"])

    def _op_audit(self, header, payload):
        return self.storage.audit()

    def _op_create_snapshot(self, header, payload):
        return self.storage.create_snapshot(header.get("snapshot_id"))

    def _op_restore_snapshot(self, header, payload):
        return self.storage.restore_snapshot(header["snapshot_id"])

    def _op_list_snapshots(self, header, payload):
        return {"snapshots": self.storage.list_snapshots()}

    def _op_inject_fault(self, header, payload):
        fault = header["fault"]
        storage = self.storage
        if fault == "chunk_truncation":
            return storage.inject_chunk_truncation(
                header["session_id"], header["index"], header.get("keep_bytes", 0))
        if fault == "metadata_digest_corruption":
            return storage.inject_metadata_digest_corruption(
                header["session_id"], header["index"])
        if fault == "version_conflict":
            return storage.bump_object_version(header["object_key"])
        raise BusinessError("E_PARAM", f"unknown fault: {fault}")

    def serve_forever(self, host="127.0.0.1", port=9101):
        validate_port(port)
        self._server = _ThreadingServer((host, port), _make_handler(self))
        actual_host, actual_port = self._server.server_address
        log_event(self.logger, logging.INFO, "server_started",
                  host=actual_host, port=actual_port, base_dir=self.storage.base_dir)
        try:
            self._server.serve_forever()
        finally:
            self._server.server_close()

    def shutdown(self):
        if self._server:
            self._server.shutdown()


def _make_handler(service):
    class FrameHandler(socketserver.BaseRequestHandler):
        def handle(self):
            server = self.server
            server.register_request(self.request)
            try:
                header, payload = recv_frame(self.request)
                log_event(service.logger, logging.DEBUG, "request_received",
                          op=header.get("op"))
                try:
                    result = service.dispatch(header, payload)
                    response = {"code": "OK", "result": result}
                except BusinessError as error:
                    response = {"code": error.code, "error": error.message,
                                "detail": error.detail}
                send_frame(self.request, response)
            except Exception as error:  # pragma: no cover - defensive transport path
                log_event(service.logger, logging.ERROR, "request_failed",
                          error=repr(error))
                try:
                    send_frame(self.request,
                               {"code": "E_TRANSPORT", "error": repr(error),
                                "detail": {}})
                except OSError:
                    pass
            finally:
                server.unregister_request(self.request)

    return FrameHandler


def run_server(host, port, base_dir, log_dir=None, echo=False):
    service = StorageService(base_dir, log_dir=log_dir, echo=echo)
    service.serve_forever(host=host, port=port)
    return service
