from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .errors import BusinessError, ErrorCode


@dataclass(frozen=True)
class VideoConfig:
    prompt: str = "赤色山脊上的缓慢镜头，薄雾与晨光"
    width: int = 192
    height: int = 108
    frames: int = 32
    steps: int = 8
    seed: int = 20261120
    latent_channels: int = 4
    temporal_mix: float = 0.24
    condition_strength: float = 0.18
    noise_strength: float = 0.36
    anchor_strength: float = 0.32
    anchor_path: str | None = None
    block_size: int = 8
    latency_budget_ms: float = 120.0
    max_frames: int = 240
    max_chunk_bytes: int = 64 * 1024 * 1024
    max_concurrent_decode_tasks: int = 4

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "VideoConfig":
        data = dict(payload or {})
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                "配置包含未知字段",
                {"fields": unknown},
            )
        try:
            config = cls(**data)
        except TypeError as exc:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                "配置字段类型或名称无效",
                {"cause": str(exc)},
            ) from exc
        config.validate()
        return config

    def validate(self) -> None:
        if not isinstance(self.prompt, str) or len(self.prompt) > 4096:
            self._invalid("prompt", "必须为不超过 4096 字符的字符串")
        if not isinstance(self.width, int) or isinstance(self.width, bool) or not 16 <= self.width <= 1024:
            self._invalid("width", "必须为 16..1024 的整数")
        if not isinstance(self.height, int) or isinstance(self.height, bool) or not 16 <= self.height <= 1024:
            self._invalid("height", "必须为 16..1024 的整数")
        if not isinstance(self.frames, int) or isinstance(self.frames, bool) or self.frames < 1:
            self._invalid("frames", "必须为正整数")
        if not isinstance(self.steps, int) or isinstance(self.steps, bool) or not 1 <= self.steps <= 64:
            self._invalid("steps", "必须为 1..64 的整数")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool) or not 0 <= self.seed <= 2**63 - 1:
            self._invalid("seed", "必须为 0..2^63-1 的整数")
        if not isinstance(self.latent_channels, int) or isinstance(self.latent_channels, bool) or not 1 <= self.latent_channels <= 16:
            self._invalid("latent_channels", "必须为 1..16 的整数")
        for name in ("temporal_mix", "condition_strength", "noise_strength"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
                self._invalid(name, "必须为 [0, 1] 内的有限数值")
        if not isinstance(self.anchor_strength, (int, float)) or isinstance(self.anchor_strength, bool) or not math.isfinite(float(self.anchor_strength)) or not 0.0 <= float(self.anchor_strength) <= 1.0:
            self._invalid("anchor_strength", "必须为 [0, 1] 内的有限数值")
        if self.anchor_path is not None and (not isinstance(self.anchor_path, str) or not self.anchor_path.strip()):
            self._invalid("anchor_path", "必须为非空路径或 null")
        if not isinstance(self.block_size, int) or isinstance(self.block_size, bool) or self.block_size < 1:
            self._invalid("block_size", "必须为正整数")
        if not isinstance(self.latency_budget_ms, (int, float)) or isinstance(self.latency_budget_ms, bool) or not math.isfinite(float(self.latency_budget_ms)) or self.latency_budget_ms <= 0:
            self._invalid("latency_budget_ms", "必须为大于 0 的有限数值")
        if not isinstance(self.max_frames, int) or isinstance(self.max_frames, bool) or self.max_frames < 1:
            self._invalid("max_frames", "必须为正整数")
        if not isinstance(self.max_chunk_bytes, int) or isinstance(self.max_chunk_bytes, bool) or self.max_chunk_bytes < 1:
            self._invalid("max_chunk_bytes", "必须为正整数")
        if not isinstance(self.max_concurrent_decode_tasks, int) or isinstance(self.max_concurrent_decode_tasks, bool) or not 1 <= self.max_concurrent_decode_tasks <= 32:
            self._invalid("max_concurrent_decode_tasks", "必须为 1..32 的整数")

        if self.frames > self.max_frames:
            raise BusinessError(
                ErrorCode.E_RESOURCE_LIMIT,
                "单次生成帧数超过资源上限",
                {
                    "resource": "frames",
                    "limit": self.max_frames,
                    "requested": self.frames,
                    "strategy": "reject",
                },
            )
        if self.max_chunk_bytes < self.width * self.height * 3:
            raise BusinessError(
                ErrorCode.E_RESOURCE_LIMIT,
                "单块内存上限不足以容纳一帧 RGB",
                {
                    "resource": "chunk_memory_bytes",
                    "limit": self.max_chunk_bytes,
                    "minimum_frame_bytes": self.width * self.height * 3,
                    "strategy": "reject",
                },
            )

    def _invalid(self, field: str, reason: str) -> None:
        raise BusinessError(
            ErrorCode.E_CONFIG_INVALID,
            f"配置字段 {field} 无效：{reason}",
            {"field": field, "reason": reason},
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def latent_shape(self) -> tuple[int, int, int, int]:
        return (
            self.frames,
            max(1, self.height // 8),
            max(1, self.width // 8),
            self.latent_channels,
        )

    @property
    def mode(self) -> str:
        return "image_to_video" if self.anchor_path else "text_to_video"

    def resolved_anchor_path(self, project_root: Path | None = None) -> Path | None:
        if self.anchor_path is None:
            return None
        root = (project_root or Path.cwd()).resolve()
        candidate = Path(self.anchor_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        if root not in resolved.parents and resolved != root:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                "锚点图片必须位于项目目录内",
                {"field": "anchor_path", "path": str(resolved)},
            )
        if not resolved.is_file():
            raise BusinessError(
                ErrorCode.E_ANCHOR_MISMATCH,
                "本地锚点图片不存在",
                {"field": "anchor_path", "path": str(resolved)},
            )
        return resolved
