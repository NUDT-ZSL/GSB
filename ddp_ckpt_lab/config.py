"""Versioned training configuration and compatibility metadata."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .canonical import sha256_json
from .errors import BusinessError


SUPPORTED_SHARDING = ("modulo", "contiguous")

# The fixed-point integer scaler makes all user-facing optimizer parameters
# integers while retaining useful SGD update magnitudes.
OPTIMIZER_SCALE = 1000.0


@dataclass(frozen=True)
class TrainingConfig:
    seed: int
    data_seed: int
    samples: int
    features: int
    hidden_dim: int
    workers: int
    batch_size: int
    train_steps: int
    learning_rate: int
    momentum: int
    sharding: str
    checkpoint_every: int

    def __post_init__(self) -> None:
        integer_fields = (
            "seed",
            "data_seed",
            "samples",
            "features",
            "hidden_dim",
            "workers",
            "batch_size",
            "train_steps",
            "learning_rate",
            "momentum",
            "checkpoint_every",
        )
        for field_name in integer_fields:
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise BusinessError(
                    "E_CONFIG",
                    f"配置字段 {field_name} 必须为整数",
                    field=field_name,
                    value=value,
                )

        if self.workers != 4:
            raise BusinessError(
                "E_CONFIG",
                "本实验固定使用 4 个逻辑工作进程",
                field="workers",
                value=self.workers,
            )
        if self.seed < 0 or self.data_seed < 0:
            raise BusinessError(
                "E_CONFIG",
                "随机种子必须为非负整数",
                fields=["seed", "data_seed"],
            )
        if self.samples <= 0 or self.features <= 0 or self.hidden_dim <= 0:
            raise BusinessError(
                "E_CONFIG",
                "样本数、特征维度和隐藏层维度必须为正整数",
                samples=self.samples,
                features=self.features,
                hidden_dim=self.hidden_dim,
            )
        if self.batch_size <= 0 or self.batch_size % self.workers != 0:
            raise BusinessError(
                "E_CONFIG",
                "batch_size 必须能被 workers 整除",
                batch_size=self.batch_size,
                workers=self.workers,
            )
        if self.samples % self.batch_size != 0:
            raise BusinessError(
                "E_CONFIG",
                "samples 必须能被 batch_size 整除，以保证批次划分稳定",
                samples=self.samples,
                batch_size=self.batch_size,
            )
        if self.train_steps <= 0:
            raise BusinessError(
                "E_CONFIG",
                "train_steps 必须为正整数",
                field="train_steps",
                value=self.train_steps,
            )
        if not 0 < self.learning_rate <= 100_000:
            raise BusinessError(
                "E_CONFIG",
                "learning_rate 必须为 1..100000 的整数（千分位定点）",
                field="learning_rate",
                value=self.learning_rate,
            )
        if not 0 <= self.momentum <= 999:
            raise BusinessError(
                "E_CONFIG",
                "momentum 必须为 0..999 的整数（千分位定点）",
                field="momentum",
                value=self.momentum,
            )
        if self.sharding not in SUPPORTED_SHARDING:
            raise BusinessError(
                "E_CONFIG",
                "不支持的数据分片方式",
                field="sharding",
                value=self.sharding,
                supported=list(SUPPORTED_SHARDING),
            )
        if self.checkpoint_every <= 0:
            raise BusinessError(
                "E_CONFIG",
                "checkpoint_every 必须为正整数",
                field="checkpoint_every",
                value=self.checkpoint_every,
            )

    @property
    def local_batch_size(self) -> int:
        return self.batch_size // self.workers

    @property
    def effective_learning_rate(self) -> float:
        return self.learning_rate / OPTIMIZER_SCALE

    @property
    def effective_momentum(self) -> float:
        return self.momentum / OPTIMIZER_SCALE

    @property
    def config_digest(self) -> str:
        return sha256_json(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def with_changes(self, **changes: Any) -> "TrainingConfig":
        updated = replace(self, **changes)
        return TrainingConfig(**asdict(updated))

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, strict: bool = True) -> "TrainingConfig":
        if not isinstance(value, dict):
            raise BusinessError(
                "E_CONFIG",
                "训练配置必须为对象",
                actual_type=type(value).__name__,
            )
        known = set(cls.__dataclass_fields__)
        unknown = sorted(set(value) - known)
        if strict and unknown:
            raise BusinessError(
                "E_CONFIG",
                "训练配置包含未知字段",
                unknown_fields=unknown,
            )
        missing = sorted(known - set(value))
        if missing:
            raise BusinessError(
                "E_CONFIG",
                "训练配置缺少字段",
                missing_fields=missing,
            )
        return cls(**{name: value[name] for name in known})


def load_config(path: str | Path) -> TrainingConfig:
    config_path = Path(path)
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BusinessError(
            "E_CONFIG",
            "配置文件不存在",
            path=str(config_path),
        ) from exc
    except json.JSONDecodeError as exc:
        raise BusinessError(
            "E_CONFIG",
            "配置文件 JSON 无法解析",
            path=str(config_path),
            error=str(exc),
        ) from exc
    return TrainingConfig.from_dict(value)


def default_config() -> TrainingConfig:
    return TrainingConfig(
        seed=20261117,
        data_seed=1307,
        samples=320,
        features=8,
        hidden_dim=12,
        workers=4,
        batch_size=16,
        train_steps=18,
        learning_rate=40,
        momentum=900,
        sharding="modulo",
        checkpoint_every=6,
    )
