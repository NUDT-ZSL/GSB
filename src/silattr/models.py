"""跨阶段共享的数据结构。"""

from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np

from .config import CATEGORY_NAMES


@dataclass(frozen=True)
class FrameObs:
    frame_index: int
    sample_start: int
    sample_end: int
    noise_floor_true: float  # dBFS
    is_speech: bool
    speaker_id: Optional[int]

    def to_dict(self):
        return asdict(self)


@dataclass
class CallManifest:
    call_id: str
    seed: int
    duration_ms: int
    frame_ms: int
    noise_profile_ref: str

    def to_dict(self):
        return asdict(self)


@dataclass
class SilenceSegment:
    """以帧级观测为真值口径的最大连续静音段（端点判定不在本组件职责内）。"""

    silence_id: int
    start_frame: int
    end_frame: int  # 不含

    @property
    def frames(self):
        return range(self.start_frame, self.end_frame)


@dataclass
class WindowCoverage:
    window_id: int
    start_frame: int
    end_frame: int  # 不含
    anchor_frame: int


@dataclass
class NoiseSegment:
    start_frame: int
    end_frame: int  # 不含
    level_db: float       # 段内估计值（均值）
    dispersion_db: float  # 段内离散度（总体标准差）
    is_speech: bool


@dataclass
class AttributionResult:
    silence_id: int
    start_frame: int
    end_frame: int
    category: int
    category_name: str
    deviation_db: float
    flux_db: float
    noise_version: str
    threshold_version: str


@dataclass
class Event:
    event_id: int
    start_frame: int
    end_frame: int
    category: int
    category_name: str
    silence_ids: list
    step_count: int


def category_name(category: int) -> str:
    return CATEGORY_NAMES.get(category, f"unknown:{category}")


def frame_rms_db(pcm: np.ndarray, start_sample: int, end_sample: int) -> float:
    """单帧 RMS 能量（dBFS，满幅 int16 为 0 dB）。"""
    frame = pcm[start_sample:end_sample].astype(np.float64)
    rms = float(np.sqrt(np.mean(frame * frame)))
    if rms <= 0.0:
        return -120.0
    return 20.0 * float(np.log10(rms / 32768.0))

