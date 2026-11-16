"""Business error codes used across the workbench.

Every diagnostic anomaly is reported with one of these stable codes so that
traces, session logs and replay results can be compared byte-for-byte.
"""

E_PORT = "E_PORT"          # loopback port occupied / process spawn failure
E_CONFIG = "E_CONFIG"      # cache policy config rejected
E_STALE = "E_STALE"        # origin content update injected / stale entry observed
E_UPSTREAM = "E_UPSTREAM"  # origin unreachable
E_EVICT = "E_EVICT"        # eviction pressure injected / eviction event
E_REPLAY = "E_REPLAY"      # snapshot replay divergence

ALL_CODES = (E_PORT, E_CONFIG, E_STALE, E_UPSTREAM, E_EVICT, E_REPLAY)


class CdnError(Exception):
    """Structured business error."""

    def __init__(self, code, message, location=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.location = location

    def to_dict(self):
        return {"code": self.code, "message": self.message, "location": self.location}
