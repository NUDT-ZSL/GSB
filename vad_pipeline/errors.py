"""Structured business error codes returned by the VAD pipeline."""

E_FRAME_COUNT = "E_FRAME_COUNT"
E_PERCENTILE_INVALID = "E_PERCENTILE_INVALID"
E_NOISE_UNESTIMABLE = "E_NOISE_UNESTIMABLE"
E_OVERLAP = "E_OVERLAP"
E_OUT_OF_RANGE = "E_OUT_OF_RANGE"
E_LINEAGE_BROKEN = "E_LINEAGE_BROKEN"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_CACHE_BUSY = "E_CACHE_BUSY"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_SAMPLE_RATE_INVALID = "E_SAMPLE_RATE_INVALID"
E_FRAME_MS_INVALID = "E_FRAME_MS_INVALID"
E_HOP_INVALID = "E_HOP_INVALID"
E_MULTIPLIER_INVALID = "E_MULTIPLIER_INVALID"
E_DUPLICATE_CLIP_ID = "E_DUPLICATE_CLIP_ID"


class VadError(Exception):
    """Business error carrying a stable machine-readable code."""

    def __init__(self, code, message, **details):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}
