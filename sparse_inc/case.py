"""Sample manifest parsing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .canonical import sha256_json
from .codes import E_INVALID_REQUEST, E_SHAPE_INVALID
from .csr import CSRMatrix, parse_csr
from .errors import raise_business


SUPPORTED_OPERATIONS = {"spmm", "spspmm"}


@dataclass(frozen=True)
class SampleCase:
    case_id: str
    operation: str
    shape_mnk: tuple[int, int, int]
    dtype: str
    seed: int
    nnz_a: int
    nnz_b: int
    a: CSRMatrix
    b: CSRMatrix
    write_batches: tuple[dict[str, Any], ...]
    raw: dict[str, Any]

    @property
    def m(self) -> int:
        return self.shape_mnk[0]

    @property
    def n(self) -> int:
        return self.shape_mnk[1]

    @property
    def k(self) -> int:
        return self.shape_mnk[2]

    @property
    def base_checksum(self) -> str:
        return sha256_json(
            {
                "case_id": self.case_id,
                "shape_mnk": list(self.shape_mnk),
                "dtype": self.dtype,
                "seed": self.seed,
                "nnz_a": self.nnz_a,
                "nnz_b": self.nnz_b,
                "csr_a": self.a.payload(),
                "csr_b": self.b.payload(),
            }
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "operation": self.operation,
            "shape_mnk": list(self.shape_mnk),
            "dtype": self.dtype,
            "seed": self.seed,
            "nnz_a": self.nnz_a,
            "nnz_b": self.nnz_b,
            "base_checksum": self.base_checksum,
            "write_batch_count": len(self.write_batches),
        }


def parse_case(raw: Any, *, operation_override: str | None = None) -> SampleCase:
    if not isinstance(raw, dict):
        raise_business(
            E_INVALID_REQUEST,
            case_id=None,
            segment="case",
            message="case manifest entry must be an object",
        )
    case_id = raw.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise_business(
            E_INVALID_REQUEST,
            case_id=None,
            segment="case_id",
            message="case_id must be a non-empty string",
        )

    operation = operation_override or raw.get("operation")
    if operation not in SUPPORTED_OPERATIONS:
        raise_business(
            E_INVALID_REQUEST,
            case_id=case_id,
            segment="operation",
            message="operation must be spmm or spspmm",
            operation=operation,
        )

    shape_mnk = _parse_shape(raw.get("shape_mnk"), case_id)
    m, n, k = shape_mnk
    dtype = raw.get("dtype")
    seed = raw.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise_business(
            E_INVALID_REQUEST,
            case_id=case_id,
            segment="seed",
            message="seed must be an integer",
        )
    nnz_a = raw.get("nnz_a")
    nnz_b = raw.get("nnz_b")
    base = raw.get("base_csr")
    if not isinstance(base, dict):
        raise_business(
            E_INVALID_REQUEST,
            case_id=case_id,
            segment="base_csr",
            message="base_csr must contain csr_a and csr_b",
        )
    a = parse_csr(
        base.get("csr_a"),
        expected_shape=(m, k),
        declared_nnz=nnz_a,
        dtype=dtype,
        case_id=case_id,
        segment="csr_a",
    )
    b = parse_csr(
        base.get("csr_b"),
        expected_shape=(k, n),
        declared_nnz=nnz_b,
        dtype=dtype,
        case_id=case_id,
        segment="csr_b",
    )
    writes = raw.get("write_batches")
    if not isinstance(writes, list):
        raise_business(
            E_INVALID_REQUEST,
            case_id=case_id,
            segment="write_batches",
            message="write_batches must be a list",
        )
    return SampleCase(
        case_id=case_id,
        operation=operation,
        shape_mnk=shape_mnk,
        dtype=dtype,
        seed=seed,
        nnz_a=nnz_a,
        nnz_b=nnz_b,
        a=a,
        b=b,
        write_batches=tuple(writes),
        raw=raw,
    )


def load_cases(path: str) -> dict[str, SampleCase]:
    import json

    cases: dict[str, SampleCase] = {}
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise_business(
                    E_INVALID_REQUEST,
                    case_id=None,
                    segment=f"manifest.line_{line_number}",
                    message=f"invalid JSONL line: {exc}",
                )
            case = parse_case(raw)
            if case.case_id in cases:
                raise_business(
                    E_INVALID_REQUEST,
                    case_id=case.case_id,
                    segment="case_id",
                    message="duplicate case_id in manifest",
                )
            cases[case.case_id] = case
    return cases


def _parse_shape(raw: Any, case_id: str) -> tuple[int, int, int]:
    if not isinstance(raw, list) or len(raw) != 3:
        raise_business(
            E_SHAPE_INVALID,
            case_id=case_id,
            segment="shape_mnk",
            message="shape_mnk must be [M, N, K]",
        )
    values: list[int] = []
    for index, item in enumerate(raw):
        if isinstance(item, bool) or not isinstance(item, int):
            raise_business(
                E_SHAPE_INVALID,
                case_id=case_id,
                segment=f"shape_mnk[{index}]",
                message="shape dimensions must be integers",
            )
        values.append(item)
    m, n, k = values
    if m <= 0 or n <= 0 or k <= 0:
        raise_business(
            E_SHAPE_INVALID,
            case_id=case_id,
            segment="shape_mnk",
            message="M, N, and K must all be positive",
            shape_mnk=values,
        )
    return m, n, k
