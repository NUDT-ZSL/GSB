"""配置对象：估计窗口、突变检测、归因阈值与资源上限均可显式声明。"""

from dataclasses import dataclass, asdict, replace

# 受控噪声源类别（编号即平局打破顺序：编号小者优先）
CATEGORY_FLAT = 0       # 平稳底噪
CATEGORY_DEVICE = 1     # 设备突变
CATEGORY_CROSSTALK = 2  # 串入人声残留

CONTROLLED_CATEGORIES = (CATEGORY_FLAT, CATEGORY_DEVICE, CATEGORY_CROSSTALK)
CATEGORY_NAMES = {
    CATEGORY_FLAT: "flat_floor",
    CATEGORY_DEVICE: "device_step",
    CATEGORY_CROSSTALK: "crosstalk_residue",
}


@dataclass(frozen=True)
class NoiseConfig:
    """噪声底低分位滑窗估计配置。"""

    window_frames: int = 25          # 估计窗口（帧），20ms 帧下 500ms
    hop_frames: int = 5              # 滑窗步长（帧）
    quantile: float = 0.1            # 低分位（0,1）

    def key(self):
        return ("noise", self.window_frames, self.hop_frames, self.quantile)


@dataclass(frozen=True)
class SegmentConfig:
    """噪声底突变检测与分段配置。"""

    step_threshold_db: float = 6.0   # 相邻帧突变阈值（dB）
    min_duration_frames: int = 5     # 台阶最小持续帧数

    def key(self):
        return ("segment", self.step_threshold_db, self.min_duration_frames)


@dataclass(frozen=True)
class AttributionConfig:
    """静音段归因规则与阈值（全部显式声明）。"""

    # 设备突变：偏离量达到 dev_device_db 且波动幅度低于 flux_device_db
    dev_device_db: float = 6.0
    flux_device_db: float = 3.0
    # 串入人声残留：波动幅度高于 flux_crosstalk_db 且偏离量达到 dev_crosstalk_db
    flux_crosstalk_db: float = 3.0
    dev_crosstalk_db: float = 2.0
    version_tag: str = "attr-v1"

    def key(self):
        return (
            "attr",
            self.dev_device_db,
            self.flux_device_db,
            self.flux_crosstalk_db,
            self.dev_crosstalk_db,
            self.version_tag,
        )


@dataclass(frozen=True)
class ResourceLimits:
    """可配置资源上限；on_exceed='error' 抛错，'degrade' 降级并标记。"""

    max_frames: int = 200_000
    max_noise_intervals: int = 50_000
    max_lineage_entries: int = 200_000
    on_exceed: str = "error"  # error | degrade


@dataclass(frozen=True)
class PipelineConfig:
    sample_rate: int = 16000
    frame_ms: int = 20
    merge_gap_frames: int = 2          # 事件合并的最大相邻静音间隔（帧）
    noise: NoiseConfig = NoiseConfig()
    segment: SegmentConfig = SegmentConfig()
    attribution: AttributionConfig = AttributionConfig()
    limits: ResourceLimits = ResourceLimits()

    def with_changes(self, **changes):
        kwargs = asdict(self)
        for name, value in changes.items():
            if name in ("noise", "segment", "attribution", "limits"):
                nested = kwargs[name]
                nested.update(value)
            else:
                kwargs[name] = value
        return PipelineConfig(
            sample_rate=kwargs["sample_rate"],
            frame_ms=kwargs["frame_ms"],
            merge_gap_frames=kwargs["merge_gap_frames"],
            noise=NoiseConfig(**kwargs["noise"]),
            segment=SegmentConfig(**kwargs["segment"]),
            attribution=AttributionConfig(**kwargs["attribution"]),
            limits=ResourceLimits(**kwargs["limits"]),
        )

