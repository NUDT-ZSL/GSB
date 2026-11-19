from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .errors import ErrorCode, Issue


SUPPORTED_DTYPES = frozenset({"float32", "float64"})
MASK_MODES = frozenset({"auto", "causal", "none"})


def _as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    return int(value)


@dataclass(frozen=True)
class AttentionCase:
    case_id: str
    seq_len: int
    head_dim: int
    n_heads: int
    block_size: int
    causal: bool
    dtype: str
    seed: int
    kv_revision: int | tuple[int, ...] = 0
    mask_mode: str | None = None
    scale: float | None = None
    extras: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "dtype", str(self.dtype).lower())
        if self.mask_mode is not None:
            object.__setattr__(self, "mask_mode", str(self.mask_mode).lower())
        if isinstance(self.kv_revision, (list, tuple)):
            object.__setattr__(
                self,
                "kv_revision",
                tuple(int(item) for item in self.kv_revision),
            )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AttentionCase":
        revision = payload.get("kv_revision", 0)
        if isinstance(revision, (list, tuple)):
            normalized_revision: int | tuple[int, ...] = tuple(
                _as_int(item) for item in revision
            )
        else:
            normalized_revision = _as_int(revision)
        scale = payload.get("scale")
        known = {
            "case_id",
            "seq_len",
            "head_dim",
            "n_heads",
            "block_size",
            "causal",
            "dtype",
            "seed",
            "kv_revision",
            "mask_mode",
            "scale",
        }
        extras = {key: value for key, value in payload.items() if key not in known}
        return cls(
            case_id=str(payload.get("case_id", "")),
            seq_len=_as_int(payload.get("seq_len")),
            head_dim=_as_int(payload.get("head_dim")),
            n_heads=_as_int(payload.get("n_heads")),
            block_size=_as_int(payload.get("block_size")),
            causal=bool(payload.get("causal", False)),
            dtype=str(payload.get("dtype", "float64")).lower(),
            seed=_as_int(payload.get("seed")),
            kv_revision=normalized_revision,
            mask_mode=(
                None
                if payload.get("mask_mode") is None
                else str(payload.get("mask_mode")).lower()
            ),
            scale=None if scale is None else float(scale),
            extras=extras,
        )

    @property
    def num_kv_blocks(self) -> int:
        if self.seq_len <= 0 or self.block_size <= 0:
            return 0
        return int(math.ceil(self.seq_len / self.block_size))

    @property
    def effective_mask_mode(self) -> str:
        if self.mask_mode in (None, "auto"):
            return "causal" if self.causal else "none"
        return self.mask_mode

    def revision_for(self, block_index: int) -> int:
        if isinstance(self.kv_revision, tuple):
            if 0 <= block_index < len(self.kv_revision):
                return self.kv_revision[block_index]
            return 0
        return self.kv_revision

    def revisions(self) -> tuple[int, ...]:
        return tuple(self.revision_for(i) for i in range(self.num_kv_blocks))

    def replace(self, **changes: Any) -> "AttentionCase":
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "case_id": self.case_id,
            "seq_len": self.seq_len,
            "head_dim": self.head_dim,
            "n_heads": self.n_heads,
            "block_size": self.block_size,
            "causal": self.causal,
            "dtype": self.dtype,
            "seed": self.seed,
            "kv_revision": (
                list(self.kv_revision)
                if isinstance(self.kv_revision, tuple)
                else self.kv_revision
            ),
        }
        if self.mask_mode is not None:
            payload["mask_mode"] = self.mask_mode
        if self.scale is not None:
            payload["scale"] = self.scale
        payload.update(dict(self.extras))
        return payload


@dataclass
class QKVArrays:
    q: np.ndarray
    k: np.ndarray
    v: np.ndarray

    @classmethod
    def load_npy(
        cls,
        q_path: str | Path,
        k_path: str | Path,
        v_path: str | Path,
    ) -> "QKVArrays":
        return cls(
            q=np.load(Path(q_path), allow_pickle=False),
            k=np.load(Path(k_path), allow_pickle=False),
            v=np.load(Path(v_path), allow_pickle=False),
        )

    def as_float64(self) -> "QKVArrays":
        return QKVArrays(
            q=np.asarray(self.q, dtype=np.float64),
            k=np.asarray(self.k, dtype=np.float64),
            v=np.asarray(self.v, dtype=np.float64),
        )

    def nbytes(self) -> int:
        return int(self.q.nbytes + self.k.nbytes + self.v.nbytes)


@dataclass(frozen=True)
class SoftmaxConfig:
    subnormal_floor: float = 1e-300
    rounding_mode: str = "nearest"
    rounding_precision: int = 52
    atol: float = 1e-10
    rtol: float = 1e-10

    def validate(self, case_id: str | None = None) -> list[Issue]:
        issues: list[Issue] = []
        if not np.isfinite(self.subnormal_floor) or self.subnormal_floor < 0:
            issues.append(
                Issue(
                    ErrorCode.INVALID_CONFIG,
                    "subnormal_floor must be finite and non-negative",
                    case_id=case_id,
                    field="subnormal_floor",
                )
            )
        if self.rounding_mode not in {"nearest", "truncate"}:
            issues.append(
                Issue(
                    ErrorCode.INVALID_CONFIG,
                    "rounding_mode must be 'nearest' or 'truncate'",
                    case_id=case_id,
                    field="rounding_mode",
                )
            )
        if not 0 <= self.rounding_precision <= 52:
            issues.append(
                Issue(
                    ErrorCode.INVALID_CONFIG,
                    "rounding_precision must be in [0, 52]",
                    case_id=case_id,
                    field="rounding_precision",
                )
            )
        if self.atol < 0 or self.rtol < 0:
            issues.append(
                Issue(
                    ErrorCode.INVALID_CONFIG,
                    "atol and rtol must be non-negative",
                    case_id=case_id,
                    field="atol",
                )
            )
        return issues


@dataclass(frozen=True)
class RuntimeConfig:
    max_resident_bytes: int = 512 * 1024 * 1024
    max_cache_entries: int = 20_000
    parallel_nodes: int = 4
    max_parallel_nodes: int = 4
    cache_lock_timeout_s: float = 10.0
    cache_lock_stale_s: float = 60.0
    self_check_enabled: bool = True

    def validate(self, case_id: str | None = None) -> list[Issue]:
        issues: list[Issue] = []
        for field_name in ("max_resident_bytes", "max_cache_entries"):
            if getattr(self, field_name) <= 0:
                issues.append(
                    Issue(
                        ErrorCode.INVALID_CONFIG,
                        f"{field_name} must be positive",
                        case_id=case_id,
                        field=field_name,
                    )
                )
        for field_name in ("parallel_nodes", "max_parallel_nodes"):
            if getattr(self, field_name) <= 0:
                issues.append(
                    Issue(
                        ErrorCode.INVALID_CONFIG,
                        f"{field_name} must be positive",
                        case_id=case_id,
                        field=field_name,
                    )
                )
        if self.cache_lock_timeout_s <= 0 or self.cache_lock_stale_s <= 0:
            issues.append(
                Issue(
                    ErrorCode.INVALID_CONFIG,
                    "cache lock timeouts must be positive",
                    case_id=case_id,
                    field="cache_lock_timeout_s",
                )
            )
        return issues

    @property
    def effective_parallel_nodes(self) -> int:
        return min(self.parallel_nodes, self.max_parallel_nodes)

    @property
    def parallel_limited(self) -> bool:
        return self.parallel_nodes > self.max_parallel_nodes
