"""Public data models used by the component and its reports."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterator, Mapping, Sequence

import numpy as np

from .errors import Diagnostic


SUPPORTED_DTYPES = ("float16", "float32", "float64")
DTYPE_ITEMSIZE = {"float16": 2, "float32": 4, "float64": 8}


@dataclass(frozen=True)
class TaskCase:
    case_id: str
    op_chain: tuple[str, ...]
    input_shape: tuple[int, ...]
    axis_order: tuple[int, ...]
    activation_kind: str
    block_size: int
    eps: float
    dtype: str
    seed: int
    policy: str | Mapping[str, Any] = "fusion_first"
    input_file: str | None = None
    tolerance: Mapping[str, float] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "case_id": self.case_id,
            "op_chain": list(self.op_chain),
            "input_shape": list(self.input_shape),
            "axis_order": list(self.axis_order),
            "activation_kind": self.activation_kind,
            "block_size": self.block_size,
            "eps": self.eps,
            "dtype": self.dtype,
            "seed": self.seed,
            "policy": self.policy,
        }
        if self.input_file is not None:
            out["input_file"] = self.input_file
        if self.tolerance is not None:
            out["tolerance"] = dict(self.tolerance)
        if self.metadata:
            out["metadata"] = dict(self.metadata)
        return out

    def evolved(self, **updates: Any) -> "TaskCase":
        return replace(self, **updates)

    @property
    def numel(self) -> int:
        return int(np.prod(self.input_shape, dtype=np.int64))

    @property
    def output_shape(self) -> tuple[int, ...]:
        return tuple(self.input_shape[axis] for axis in self.axis_order)

    @property
    def policy_mode(self) -> str:
        if isinstance(self.policy, str):
            return self.policy
        return str(self.policy.get("mode", self.policy.get("name", "balanced")))

    @property
    def policy_options(self) -> dict[str, Any]:
        if isinstance(self.policy, Mapping):
            return dict(self.policy)
        return {}


@dataclass(frozen=True)
class ResourceLimits:
    max_resident_bytes: int = 256 * 1024 * 1024
    max_blocks: int = 1_000_000
    max_parallel_cases: int = 8
    max_parallel_recompute_cases: int = 8
    min_block_size: int = 1
    max_block_size: int = 1_000_000
    allow_block_shrink: bool = True
    allow_block_expand: bool = True
    allow_separate_fallback: bool = True
    allow_precision_downgrade: bool = False
    strict: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | "ResourceLimits" | None) -> "ResourceLimits":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in value.items() if k in known})

    def merged(self, value: Mapping[str, Any] | None) -> "ResourceLimits":
        if not value:
            return self
        known = {f.name for f in self.__dataclass_fields__.values()}
        return replace(self, **{k: v for k, v in value.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_resident_bytes": self.max_resident_bytes,
            "max_blocks": self.max_blocks,
            "max_parallel_cases": self.max_parallel_cases,
            "max_parallel_recompute_cases": self.max_parallel_recompute_cases,
            "min_block_size": self.min_block_size,
            "max_block_size": self.max_block_size,
            "allow_block_shrink": self.allow_block_shrink,
            "allow_block_expand": self.allow_block_expand,
            "allow_separate_fallback": self.allow_separate_fallback,
            "allow_precision_downgrade": self.allow_precision_downgrade,
            "strict": self.strict,
        }


@dataclass(frozen=True)
class ParsedCase:
    task: TaskCase
    ops: tuple[str, ...]
    valid: bool
    diagnostics: tuple[Diagnostic, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "ops": list(self.ops),
            "diagnostics": [d.to_dict() for d in self.diagnostics],
        }


@dataclass(frozen=True)
class FusionPair:
    activation_index: int
    transpose_index: int
    activation_op: str
    transpose_op: str
    feasible: bool
    reason_code: str | None
    reasons: tuple[str, ...] = ()
    checks: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "activation_index": self.activation_index,
            "transpose_index": self.transpose_index,
            "activation_op": self.activation_op,
            "transpose_op": self.transpose_op,
            "feasible": self.feasible,
            "reason_code": self.reason_code,
            "reasons": list(self.reasons),
            "checks": dict(self.checks),
        }


@dataclass
class CasePlan:
    case_id: str
    fuse: bool
    path: str
    feasible: bool
    requested_block_size: int
    effective_block_size: int
    effective_dtype: str
    precision_tier: str
    priority: list[str]
    metrics: dict[str, Any]
    reasons: list[str]
    fallback_category: str | None = None
    upstream_code: str | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    block_count: int = 0
    waves: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "fuse": self.fuse,
            "path": self.path,
            "feasible": self.feasible,
            "requested_block_size": self.requested_block_size,
            "effective_block_size": self.effective_block_size,
            "effective_dtype": self.effective_dtype,
            "precision_tier": self.precision_tier,
            "priority": list(self.priority),
            "metrics": dict(self.metrics),
            "reasons": list(self.reasons),
            "fallback_category": self.fallback_category,
            "upstream_code": self.upstream_code,
            "block_count": self.block_count,
            "waves": self.waves,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
        }


@dataclass
class CaseOutcome:
    case_id: str
    status: str
    parsed: ParsedCase | None
    pair: FusionPair | None
    plan: CasePlan | None
    input_sha256: str | None = None
    input_shape: tuple[int, ...] | None = None
    input_dtype: str | None = None
    intermediate_digest: str | None = None
    separate_intermediate_digest: str | None = None
    fused_output_digest: str | None = None
    separate_output_digest: str | None = None
    reference_digest: str | None = None
    committed_digest: str | None = None
    fused_output: np.ndarray | None = None
    separate_output: np.ndarray | None = None
    reference_output: np.ndarray | None = None
    committed_output: np.ndarray | None = None
    input_array: np.ndarray | None = field(default=None, repr=False)
    equivalence: dict[str, Any] = field(default_factory=dict)
    cache: dict[str, Any] = field(default_factory=dict)
    snapshot: dict[str, Any] = field(default_factory=dict)
    block_boundaries: list[tuple[int, int]] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok" and not any(d.severity == "error" for d in self.diagnostics)

    def to_dict(self, include_arrays: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "case_id": self.case_id,
            "status": self.status,
            "input": {
                "sha256": self.input_sha256,
                "shape": list(self.input_shape) if self.input_shape is not None else None,
                "dtype": self.input_dtype,
            },
            "parsed": self.parsed.to_dict() if self.parsed else None,
            "pair": self.pair.to_dict() if self.pair else None,
            "plan": self.plan.to_dict() if self.plan else None,
            "digests": {
                "fused_intermediate": self.intermediate_digest,
                "separate_intermediate": self.separate_intermediate_digest,
                "fused_output": self.fused_output_digest,
                "separate_output": self.separate_output_digest,
                "reference": self.reference_digest,
                "committed": self.committed_digest,
            },
            "blocks": {
                "count": len(self.block_boundaries),
                "boundaries": [list(x) for x in self.block_boundaries],
            },
            "equivalence": self.equivalence,
            "cache": self.cache,
            "snapshot": self.snapshot,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "extra": self.extra,
        }
        if include_arrays:
            for name in (
                "fused_output",
                "separate_output",
                "reference_output",
                "committed_output",
            ):
                value = getattr(self, name)
                out[name] = value.tolist() if value is not None else None
        return out


@dataclass
class ExecutionReport:
    cases: list[CaseOutcome] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    manifest: str | None = None
    resource: dict[str, Any] = field(default_factory=dict)
    stage_case_sets: dict[str, list[str]] = field(default_factory=dict)
    selfcheck: dict[str, Any] = field(default_factory=dict)
    cache_summary: dict[str, Any] = field(default_factory=dict)
    snapshot_summary: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(d.severity == "error" for d in self.diagnostics) and all(c.ok for c in self.cases)

    @property
    def case_map(self) -> dict[str, CaseOutcome]:
        return {case.case_id: case for case in self.cases}

    def __getitem__(self, key: str) -> Any:
        if key == "cases":
            return self.cases
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(
            (
                "ok",
                "cases",
                "diagnostics",
                "manifest",
                "resource",
                "stage_case_sets",
                "selfcheck",
                "cache_summary",
                "snapshot_summary",
            )
        )

    def to_dict(self, include_arrays: bool = False) -> dict[str, Any]:
        error_diags = [d for d in self.diagnostics if d.severity == "error"]
        warning_diags = [d for d in self.diagnostics if d.severity != "error"]
        return {
            "ok": self.ok,
            "summary": {
                "case_count": len(self.cases),
                "ok_count": sum(1 for c in self.cases if c.ok),
                "error_count": sum(1 for c in self.cases if not c.ok),
                "fused_path_count": sum(1 for c in self.cases if c.plan and c.plan.path == "fused"),
                "separate_path_count": sum(
                    1 for c in self.cases if c.plan and c.plan.path == "separate"
                ),
                "error_diagnostics": len(error_diags),
                "warning_diagnostics": len(warning_diags),
            },
            "manifest": self.manifest,
            "resource": self.resource,
            "stage_case_sets": self.stage_case_sets,
            "cases": [case.to_dict(include_arrays=include_arrays) for case in self.cases],
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "cache": self.cache_summary,
            "snapshot": self.snapshot_summary,
            "selfcheck": self.selfcheck,
            "extra": self.extra,
        }
