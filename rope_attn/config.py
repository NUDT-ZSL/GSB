"""任务清单、运行配置与输入/配置校验链路。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np

from .errors import (
    DuplicateCaseError,
    OddHeadDimError,
    UnsupportedDtypeError,
    ValidationError,
)

SUPPORTED_VARIANTS = ("standard", "linear", "ntk")
SUPPORTED_DTYPES = ("float32", "float64")
SUPPORTED_ROUNDING = ("half_to_even", "truncate", "down", "half_up")

# base_theta 声明的合法区间（下界开、上界闭）。
BASE_THETA_RANGE = (1.0, 1.0e9)


@dataclass(frozen=True)
class Task:
    """JSONL 单行对应的注意力任务。"""

    case_id: str
    seq_len: int
    head_dim: int
    num_heads: int
    base_theta: float
    pos_encoding_variant: str
    pos_scale: float
    kv_revision: int
    block_size: int
    layers: int
    causal: bool
    seed: int
    dtype: str = "float32"

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Task":
        known = {key: raw[key] for key in raw if key in cls.__dataclass_fields__}
        return cls(**known)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RunConfig:
    """执行器资源预算、数值策略与恢复策略。"""

    cache_dir: str
    state_dir: str
    workers: int = 2
    # 整段分母还原相对容差。
    tolerance: float = 1.0e-12
    subnormal_protection: bool = True
    rounding: str = "half_to_even"
    # 分块最大值/分母的累加 dtype；float32 时舍入模式才有实际差异。
    stat_dtype: str = "float64"
    # 单次调用分块驻留内存字节上限（单 worker 单块峰值估算）。
    max_block_bytes: int = 64 * 1024 * 1024
    # 缓存条目总量上限。
    max_cache_entries: int = 100_000
    # 并行重算节点数上限。
    max_parallel_nodes: int = 256
    # 超限策略：error -> E_RESOURCE_LIMIT；degrade -> 受限并发并打标。
    resource_policy: str = "error"
    # resume：batch 粒度进度侧车（默认）；strict_stage 只按阶段边界恢复。
    resume_mode: str = "batch"

    def validate(self) -> None:
        if self.rounding not in SUPPORTED_ROUNDING:
            raise ValidationError(
                f"不支持的舍入模式 {self.rounding!r}，允许 {SUPPORTED_ROUNDING}",
                field="rounding",
            )
        if self.stat_dtype not in SUPPORTED_DTYPES:
            raise UnsupportedDtypeError(
                f"统计 dtype {self.stat_dtype!r} 不受支持，允许 {SUPPORTED_DTYPES}",
                field="stat_dtype",
            )
        if self.resource_policy not in ("error", "degrade"):
            raise ValidationError(
                f"资源策略 {self.resource_policy!r} 非法",
                field="resource_policy",
            )
        if self.tolerance <= 0:
            raise ValidationError(
                f"容差必须为正数，收到 {self.tolerance}", field="tolerance"
            )
        for name in ("workers", "max_block_bytes", "max_cache_entries",
                     "max_parallel_nodes"):
            if getattr(self, name) <= 0:
                raise ValidationError(
                    f"{name} 必须为正整数，收到 {getattr(self, name)}", field=name
                )


def _require_int(value: Any, field_name: str, case_id: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(
            f"字段 {field_name} 必须是整数，收到 {value!r}",
            field=field_name,
            case_id=case_id,
        )
    return value


def validate_task_dict(raw: Mapping[str, Any], index: int) -> Task:
    """校验单条任务记录并转为 Task，所有失败带字段与 case_id 归因。"""
    case_id = raw.get("case_id", f"<row {index}>")
    required = [
        "case_id", "seq_len", "head_dim", "num_heads", "base_theta",
        "pos_encoding_variant", "pos_scale", "kv_revision", "block_size",
        "layers", "causal", "seed",
    ]
    missing = [name for name in required if name not in raw]
    if missing:
        raise ValidationError(
            f"任务缺少必填字段 {missing}",
            field=",".join(missing),
            case_id=str(case_id),
        )
    if not isinstance(case_id, str) or not case_id:
        raise ValidationError(
            "case_id 必须是非空字符串", field="case_id", case_id=str(case_id)
        )

    seq_len = _require_int(raw["seq_len"], "seq_len", case_id)
    head_dim = _require_int(raw["head_dim"], "head_dim", case_id)
    num_heads = _require_int(raw["num_heads"], "num_heads", case_id)
    kv_revision = _require_int(raw["kv_revision"], "kv_revision", case_id)
    block_size = _require_int(raw["block_size"], "block_size", case_id)
    layers = _require_int(raw["layers"], "layers", case_id)
    seed = _require_int(raw["seed"], "seed", case_id)

    if seq_len <= 0:
        raise ValidationError(
            f"seq_len 必须为正，收到 {seq_len}", field="seq_len", case_id=case_id
        )
    # head_dim 为奇数会导致半维配对越界，单独业务码。
    if head_dim <= 0 or head_dim % 2 != 0:
        raise OddHeadDimError(
            f"head_dim 必须为正偶数（半维配对需要），收到 {head_dim}",
            field="head_dim",
            case_id=case_id,
        )
    if num_heads <= 0:
        raise ValidationError(
            f"num_heads 必须为正，收到 {num_heads}",
            field="num_heads",
            case_id=case_id,
        )
    low, high = BASE_THETA_RANGE
    base_theta = raw["base_theta"]
    if not isinstance(base_theta, (int, float)) or isinstance(base_theta, bool):
        raise ValidationError(
            f"base_theta 必须是数值，收到 {base_theta!r}",
            field="base_theta",
            case_id=case_id,
        )
    if not (low < float(base_theta) <= high):
        raise ValidationError(
            f"base_theta={base_theta} 越界，合法区间 ({low}, {high}]",
            field="base_theta",
            case_id=case_id,
        )
    variant = raw["pos_encoding_variant"]
    if variant not in SUPPORTED_VARIANTS:
        raise ValidationError(
            f"pos_encoding_variant={variant!r} 不受支持，允许 {SUPPORTED_VARIANTS}",
            field="pos_encoding_variant",
            case_id=case_id,
        )
    pos_scale = raw["pos_scale"]
    if not isinstance(pos_scale, (int, float)) or isinstance(pos_scale, bool):
        raise ValidationError(
            f"pos_scale 必须是数值，收到 {pos_scale!r}",
            field="pos_scale",
            case_id=case_id,
        )
    if float(pos_scale) <= 0.0:
        raise ValidationError(
            f"pos_scale 必须为正数，收到 {pos_scale}",
            field="pos_scale",
            case_id=case_id,
        )
    if kv_revision < 1:
        raise ValidationError(
            f"kv_revision 必须 >=1，收到 {kv_revision}",
            field="kv_revision",
            case_id=case_id,
        )
    if not (1 <= block_size <= seq_len):
        raise ValidationError(
            f"block_size={block_size} 必须落在 [1, seq_len={seq_len}]",
            field="block_size",
            case_id=case_id,
        )
    if layers <= 0:
        raise ValidationError(
            f"layers 必须为正，收到 {layers}", field="layers", case_id=case_id
        )
    if not isinstance(raw["causal"], bool):
        raise ValidationError(
            f"causal 必须是布尔值，收到 {raw['causal']!r}",
            field="causal",
            case_id=case_id,
        )
    dtype = raw.get("dtype", "float32")
    if dtype not in SUPPORTED_DTYPES:
        raise UnsupportedDtypeError(
            f"dtype={dtype!r} 不受支持，允许 {SUPPORTED_DTYPES}",
            field="dtype",
            case_id=case_id,
        )
    return Task(
        case_id=case_id,
        seq_len=seq_len,
        head_dim=head_dim,
        num_heads=num_heads,
        base_theta=float(base_theta),
        pos_encoding_variant=variant,
        pos_scale=float(pos_scale),
        kv_revision=kv_revision,
        block_size=block_size,
        layers=layers,
        causal=bool(raw["causal"]),
        seed=seed,
        dtype=dtype,
    )


def validate_tasks(raw_tasks: Iterable[Mapping[str, Any]]) -> list[Task]:
    """完整校验链路：逐行结构/取值校验 + 全局 case_id 去重。"""
    tasks: list[Task] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_tasks):
        task = validate_task_dict(raw, index)
        if task.case_id in seen:
            raise DuplicateCaseError(
                f"case_id={task.case_id} 在任务清单中重复",
                field="case_id",
                case_id=task.case_id,
            )
        seen.add(task.case_id)
        tasks.append(task)
    if not tasks:
        raise ValidationError("任务清单为空，至少需要一条任务", field="tasks")
    return tasks


def load_tasks(path: str | Path) -> list[Task]:
    raw_tasks = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw_tasks.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValidationError(
                    f"第 {line_number} 行不是合法 JSON：{exc.msg}",
                    field=f"line:{line_number}",
                ) from exc
    return validate_tasks(raw_tasks)


def validate_array_dims(
    task: Task, name: str, array: np.ndarray
) -> None:
    """K/V 替换等入口的数组维度校验，维度不符返回 E_DIM_MISMATCH。"""
    expected = (task.seq_len, task.num_heads, task.head_dim)
    if tuple(array.shape) != expected:
        from .errors import DimensionMismatchError

        raise DimensionMismatchError(
            f"{name} 维度 {tuple(array.shape)} 与期望 {expected} 不符",
            field=name,
            case_id=task.case_id,
            details={"expected": list(expected), "actual": list(array.shape)},
        )
    if str(array.dtype) not in SUPPORTED_DTYPES:
        raise UnsupportedDtypeError(
            f"{name} dtype={array.dtype!s} 不受支持",
            field=name,
            case_id=task.case_id,
        )
