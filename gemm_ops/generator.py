"""Deterministic local synthetic matrix generation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from .errors import GemmError


@dataclass
class SyntheticCase:
    shape_id: str
    category: str
    M: int
    N: int
    K: int
    batch: int
    seed: int
    A: np.ndarray
    B: np.ndarray
    C: np.ndarray
    special_values: list[dict[str, Any]]

    def metadata(self) -> dict[str, Any]:
        return {
            "shape_id": self.shape_id,
            "category": self.category,
            "M": self.M,
            "N": self.N,
            "K": self.K,
            "batch": self.batch,
            "seed": self.seed,
            "dtypes": {
                "A": str(self.A.dtype),
                "B": str(self.B.dtype),
                "C": str(self.C.dtype),
            },
            "shapes": {
                "A": list(self.A.shape),
                "B": list(self.B.shape),
                "C": list(self.C.shape),
            },
            "special_values": self.special_values,
            "finite": {
                "A": bool(np.isfinite(self.A).all()),
                "B": bool(np.isfinite(self.B).all()),
                "C": bool(np.isfinite(self.C).all()),
            },
        }


def _case_seed(global_seed: int, spec: Mapping[str, Any]) -> int:
    stable_payload = {
        "global_seed": global_seed,
        "shape_id": spec["shape_id"],
        "M": spec["M"],
        "N": spec["N"],
        "K": spec["K"],
        "batch": spec["batch"],
    }
    digest = hashlib.sha256(
        json.dumps(stable_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "little")


def _place_special_values(
    matrix: np.ndarray,
    rng: np.random.Generator,
    matrix_label: str,
    counts: Mapping[str, int],
) -> list[dict[str, Any]]:
    flat_size = int(matrix.size)
    events: list[dict[str, Any]] = []
    values: dict[str, np.float32] = {
        "zero": np.float32(0.0),
        "subnormal": np.nextafter(np.float32(0.0), np.float32(1.0)),
        "large_finite": np.float32(1.0e8),
    }
    available = np.arange(flat_size, dtype=np.int64)
    for kind in ("zero", "subnormal", "large_finite"):
        count = int(counts.get(kind, 0))
        if count <= 0:
            continue
        if count > available.size:
            raise GemmError(
                "E_CONFIG_INVALID",
                "special value count exceeds matrix size",
                {"matrix": matrix_label, "kind": kind, "count": count},
            )
        positions = rng.choice(available, size=count, replace=False)
        available = available[~np.isin(available, positions)]
        matrix.reshape(-1)[positions] = values[kind]
        for position in positions.tolist():
            events.append(
                {
                    "matrix_label": matrix_label,
                    "kind": kind,
                    "flat_index": int(position),
                    "unraveled_index": [
                        int(item) for item in np.unravel_index(position, matrix.shape)
                    ],
                    "value": float(values[kind]),
                }
            )
    return events


def generate_case(
    spec: Mapping[str, Any],
    global_seed: int,
    *,
    category: str | None = None,
) -> SyntheticCase:
    required = ("shape_id", "M", "N", "K", "batch")
    for field in required:
        if field not in spec:
            raise GemmError(
                "E_CONFIG_INVALID",
                f"shape specification missing {field}",
                {"spec": dict(spec)},
            )
    shape_id = str(spec["shape_id"])
    M, N, K, batch = (int(spec[field]) for field in ("M", "N", "K", "batch"))
    if min(M, N, K, batch) <= 0:
        raise GemmError(
            "E_SHAPE_MISMATCH",
            "shape dimensions and batch must be positive",
            {"shape_id": shape_id, "M": M, "N": N, "K": K, "batch": batch},
        )
    seed = _case_seed(global_seed, spec)
    rng = np.random.default_rng(seed)
    A = (rng.standard_normal((batch, M, K), dtype=np.float32) * np.float32(0.125)).astype(
        np.float32, copy=False
    )
    B = (rng.standard_normal((batch, K, N), dtype=np.float32) * np.float32(0.125)).astype(
        np.float32, copy=False
    )
    C = (rng.standard_normal((batch, M, N), dtype=np.float32) * np.float32(0.0625)).astype(
        np.float32, copy=False
    )
    counts = {
        "zero": int(spec.get("special_zero_count", 2)),
        "subnormal": int(spec.get("special_subnormal_count", 1)),
        "large_finite": int(spec.get("special_large_finite_count", 1)),
    }
    special_values: list[dict[str, Any]] = []
    special_values.extend(_place_special_values(A, rng, "A", counts))
    special_values.extend(_place_special_values(B, rng, "B", counts))
    special_values.extend(_place_special_values(C, rng, "C", counts))
    if not (np.isfinite(A).all() and np.isfinite(B).all() and np.isfinite(C).all()):
        raise GemmError(
            "E_NONFINITE_INPUT",
            "synthetic generator produced a non-finite value",
            {"shape_id": shape_id},
        )
    return SyntheticCase(
        shape_id=shape_id,
        category=category or str(spec.get("category", "unspecified")),
        M=M,
        N=N,
        K=K,
        batch=batch,
        seed=seed,
        A=A,
        B=B,
        C=C,
        special_values=special_values,
    )


def generate_from_config(
    config: Mapping[str, Any],
    *,
    category: str | None = None,
) -> list[SyntheticCase]:
    global_seed = int(config.get("seed", 0))
    cases: list[SyntheticCase] = []
    categories = config.get("categories")
    if not isinstance(categories, list):
        raise GemmError("E_CONFIG_INVALID", "shape config categories must be a list")
    for category_entry in categories:
        if not isinstance(category_entry, Mapping):
            raise GemmError("E_CONFIG_INVALID", "shape category must be an object")
        category_name = str(category_entry.get("name", "unspecified"))
        specs = category_entry.get("specs")
        if not isinstance(specs, list):
            raise GemmError(
                "E_CONFIG_INVALID",
                "shape category specs must be a list",
                {"category": category_name},
            )
        for spec in specs:
            cases.append(generate_case(spec, global_seed, category=category_name))
    return cases
