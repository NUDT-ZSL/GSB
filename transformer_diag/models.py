from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .errors import BusinessError
from .util import canonical_json_bytes, float_key, is_int, normalize_axis, sanitize_component, sha256_bytes


SUPPORTED_OPS = frozenset({"softmax", "layernorm"})
SUPPORTED_DTYPES = frozenset({"float16", "float32", "float64"})
DTYPE_RANK = {"float16": 1, "float32": 2, "float64": 3}
STAGES = ("validate_plan", "block_stats", "normalize", "output")
STAGE_LABELS = {
    "validate_plan": "输入校验与分块计划",
    "block_stats": "分块统计",
    "normalize": "归一化累加",
    "output": "输出写回",
}


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    op_kind: str
    input_shape: tuple[int, ...]
    block_size: int
    axis: int
    eps: float
    dtype: str
    seed: int
    batch_no: int
    revision: int
    axis_normalized: int = field(init=False)

    def __post_init__(self) -> None:
        normalized, _ = normalize_axis(self.axis, len(self.input_shape), job_id=self.job_id)
        object.__setattr__(self, "axis_normalized", normalized)

    @classmethod
    def from_dict(cls, raw: Any, *, source: str | None = None) -> "JobSpec":
        if not isinstance(raw, dict):
            raise BusinessError(
                "E_JOB_INVALID", "job record must be an object", field=source or "job"
            )

        def required(name: str) -> Any:
            if name not in raw:
                raise BusinessError(
                    "E_JOB_INVALID",
                    f"missing required field {name}",
                    job_id=raw.get("job_id") if isinstance(raw.get("job_id"), str) else None,
                    field=name,
                    details={"source": source},
                )
            return raw[name]

        job_id = required("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise BusinessError("E_JOB_INVALID", "job_id must be a non-empty string", field="job_id")
        sanitize_component(job_id)

        explicit_job = job_id
        op_kind = required("op_kind")
        if not isinstance(op_kind, str) or op_kind not in SUPPORTED_OPS:
            raise BusinessError(
                "E_OP_UNSUPPORTED",
                f"unsupported op_kind {op_kind!r}; expected softmax or layernorm",
                job_id=explicit_job,
                field="op_kind",
                details={"supported": sorted(SUPPORTED_OPS)},
            )

        shape_raw = required("input_shape")
        if not isinstance(shape_raw, (list, tuple)) or not shape_raw:
            raise BusinessError(
                "E_SHAPE_INVALID",
                "input_shape must be a non-empty array of positive integers",
                job_id=explicit_job,
                field="input_shape",
            )
        shape: list[int] = []
        for index, value in enumerate(shape_raw):
            if not is_int(value) or int(value) <= 0:
                raise BusinessError(
                    "E_SHAPE_INVALID",
                    "input_shape entries must be positive integers",
                    job_id=explicit_job,
                    field=f"input_shape[{index}]",
                )
            shape.append(int(value))

        block_size = required("block_size")
        if not is_int(block_size) or int(block_size) <= 0:
            raise BusinessError(
                "E_BLOCK_INVALID",
                "block_size must be a positive integer",
                job_id=explicit_job,
                field="block_size",
            )

        axis = required("axis")
        normalized_axis, _ = normalize_axis(axis, len(shape), job_id=explicit_job)
        del normalized_axis

        eps_raw = required("eps")
        if isinstance(eps_raw, bool):
            raise BusinessError(
                "E_EPS_INVALID", "eps must be a positive finite number", job_id=explicit_job, field="eps"
            )
        try:
            eps = float(eps_raw)
        except (TypeError, ValueError) as exc:
            raise BusinessError(
                "E_EPS_INVALID", "eps must be a positive finite number", job_id=explicit_job, field="eps"
            ) from exc
        if not math.isfinite(eps) or eps <= 0.0:
            raise BusinessError(
                "E_EPS_INVALID",
                "eps must be strictly positive, finite, and non-zero",
                job_id=explicit_job,
                field="eps",
                details={"eps": eps_raw},
            )

        dtype = required("dtype")
        if not isinstance(dtype, str) or dtype not in SUPPORTED_DTYPES:
            raise BusinessError(
                "E_DTYPE_UNSUPPORTED",
                f"unsupported dtype {dtype!r}",
                job_id=explicit_job,
                field="dtype",
                details={"supported": sorted(SUPPORTED_DTYPES)},
            )

        seed = required("seed")
        if not is_int(seed) or int(seed) < 0:
            raise BusinessError(
                "E_SEED_INVALID",
                "seed must be a non-negative integer",
                job_id=explicit_job,
                field="seed",
            )

        batch_no = required("batch_no")
        if not is_int(batch_no) or int(batch_no) < 0:
            raise BusinessError(
                "E_JOB_INVALID",
                "batch_no must be a non-negative integer",
                job_id=explicit_job,
                field="batch_no",
            )

        revision = required("revision")
        if not is_int(revision) or int(revision) < 1:
            raise BusinessError(
                "E_JOB_INVALID",
                "revision must be a positive integer",
                job_id=explicit_job,
                field="revision",
            )

        return cls(
            job_id=explicit_job,
            op_kind=op_kind,
            input_shape=tuple(shape),
            block_size=int(block_size),
            axis=int(axis),
            eps=float(eps),
            dtype=dtype,
            seed=int(seed),
            batch_no=int(batch_no),
            revision=int(revision),
        )

    def to_public(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "op_kind": self.op_kind,
            "input_shape": list(self.input_shape),
            "block_size": self.block_size,
            "axis": self.axis,
            "axis_normalized": self.axis_normalized,
            "eps": self.eps,
            "eps_key": float_key(self.eps),
            "dtype": self.dtype,
            "seed": self.seed,
            "batch_no": self.batch_no,
            "revision": self.revision,
        }

    def semantic_signature(self) -> str:
        payload = {
            "op_kind": self.op_kind,
            "input_shape": list(self.input_shape),
            "block_size": self.block_size,
            "axis": self.axis_normalized,
            "eps_key": float_key(self.eps),
            "dtype": self.dtype,
            "seed": self.seed,
        }
        return sha256_bytes(canonical_json_bytes(payload))

    def input_signature(self) -> str:
        payload = {
            "input_shape": list(self.input_shape),
            "dtype": self.dtype,
            "seed": self.seed,
        }
        return sha256_bytes(canonical_json_bytes(payload))

    def dtype_np(self) -> np.dtype[Any]:
        return np.dtype(self.dtype)


@dataclass(frozen=True)
class ToleranceConfig:
    atol: float = 1e-9
    rtol: float = 1e-8
    stat_atol: float = 1e-10
    stat_rtol: float = 1e-8

    def validate(self) -> None:
        for name in ("atol", "rtol", "stat_atol", "stat_rtol"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise BusinessError(
                    "E_POLICY_INVALID",
                    f"tolerance.{name} must be a non-negative finite number",
                    field=f"tolerance.{name}",
                )
            numeric = float(value)
            if not math.isfinite(numeric) or numeric < 0:
                raise BusinessError(
                    "E_POLICY_INVALID",
                    f"tolerance.{name} must be a non-negative finite number",
                    field=f"tolerance.{name}",
                    details={"value": value},
                )

    def to_public(self) -> dict[str, float]:
        return {
            "atol": self.atol,
            "rtol": self.rtol,
            "stat_atol": self.stat_atol,
            "stat_rtol": self.stat_rtol,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "ToleranceConfig":
        if raw is None:
            config = cls()
        elif not isinstance(raw, dict):
            raise BusinessError(
                "E_POLICY_INVALID", "tolerance must be an object", field="tolerance"
            )
        else:
            for name in ("atol", "rtol", "stat_atol", "stat_rtol"):
                if name in raw and (
                    isinstance(raw[name], bool)
                    or not isinstance(raw[name], (int, float))
                ):
                    raise BusinessError(
                        "E_POLICY_INVALID",
                        f"tolerance.{name} must be a non-negative finite number",
                        field=f"tolerance.{name}",
                    )
            try:
                config = cls(
                    atol=float(raw.get("atol", 1e-9)),
                    rtol=float(raw.get("rtol", 1e-8)),
                    stat_atol=float(raw.get("stat_atol", 1e-10)),
                    stat_rtol=float(raw.get("stat_rtol", 1e-8)),
                )
            except (TypeError, ValueError) as exc:
                raise BusinessError(
                    "E_POLICY_INVALID", f"invalid tolerance value: {exc}", field="tolerance"
                ) from exc
        config.validate()
        return config


@dataclass(frozen=True)
class PolicyConfig:
    strategy: str = "band"
    band: float = 1e-15
    eps_min: float = 1e-12
    block_size_max: int = 1 << 20
    denom_min: float = 1e-300
    subnormal_policy: str = "flush"

    def validate(self) -> None:
        if self.strategy not in {"strict", "band"}:
            raise BusinessError(
                "E_POLICY_INVALID",
                "policy.strategy must be strict or band",
                field="policy.strategy",
                details={"value": self.strategy},
            )
        if isinstance(self.band, bool) or not isinstance(self.band, (int, float)):
            raise BusinessError("E_POLICY_INVALID", "policy.band must be numeric", field="policy.band")
        if not math.isfinite(float(self.band)) or float(self.band) < 0:
            raise BusinessError(
                "E_POLICY_INVALID",
                "policy.band must be a non-negative finite number",
                field="policy.band",
            )
        if (
            isinstance(self.eps_min, bool)
            or not isinstance(self.eps_min, (int, float))
            or not math.isfinite(float(self.eps_min))
            or float(self.eps_min) < 0
        ):
            raise BusinessError(
                "E_POLICY_INVALID",
                "policy.eps_min must be a non-negative finite number",
                field="policy.eps_min",
            )
        if not is_int(self.block_size_max) or int(self.block_size_max) < 1:
            raise BusinessError(
                "E_POLICY_INVALID",
                "policy.block_size_max must be a positive integer",
                field="policy.block_size_max",
            )
        if (
            isinstance(self.denom_min, bool)
            or not isinstance(self.denom_min, (int, float))
            or not math.isfinite(float(self.denom_min))
            or float(self.denom_min) <= 0
        ):
            raise BusinessError(
                "E_POLICY_INVALID",
                "policy.denom_min must be a positive finite number",
                field="policy.denom_min",
            )
        if self.subnormal_policy not in {"flush", "preserve"}:
            raise BusinessError(
                "E_POLICY_INVALID",
                "policy.subnormal_policy must be flush or preserve",
                field="policy.subnormal_policy",
            )

    def to_public(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "band": self.band,
            "eps_min": self.eps_min,
            "block_size_max": self.block_size_max,
            "denom_min": self.denom_min,
            "subnormal_policy": self.subnormal_policy,
        }

    def signature(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.to_public()))

    @classmethod
    def from_dict(cls, raw: Any) -> "PolicyConfig":
        if raw is None:
            config = cls()
        elif not isinstance(raw, dict):
            raise BusinessError(
                "E_POLICY_INVALID", "policy must be an object", field="policy"
            )
        else:
            for name in ("band", "eps_min", "denom_min"):
                if name in raw and (
                    isinstance(raw[name], bool)
                    or not isinstance(raw[name], (int, float))
                ):
                    raise BusinessError(
                        "E_POLICY_INVALID",
                        f"policy.{name} must be numeric",
                        field=f"policy.{name}",
                    )
            try:
                config = cls(
                    strategy=str(raw.get("strategy", "band")),
                    band=float(raw.get("band", 1e-15)),
                    eps_min=float(raw.get("eps_min", 1e-12)),
                    block_size_max=raw.get("block_size_max", 1 << 20),
                    denom_min=float(raw.get("denom_min", 1e-300)),
                    subnormal_policy=str(raw.get("subnormal_policy", "flush")),
                )
            except (TypeError, ValueError) as exc:
                raise BusinessError(
                    "E_POLICY_INVALID", f"invalid policy value: {exc}", field="policy"
                ) from exc
        config.validate()
        return config


@dataclass(frozen=True)
class InjectionSpec:
    stage: str
    code: str = "E_INJECTED"
    job_id: str | None = None

    def validate(self) -> None:
        if self.stage not in STAGES:
            raise BusinessError(
                "E_POLICY_INVALID",
                f"injection stage must be one of {list(STAGES)}",
                field="injection.stage",
                details={"value": self.stage},
            )
        if (
            not self.code.startswith("E_")
            or len(self.code) <= 2
            or not all(
                char.isupper() or char.isdigit() or char == "_"
                for char in self.code[2:]
            )
        ):
            raise BusinessError(
                "E_POLICY_INVALID",
                "injection code must look like E_UPPER_CASE",
                field="injection.code",
            )

    def to_public(self) -> dict[str, Any]:
        return {"stage": self.stage, "code": self.code, "job_id": self.job_id}

    @classmethod
    def parse(cls, raw: str) -> "InjectionSpec":
        parts = raw.split(":", 2)
        if not parts or not parts[0]:
            raise BusinessError(
                "E_POLICY_INVALID", "injection must be STAGE[:CODE[:JOB_ID]]", field="injection"
            )
        stage = parts[0]
        code = parts[1] if len(parts) >= 2 and parts[1] else "E_INJECTED"
        job_id = parts[2] if len(parts) >= 3 and parts[2] else None
        spec = cls(stage=stage, code=code, job_id=job_id)
        spec.validate()
        return spec


@dataclass(frozen=True)
class RuntimeConfig:
    tolerance: ToleranceConfig = field(default_factory=ToleranceConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    degrade_policy: str = "fail"
    injections: tuple[InjectionSpec, ...] = ()

    def validate(self) -> None:
        self.tolerance.validate()
        self.policy.validate()
        if self.degrade_policy not in {"fail", "skip"}:
            raise BusinessError(
                "E_POLICY_INVALID",
                "degrade_policy must be fail or skip",
                field="degrade_policy",
            )
        for injection in self.injections:
            injection.validate()

    def signature(self) -> str:
        return sha256_bytes(
            canonical_json_bytes(
                {
                    "tolerance": self.tolerance.to_public(),
                    "policy": self.policy.to_public(),
                    "degrade_policy": self.degrade_policy,
                }
            )
        )

    def policy_signature(self) -> str:
        return self.policy.signature()

    def tolerance_signature(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.tolerance.to_public()))

    def to_public(self) -> dict[str, Any]:
        return {
            "tolerance": self.tolerance.to_public(),
            "policy": self.policy.to_public(),
            "degrade_policy": self.degrade_policy,
            "injections": [item.to_public() for item in self.injections],
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "RuntimeConfig":
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise BusinessError("E_POLICY_INVALID", "config must be a JSON object", field="config")
        injections: list[InjectionSpec] = []
        raw_injections = raw.get("injections", [])
        if not isinstance(raw_injections, list):
            raise BusinessError(
                "E_POLICY_INVALID", "injections must be an array", field="injections"
            )
        for index, item in enumerate(raw_injections):
            if isinstance(item, str):
                injections.append(InjectionSpec.parse(item))
            elif isinstance(item, dict):
                try:
                    spec = InjectionSpec(
                        stage=str(item["stage"]),
                        code=str(item.get("code", "E_INJECTED")),
                        job_id=str(item["job_id"]) if item.get("job_id") is not None else None,
                    )
                except KeyError as exc:
                    raise BusinessError(
                        "E_POLICY_INVALID",
                        "injection object requires stage",
                        field=f"injections[{index}]",
                    ) from exc
                spec.validate()
                injections.append(spec)
            else:
                raise BusinessError(
                    "E_POLICY_INVALID",
                    "injection entries must be strings or objects",
                    field=f"injections[{index}]",
                )
        config = cls(
            tolerance=ToleranceConfig.from_dict(raw.get("tolerance")),
            policy=PolicyConfig.from_dict(raw.get("policy")),
            degrade_policy=str(raw.get("degrade_policy", "fail")),
            injections=tuple(injections),
        )
        config.validate()
        return config

