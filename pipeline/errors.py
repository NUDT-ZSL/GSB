"""Business error codes and the pipeline's typed exception.

All failure codes are stable strings so that reports produced by different
runs (different concurrency, resume points or injected faults) stay
byte-for-byte comparable.
"""

E_SAMPLE_INVALID = "E_SAMPLE_INVALID"
E_RELATION_DANGLING = "E_RELATION_DANGLING"
E_CONFIG_MISMATCH = "E_CONFIG_MISMATCH"
E_IMAGE_TRUNCATED = "E_IMAGE_TRUNCATED"
E_ASSERTION_MISSING = "E_ASSERTION_MISSING"
E_REGION_OUT_OF_RANGE = "E_REGION_OUT_OF_RANGE"
E_NON_FINITE_FEATURE = "E_NON_FINITE_FEATURE"
E_FUSION_ERROR = "E_FUSION_ERROR"
E_SNAPSHOT_INVALID = "E_SNAPSHOT_INVALID"

#: Stage names used both in failure records and in the diagnostic report.
STAGE_LOAD = "load"
STAGE_VISUAL = "visual_feature"
STAGE_ALIGN = "relation_align"
STAGE_FUSION = "fusion"

#: Every code understood by the pipeline.
KNOWN_CODES = (
    E_SAMPLE_INVALID,
    E_RELATION_DANGLING,
    E_CONFIG_MISMATCH,
    E_IMAGE_TRUNCATED,
    E_ASSERTION_MISSING,
    E_REGION_OUT_OF_RANGE,
    E_NON_FINITE_FEATURE,
    E_FUSION_ERROR,
    E_SNAPSHOT_INVALID,
)


class PipelineError(Exception):
    """Failure carrying a stable business code and an execution stage."""

    def __init__(self, code: str, stage: str, message: str):
        if code not in KNOWN_CODES:
            raise ValueError(f"unknown error code: {code}")
        super().__init__(message)
        self.code = code
        self.stage = stage
