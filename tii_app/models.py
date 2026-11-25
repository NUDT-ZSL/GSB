"""Task schema, validation, ingestion, and source ledger metadata."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ETaskInvalid
from .util import read_json, read_jsonl, sha256_obj, stable_u64


SUPPORTED_DTYPES = {"int8", "uint8", "int16", "int32"}


def _normalize_block_plan(value: Any) -> dict[str, int]:
    if isinstance(value, int):
        plan = {"row_block": value}
    elif isinstance(value, str):
        if value.isdigit():
            plan = {"row_block": int(value)}
        else:
            plan = {}
            for part in value.split(","):
                key, separator, raw = part.strip().partition(":")
                if not separator:
                    raise ETaskInvalid(f"invalid block_plan segment: {part!r}")
                plan[key.strip()] = int(raw.strip())
    elif isinstance(value, dict):
        plan = dict(value)
    else:
        raise ETaskInvalid("block_plan must be an object, integer, or key:value string")

    row_block = int(plan.get("row_block", plan.get("rows", 64)))
    col_block = int(plan.get("col_block", plan.get("cols", 1_000_000)))
    priority = int(plan.get("priority", 0))
    if row_block <= 0 or col_block <= 0:
        raise ETaskInvalid("block_plan row_block and col_block must be positive")
    return {
        "row_block": row_block,
        "col_block": col_block,
        "priority": priority,
    }


@dataclass(frozen=True)
class Task:
    case_id: str
    data_size: int
    feature_dim: int
    label_dim: int
    dtype: str
    quant_scale: float
    quant_zero_point: int
    block_plan: dict[str, int]
    revision: int
    logical_clock_ms: int

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Task":
        required = {
            "case_id",
            "data_size",
            "feature_dim",
            "label_dim",
            "dtype",
            "quant_scale",
            "quant_zero_point",
            "block_plan",
            "revision",
            "logical_clock_ms",
        }
        missing = sorted(required - raw.keys())
        if missing:
            raise ETaskInvalid("task is missing required fields", {"missing": missing, "task": raw})
        case_id = str(raw["case_id"])
        if not case_id:
            raise ETaskInvalid("case_id must be non-empty")
        data_size = int(raw["data_size"])
        feature_dim = int(raw["feature_dim"])
        label_dim = int(raw["label_dim"])
        if data_size <= 0 or feature_dim <= 0 or label_dim <= 0:
            raise ETaskInvalid(
                "data_size, feature_dim, and label_dim must be positive",
                {"case_id": case_id},
            )
        dtype = str(raw["dtype"])
        if dtype not in SUPPORTED_DTYPES:
            raise ETaskInvalid(
                "unsupported quantization dtype",
                {"case_id": case_id, "dtype": dtype, "supported": sorted(SUPPORTED_DTYPES)},
            )
        quant_scale = float(raw["quant_scale"])
        if not quant_scale > 0.0:
            raise ETaskInvalid("quant_scale must be positive", {"case_id": case_id})
        revision = int(raw["revision"])
        logical_clock_ms = int(raw["logical_clock_ms"])
        if revision < 0 or logical_clock_ms < 0:
            raise ETaskInvalid("revision and logical_clock_ms must be non-negative")
        return cls(
            case_id=case_id,
            data_size=data_size,
            feature_dim=feature_dim,
            label_dim=label_dim,
            dtype=dtype,
            quant_scale=quant_scale,
            quant_zero_point=int(raw["quant_zero_point"]),
            block_plan=_normalize_block_plan(raw["block_plan"]),
            revision=revision,
            logical_clock_ms=logical_clock_ms,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "data_size": self.data_size,
            "feature_dim": self.feature_dim,
            "label_dim": self.label_dim,
            "dtype": self.dtype,
            "quant_scale": self.quant_scale,
            "quant_zero_point": self.quant_zero_point,
            "block_plan": dict(self.block_plan),
            "revision": self.revision,
            "logical_clock_ms": self.logical_clock_ms,
        }

    @property
    def content_hash(self) -> str:
        return sha256_obj(self.to_dict())

    def sample_seed(self, global_seed: int) -> int:
        return stable_u64(global_seed, self.case_id, self.revision, self.content_hash)

    @property
    def row_block(self) -> int:
        return min(self.block_plan["row_block"], self.data_size)

    @property
    def col_block(self) -> int:
        return min(self.block_plan["col_block"], self.feature_dim)

    @property
    def priority(self) -> int:
        return self.block_plan["priority"]

    def feature_buffer_bytes(self, mode: str) -> int:
        """Explicit mode tradeoff for the total feature-buffer tier.

        Training materializes float64 dequantized features and retains gradient
        inputs. Inference keeps quantized features resident and dequantizes only
        the current block, improving memory-density for throughput.
        """

        element_count = self.data_size * self.feature_dim
        if mode == "train":
            return element_count * (8 + 8)
        if mode == "infer":
            return element_count + self.row_block * self.feature_dim * 8
        raise ValueError(f"unknown mode: {mode}")

    def single_pass_workspace_bytes(self, mode: str) -> int:
        row = self.row_block
        col = self.col_block
        if mode == "train":
            return (
                row * self.feature_dim * 8
                + row * self.label_dim * 40
                + col * self.label_dim * 24
            )
        if mode == "infer":
            return row * self.feature_dim * 8 + row * self.label_dim * 24
        raise ValueError(f"unknown mode: {mode}")

    def model_workspace_bytes(self, mode: str) -> int:
        weights = self.feature_dim * self.label_dim * 8 + self.label_dim * 8
        if mode == "train":
            return weights * 3
        if mode == "infer":
            return weights
        raise ValueError(f"unknown mode: {mode}")


@dataclass
class TaskIngest:
    tasks: list[Task]
    duplicates: list[dict[str, str]]
    source_version: int
    source_checksum: str

    @property
    def accounting_count(self) -> int:
        return len(self.tasks)


def load_tasks(input_dir: str | Path) -> TaskIngest:
    input_dir = Path(input_dir)
    tasks_path = input_dir / "tasks.jsonl"
    meta_path = input_dir / "tasks.meta.json"
    if not tasks_path.exists():
        raise ETaskInvalid(f"task list not found: {tasks_path}")
    rows = read_jsonl(tasks_path)
    by_case: dict[str, Task] = {}
    duplicates: list[dict[str, str]] = []
    for row in rows:
        task = Task.from_dict(row)
        previous = by_case.get(task.case_id)
        if previous is None:
            by_case[task.case_id] = task
            continue
        if previous.content_hash != task.content_hash:
            raise ETaskInvalid(
                "conflicting duplicate case_id",
                {
                    "case_id": task.case_id,
                    "previous_hash": previous.content_hash,
                    "current_hash": task.content_hash,
                },
            )
        duplicates.append({"case_id": task.case_id, "content_hash": task.content_hash})

    tasks = sorted(
        by_case.values(),
        key=lambda item: (item.logical_clock_ms, -item.priority, item.case_id),
    )
    computed_checksum = sha256_obj([task.to_dict() for task in tasks])
    if meta_path.exists():
        meta = read_json(meta_path)
        source_version = int(meta.get("version", 1))
        declared_checksum = str(meta.get("checksum", computed_checksum))
        if declared_checksum != computed_checksum:
            raise ETaskInvalid(
                "task source checksum mismatch",
                {"declared": declared_checksum, "computed": computed_checksum},
            )
    else:
        source_version = 1
        declared_checksum = computed_checksum
    return TaskIngest(
        tasks=tasks,
        duplicates=duplicates,
        source_version=source_version,
        source_checksum=declared_checksum,
    )
