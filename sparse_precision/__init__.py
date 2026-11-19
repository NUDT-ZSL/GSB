"""Deterministic sparse matrix multiplication with precision diagnostics."""

from .api import diagnose_error_spectrum, execute_case, spmm, spspmm
from .codes import (
    E_CONFIG_INVALID,
    E_CSR_INVALID,
    E_DTYPE_UNSUPPORTED,
    E_INCONSISTENT,
    E_METADATA_INVALID,
    E_RESOURCE_LIMIT,
    E_SHAPE_INVALID,
    E_TOLERANCE_EXCEEDED,
    OK,
)
from .models import OperatorConfig
from .strategy import decide_strategy

__all__ = [
    "E_CONFIG_INVALID",
    "E_CSR_INVALID",
    "E_DTYPE_UNSUPPORTED",
    "E_INCONSISTENT",
    "E_METADATA_INVALID",
    "E_RESOURCE_LIMIT",
    "E_SHAPE_INVALID",
    "E_TOLERANCE_EXCEEDED",
    "OK",
    "OperatorConfig",
    "decide_strategy",
    "diagnose_error_spectrum",
    "execute_case",
    "spmm",
    "spspmm",
]

__version__ = "0.1.0"

