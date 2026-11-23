"""Business error codes and the single exception type used across the pipeline.

Every validation / runtime failure that the task asks us to distinguish gets a
stable ``E_*`` code.  ``PipelineError`` always carries a machine readable code
plus diagnostic fields so that injected failures can be reproduced and located.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


# --- input validation -------------------------------------------------------
E_VALIDATION = "E1000_VALIDATION"
E_DUPLICATE_NAME = "E1001_DUPLICATE_NAME"
E_DIVISIBILITY = "E1002_DIVISIBILITY"
E_BAD_REFERENCE = "E1003_BAD_REFERENCE"
E_CACHE_DIGEST_MISMATCH = "E1004_CACHE_DIGEST_MISMATCH"

# --- shard generation / cache ----------------------------------------------
E_GEN_DIGEST_MISMATCH = "E2001_GEN_DIGEST_MISMATCH"
E_CACHE_ALGEBRA_ROLLBACK = "E2002_CACHE_ALGEBRA_ROLLBACK"
E_CACHE_WRITE_FAILED = "E2003_CACHE_WRITE_FAILED"

# --- prefetch scheduling ----------------------------------------------------
E_PREFETCH_FAILED = "E3001_PREFETCH_FAILED"
E_PREFETCH_TIMEOUT = "E3002_PREFETCH_TIMEOUT"
E_BUDGET_EXHAUSTED = "E3003_BUDGET_EXHAUSTED"  # informational, used in report

# --- logical clock ----------------------------------------------------------
E_CLOCK_ROLLBACK = "E3004_CLOCK_ROLLBACK"
E_CLOCK_STEPS_EXHAUSTED = "E3005_CLOCK_STEPS_EXHAUSTED"

# --- batch assembly ---------------------------------------------------------
E_BATCH_DIGEST_CONFLICT = "E4001_BATCH_DIGEST_CONFLICT"
E_BATCH_DIVERGENCE = "E4002_BATCH_DIVERGENCE"
E_SAMPLE_COVERAGE = "E4003_SAMPLE_COVERAGE"

# --- persistence / recovery -------------------------------------------------
E_CORRUPT_STATE = "E5001_CORRUPT_STATE"
E_LOCK_TIMEOUT = "E5002_LOCK_TIMEOUT"

# --- forced termination (in-process crash simulation) ----------------------
E_FORCED_CRASH = "E6001_FORCED_CRASH"


@dataclass
class PipelineError(Exception):
    """Raised for every expected business failure (never for programmer bugs)."""

    code: str
    message: str
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - trivial
        detail = ""
        if self.diagnostics:
            detail = " :: " + ",".join(f"{k}={v!r}" for k, v in sorted(self.diagnostics.items()))
        return f"[{self.code}] {self.message}{detail}"


def fail(code: str, message: str, **diagnostics: Any) -> "PipelineError":
    """Construct a :class:`PipelineError` with structured diagnostic fields."""
    return PipelineError(code=code, message=message, diagnostics=dict(diagnostics))
