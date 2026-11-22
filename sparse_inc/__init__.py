"""Deterministic incremental sparse matrix multiplication component."""

from .codes import (
    E_CSR_STRUCT,
    E_CASE_NOT_FOUND,
    E_DTYPE_UNSUPPORTED,
    E_INCONSISTENT,
    E_INVALID_REQUEST,
    E_NNZ_MISMATCH,
    E_RECOVERY_FAILED,
    E_RESOURCE_LIMIT,
    E_SHAPE_INVALID,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_WRITE_BUSY,
    E_WRITE_CONFLICT,
    E_WRITE_CORRUPT,
    E_WRITE_RANGE,
    E_WRITE_STALE,
    OK,
)
from .config import ComponentConfig
from .compute import deviation_report, recompute_blocks
from .csr import CSRMatrix
from .errors import BusinessError
from .case import SampleCase, load_cases, parse_case
from .operators import execute_case, multiply, spmm, spspmm
from .service import SparseIncrementalService

__all__ = [
    "BusinessError",
    "CSRMatrix",
    "ComponentConfig",
    "E_CSR_STRUCT",
    "E_CASE_NOT_FOUND",
    "E_DTYPE_UNSUPPORTED",
    "E_INCONSISTENT",
    "E_INVALID_REQUEST",
    "E_NNZ_MISMATCH",
    "E_RECOVERY_FAILED",
    "E_RESOURCE_LIMIT",
    "E_SHAPE_INVALID",
    "E_SNAPSHOT_CORRUPT",
    "E_SNAPSHOT_ROLLBACK",
    "E_WRITE_BUSY",
    "E_WRITE_CONFLICT",
    "E_WRITE_CORRUPT",
    "E_WRITE_RANGE",
    "E_WRITE_STALE",
    "OK",
    "SampleCase",
    "SparseIncrementalService",
    "deviation_report",
    "execute_case",
    "load_cases",
    "multiply",
    "parse_case",
    "recompute_blocks",
    "spmm",
    "spspmm",
]
