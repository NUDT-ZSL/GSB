"""Public one-shot operators and a unified sample-case entry point."""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy import sparse

from .case import SampleCase, parse_case
from .config import ComponentConfig
from .csr import CSRMatrix
from .service import SparseIncrementalService


def spmm(a: CSRMatrix, b: CSRMatrix | np.ndarray | sparse.spmatrix) -> np.ndarray:
    """Compute A @ B in float64 where B is treated as a dense operand."""

    if isinstance(b, CSRMatrix):
        b_dense = b.to_dense()
    elif sparse.issparse(b):
        b_dense = np.asarray(b.toarray(), dtype=np.float64)
    else:
        b_dense = np.ascontiguousarray(np.asarray(b, dtype=np.float64))
    if a.shape[1] != b_dense.shape[0]:
        raise ValueError("inner dimensions do not agree for SpMM")
    return np.ascontiguousarray(a.to_scipy() @ b_dense, dtype=np.float64)


def spspmm(a: CSRMatrix, b: CSRMatrix | sparse.spmatrix) -> sparse.csr_matrix:
    """Compute sparse-sparse A @ B and return canonical CSR float64."""

    b_csr = b.to_scipy() if isinstance(b, CSRMatrix) else sparse.csr_matrix(b)
    if a.shape[1] != b_csr.shape[0]:
        raise ValueError("inner dimensions do not agree for SpMSpM")
    return sparse.csr_matrix(a.to_scipy() @ b_csr, dtype=np.float64)


def multiply(
    a: CSRMatrix,
    b: CSRMatrix | np.ndarray | sparse.spmatrix,
    *,
    operation: str,
) -> np.ndarray | sparse.csr_matrix:
    """Unified one-shot dispatcher for SpMM and SpMSpM."""

    if operation == "spmm":
        return spmm(a, b)
    if operation == "spspmm":
        if isinstance(b, np.ndarray):
            return spspmm(a, sparse.csr_matrix(b))
        return spspmm(a, b)
    raise ValueError("operation must be 'spmm' or 'spspmm'")


def execute_case(
    case_or_raw: SampleCase | dict[str, Any],
    *,
    operation: str | None = None,
    config: ComponentConfig | None = None,
    checkpoint_dir: str | None = None,
    resume: bool = False,
) -> SparseIncrementalService:
    """Create a service and apply all manifest write batches in order."""

    case = (
        case_or_raw
        if isinstance(case_or_raw, SampleCase)
        else parse_case(case_or_raw, operation_override=operation)
    )
    service = SparseIncrementalService(
        case,
        config,
        checkpoint_dir=checkpoint_dir,
        resume=resume,
    )
    for index in range(service.next_write_index, len(case.write_batches)):
        service.submit_batch(case.write_batches[index], batch_index=index)
    return service
