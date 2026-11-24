"""Stable business and process exit codes."""

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130
EXIT_DIGEST_RECOVERY_FAILED = 3


class Code:
    E_SCHEMA = "E_SCHEMA"
    E_PARAM = "E_PARAM"
    E_REF = "E_REF"
    E_DUPLICATE = "E_DUPLICATE"
    E_DISABLED = "E_DISABLED"
    E_START = "E_START"
    E_WINDOW = "E_WINDOW"
    E_UNWRAP = "E_UNWRAP"
    E_SCALE = "E_SCALE"
    E_TOLERANCE = "E_TOLERANCE"
    E_CONCURRENCY = "E_CONCURRENCY"
    E_MEMORY_BUDGET = "E_MEMORY_BUDGET"
    E_BAND = "E_BAND"
    E_FILE = "E_FILE"
    E_WAV = "E_WAV"
    E_RATE = "E_RATE"
    E_BITS = "E_BITS"
    E_CHANNELS = "E_CHANNELS"
    E_SEGMENT = "E_SEGMENT"
    E_PEAK = "E_PEAK"
    E_DELAY = "E_DELAY"
    E_PHASE = "E_PHASE"
    E_MEMORY = "E_MEMORY"
    E_DIGEST = "E_DIGEST"
    E_LEDGER = "E_LEDGER"
    E_INTERNAL = "E_INTERNAL"


class EngineError(Exception):
    """Fatal error that is not an isolated per-track business result."""

    def __init__(self, code: str, message: str, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class BusinessFailure(Exception):
    """A per-track E_xxx result that should be isolated from other tracks."""

    def __init__(self, code: str, message: str, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class TaskSchemaError(EngineError):
    def __init__(self, message: str, details=None, code: str = Code.E_SCHEMA):
        super().__init__(code, message, details)


class CooperativeInterrupted(Exception):
    """Raised only at a track or frequency-band boundary."""

    def __init__(self, stage: str, track_id=None, completed_bands=None, completed_tracks=None):
        super().__init__("cooperative termination requested")
        self.stage = stage
        self.track_id = track_id
        self.completed_bands = completed_bands or []
        self.completed_tracks = completed_tracks or []
