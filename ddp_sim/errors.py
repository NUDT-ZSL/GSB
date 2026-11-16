"""Business error codes and the SimError exception type."""

E_INIT = "E_INIT"          # invalid seed / dim / worker count, init refused
E_PHASE = "E_PHASE"        # phase violation (broadcast before reduction done)
E_DIVERGE = "E_DIVERGE"    # global gradient byte digest mismatch across workers
E_VERSION = "E_VERSION"    # parameter version skew across workers
E_PORT = "E_PORT"          # loopback port already in use
E_BLOCK = "E_BLOCK"        # block size exceeds shard dimension
E_SNAPSHOT = "E_SNAPSHOT"  # snapshot file corrupt / unreadable
E_TIMEOUT = "E_TIMEOUT"    # real socket timeout on the ring

EXIT_CODES = {
    E_INIT: 2,
    E_PORT: 3,
    E_BLOCK: 4,
    E_SNAPSHOT: 5,
    E_PHASE: 6,
    E_DIVERGE: 7,
    E_VERSION: 8,
    E_TIMEOUT: 9,
}


class SimError(Exception):
    """Business error carrying a stable machine-readable code."""

    def __init__(self, code, message, **context):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.context = context

    def to_dict(self):
        return {"code": self.code, "message": self.message, "context": self.context}
