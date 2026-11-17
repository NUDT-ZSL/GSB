"""Business error codes and structured logging."""
import json
import os
import threading
import time

E_FORMAT = "E_FORMAT"      # manifest corrupt / unknown version / missing field
E_PORT = "E_PORT"          # node loopback port busy
E_VERSION = "E_VERSION"    # snapshot/commit version conflict
E_CONVERGE = "E_CONVERGE"  # derived digest mismatch or missing receipt
E_REPLAY = "E_REPLAY"      # recovery replay not reproducible


class PlatformError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self):
        return {"error": self.code, "message": self.message, "details": self.details}


class StructuredLogger:
    """Append-only JSON-lines structured log."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)

    def log(self, event, **fields):
        rec = {"ts": round(time.time(), 3), "event": event}
        rec.update(fields)
        line = json.dumps(rec, sort_keys=True, ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return rec

    def log_error(self, err: PlatformError, event="error"):
        return self.log(event, **err.to_dict())
