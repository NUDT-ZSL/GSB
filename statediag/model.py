"""核心数据模型与确定性算子常量。"""

from dataclasses import dataclass
from enum import Enum


class Stage(str, Enum):
    """DAG 阶段状态机的五个阶段边界。"""

    INITIAL_VALIDATION = "initial_validation"
    BLOCK_TRANSFORM = "block_transform"
    CROSS_REDUCE = "cross_reduce"
    SNAPSHOT_COMMIT = "snapshot_commit"
    RECOVERY_ARBITRATION = "recovery_arbitration"


STAGE_ORDER = tuple(Stage)


# 算子的确定性系数。整条链路（含三种精度档）共用同一算子语义，
# 仅由元素/块/步索引决定，不引入任何随机源。
ALPHA = 0.5  # 自反馈系数
BETA = 0.25  # 左邻耦合系数
GAMMA = 0.125  # 右邻耦合系数
DELTA = 0.0625  # 步驱动偏置


@dataclass(frozen=True)
class Task:
    """状态机任务清单中的一行。"""

    task_id: str
    state_seed: int
    num_steps: int
    block_size: int
    quant_scale: int
    revision: int
    logical_clock_ms: int

    @classmethod
    def from_dict(cls, row):
        return cls(
            task_id=str(row["task_id"]),
            state_seed=int(row["state_seed"]),
            num_steps=int(row["num_steps"]),
            block_size=int(row["block_size"]),
            quant_scale=int(row["quant_scale"]),
            revision=int(row["revision"]),
            logical_clock_ms=int(row["logical_clock_ms"]),
        )

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "state_seed": self.state_seed,
            "num_steps": self.num_steps,
            "block_size": self.block_size,
            "quant_scale": self.quant_scale,
            "revision": self.revision,
            "logical_clock_ms": self.logical_clock_ms,
        }


@dataclass(frozen=True)
class TransformConfig:
    """一次执行所依赖的代数配置（内容寻址的输入之一）。"""

    algebra: int
    precision: str
    rounding_mode: str
    tolerance_abs: float
    tolerance_rel: float
    effective_ms: int

    def to_dict(self):
        return {
            "algebra": self.algebra,
            "precision": self.precision,
            "rounding_mode": self.rounding_mode,
            "tolerance_abs": float(self.tolerance_abs),
            "tolerance_rel": float(self.tolerance_rel),
            "effective_ms": int(self.effective_ms),
        }


@dataclass
class BlockTrace:
    """单个块的变换轨迹，可追溯到块与归约索引。"""

    block_index: int
    global_start: int
    global_end: int
    reduction_index: int
    steps: int
    block_sum: float
    checksum: str

    def to_dict(self):
        return {
            "block_index": self.block_index,
            "global_start": self.global_start,
            "global_end": self.global_end,
            "reduction_index": self.reduction_index,
            "steps": self.steps,
            "block_sum": float(self.block_sum),
            "checksum": self.checksum,
        }
