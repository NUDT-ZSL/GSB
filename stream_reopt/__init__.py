"""Runtime adaptive re-optimization for deterministic streaming joins."""

from .engine import RunResult, RuntimeEngine
from .errors import BusinessError, ErrorCode
from .models import (
    BatchSpec,
    ExecutionShape,
    InitialStats,
    JoinPredicate,
    Plan,
    ReoptConfig,
    Scenario,
    TableSpec,
    load_scenario,
    parse_scenario,
)
from .verify import VerifyResult, verify_against_serial

__all__ = [
    "BatchSpec",
    "BusinessError",
    "ErrorCode",
    "ExecutionShape",
    "InitialStats",
    "JoinPredicate",
    "Plan",
    "ReoptConfig",
    "RunResult",
    "RuntimeEngine",
    "Scenario",
    "TableSpec",
    "VerifyResult",
    "load_scenario",
    "parse_scenario",
    "verify_against_serial",
]

