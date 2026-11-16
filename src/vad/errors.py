"""Stable business codes shared across the VAD component."""

E_AUDIO_FORMAT = "E_AUDIO_FORMAT"
E_NONFINITE_SAMPLE = "E_NONFINITE_SAMPLE"
E_SEG_TOO_SHORT = "E_SEG_TOO_SHORT"
E_GAP_MERGED = "E_GAP_MERGED"
E_CONFIG_MISMATCH = "E_CONFIG_MISMATCH"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_INJECTION_FRAME = "E_INJECTION_FRAME"

ALL_CODES = (
    E_AUDIO_FORMAT,
    E_NONFINITE_SAMPLE,
    E_SEG_TOO_SHORT,
    E_GAP_MERGED,
    E_CONFIG_MISMATCH,
    E_SNAPSHOT_CORRUPT,
    E_INJECTION_FRAME,
)


class VadError(Exception):
    """Domain error carrying a stable business code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
