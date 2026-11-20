"""qpipe: local NumPy-only quantisation, calibration and resumable inference."""

from __future__ import annotations

from .errors import (
    ALL_CODES,
    E_CACHE_CORRUPT,
    E_INCONSISTENT,
    E_PACKAGE_FORMAT,
    E_RESOURCE_LIMIT,
    E_SCALE_INVALID,
    E_SHAPE_MISMATCH,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    E_ZP_OUT_OF_RANGE,
    PipelineError,
)
from .models import ResourceBudget, RunContext
from .pipeline import QuantPipeline, PipelineSummary

__all__ = [
    "ALL_CODES",
    "PipelineError",
    "QuantPipeline",
    "PipelineSummary",
    "ResourceBudget",
    "RunContext",
    "E_CACHE_CORRUPT",
    "E_INCONSISTENT",
    "E_PACKAGE_FORMAT",
    "E_RESOURCE_LIMIT",
    "E_SCALE_INVALID",
    "E_SHAPE_MISMATCH",
    "E_SNAPSHOT_ROLLBACK",
    "E_STAGE_BUSY",
    "E_ZP_OUT_OF_RANGE",
]

__version__ = "1.0.0"
