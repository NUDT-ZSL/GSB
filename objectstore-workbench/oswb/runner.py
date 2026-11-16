"""Helpers to launch the loopback service inside the current process."""

import threading

from .server import StorageService, _ThreadingServer, _make_handler
from .protocol import validate_port


def start_inprocess(host="127.0.0.1", port=9101, base_dir=None, log_dir=None,
                    echo=False, startup_timeout=10.0):
    """Start the real TCP service on a background thread.

    The server socket is bound synchronously before this function returns, so
    callers can connect immediately without racing the listener. Requests
    still travel over a loopback socket, keeping code paths identical to a
    separately launched ``oswb serve`` process.
    """
    validate_port(port)
    service = StorageService(base_dir, log_dir=log_dir, echo=echo)
    ready = threading.Event()
    bound = {"error": None}

    def _serve():
        try:
            service._server = _ThreadingServer(
                (host, port), _make_handler(service))
            ready.set()
            service._server.serve_forever()
        except Exception as error:  # pragma: no cover - startup failure path
            bound["error"] = error
            ready.set()

    thread = threading.Thread(target=_serve, name="oswb-loopback-server",
                              daemon=True)
    thread.start()
    if not ready.wait(startup_timeout):
        raise TimeoutError("loopback service failed to start in time")
    if bound["error"]:
        raise bound["error"]
    return service, thread


def stop_inprocess(service):
    if service is not None and getattr(service, "_server", None) is not None:
        service._server.close_active_requests()
        service._server.shutdown()
        service._server.server_close()
