"""数据模型：动画清单解析后的内部表示。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class FrameSpec:
    """清单中的一帧。

    index: 清单内声明的帧序号（用于排序与重复校验，不要求从 0 连续）。
    image: 清单中声明的原始图片路径（相对清单目录或绝对路径）。
    duration_ms: 该帧停留毫秒数。
    """

    index: int
    image: str
    duration_ms: int


@dataclass(frozen=True)
class AnimationSpec:
    """通过结构校验、可进入合成阶段的一个动画。"""

    animation_id: str
    target_duration_ms: int
    frames: tuple[FrameSpec, ...]
    manifest_dir: Path

    def resolve_frame_path(self, frame: FrameSpec) -> Path:
        """按清单所在目录解析帧图片路径。

        绝对路径原样使用；相对路径（含子目录）相对清单目录拼接。
        """
        raw = Path(frame.image)
        if raw.is_absolute():
            return raw
        return (self.manifest_dir / raw).resolve()


@dataclass
class ParsedAnimation:
    """清单中一个动画条目的解析结果（通过或被拒均会出现）。"""

    animation_id: str
    target_duration_ms: int | None
    spec: AnimationSpec | None
    issues: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        """是否通过结构校验、允许进入合成。"""
        return self.spec is not None
