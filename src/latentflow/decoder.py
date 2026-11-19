from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .canonical import array_sha256
from .config import VideoConfig
from .errors import BusinessError, ErrorCode


@dataclass(frozen=True)
class DecodeTiming:
    total_ms: float
    stage_ms: dict[str, float]
    frames: int
    decode_stages: int = 4

    @property
    def ms_per_frame(self) -> float:
        return self.total_ms / self.frames if self.frames else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_ms": round(self.total_ms, 6),
            "stage_ms": {key: round(value, 6) for key, value in self.stage_ms.items()},
            "frames": self.frames,
            "decode_stages": self.decode_stages,
            "ms_per_frame": round(self.ms_per_frame, 6),
        }


@dataclass
class DecodedBlock:
    index: int
    start_frame: int
    end_frame: int
    frames: np.ndarray
    sha256: str
    timing: DecodeTiming

    @property
    def frame_count(self) -> int:
        return int(self.frames.shape[0])

    def metadata(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start_frame": self.start_frame,
            "end_frame_exclusive": self.end_frame,
            "frames": self.frame_count,
            "sha256": self.sha256,
            "timing": self.timing.to_dict(),
        }


@dataclass
class WholeDecode:
    frames: np.ndarray
    sha256: str
    timing: DecodeTiming
    context_setup_ms: float = 0.0
    context: dict[str, Any] = field(default_factory=dict)

    def metadata(self) -> dict[str, Any]:
        return {
            "frames": int(self.frames.shape[0]),
            "shape": list(self.frames.shape),
            "dtype": str(self.frames.dtype),
            "sha256": self.sha256,
            "timing": self.timing.to_dict(),
            "context_setup_ms": round(self.context_setup_ms, 6),
            "context": self.context,
        }


class DeterministicDecoder:
    """
    Decode with sequence-global normalization and prefix-only latent mixing.

    Chunking is applied only after the global context and per-frame formula have
    been fixed. No chunk-local statistic or decoded-pixel feedback is allowed.
    """

    STAGE_NAMES = ("normalize", "temporal_mix", "project_rgb", "quantize_anchor")

    def __init__(
        self,
        config: VideoConfig,
        latents: np.ndarray,
        anchor: np.ndarray | None = None,
    ) -> None:
        self.config = config
        self.latents = np.ascontiguousarray(latents, dtype=np.float32)
        self.anchor = (
            None if anchor is None else np.ascontiguousarray(anchor, dtype=np.uint8)
        )
        self._context_lock = threading.Lock()
        self._context_ready = False
        self._context_setup_ms = 0.0
        self._global_mean: np.ndarray | None = None
        self._global_std: np.ndarray | None = None
        self._normalized: np.ndarray | None = None
        self._ref_mean: np.ndarray | None = None
        self._ref_std: np.ndarray | None = None
        latent_height = self.latents.shape[1]
        latent_width = self.latents.shape[2]
        self._y_index = (
            np.arange(config.height, dtype=np.int64) * latent_height // config.height
        )
        self._x_index = (
            np.arange(config.width, dtype=np.int64) * latent_width // config.width
        )

    @property
    def context_setup_ms(self) -> float:
        return self._context_setup_ms

    def ensure_context(self) -> dict[str, Any]:
        if self._context_ready:
            return self.context_metadata()
        with self._context_lock:
            if self._context_ready:
                return self.context_metadata()
            started = time.perf_counter()
            global_mean = self.latents.mean(axis=(0, 1, 2), dtype=np.float64)
            global_std = self.latents.std(axis=(0, 1, 2), dtype=np.float64)
            global_std = np.maximum(global_std, 1e-6)
            normalized = (
                (self.latents.astype(np.float64) - global_mean[None, None, None, :])
                / global_std[None, None, None, :]
            ).astype(np.float32)
            if self.anchor is not None:
                anchor_float = self.anchor.astype(np.float64)
                ref_mean = anchor_float.mean(axis=(0, 1))
                ref_std = np.maximum(anchor_float.std(axis=(0, 1)), 1e-6)
            else:
                ref_mean = np.zeros(3, dtype=np.float64)
                ref_std = np.ones(3, dtype=np.float64)
            self._global_mean = global_mean
            self._global_std = global_std
            self._normalized = normalized
            self._ref_mean = ref_mean
            self._ref_std = ref_std
            self._context_setup_ms = (time.perf_counter() - started) * 1000.0
            self._context_ready = True
        return self.context_metadata()

    def context_metadata(self) -> dict[str, Any]:
        if not self._context_ready or self._global_mean is None or self._global_std is None:
            return {"ready": False}
        return {
            "ready": True,
            "global_mean": [round(float(value), 9) for value in self._global_mean],
            "global_std": [round(float(value), 9) for value in self._global_std],
            "normalization_scope": "all_latent_frames",
            "temporal_source": "previous_latent_frame",
            "anchor_reference_mean": (
                None if self._ref_mean is None else [round(float(value), 6) for value in self._ref_mean]
            ),
            "anchor_reference_std": (
                None if self._ref_std is None else [round(float(value), 6) for value in self._ref_std]
            ),
            "context_setup_ms": round(self._context_setup_ms, 6),
        }

    def decode_range(self, start: int, end: int, block_index: int = 0) -> DecodedBlock:
        if not 0 <= start < end <= self.config.frames:
            raise BusinessError(
                ErrorCode.E_INVALID_STATE,
                "解码帧区间越界",
                {"start": start, "end": end, "frames": self.config.frames},
            )
        self.ensure_context()
        if self._normalized is None:
            raise RuntimeError("decoder context unavailable")

        stage_ns = {name: 0 for name in self.STAGE_NAMES}
        frames = np.empty(
            (end - start, self.config.height, self.config.width, 3), dtype=np.uint8
        )
        total_started = time.perf_counter_ns()
        for output_index, frame_index in enumerate(range(start, end)):
            normalize_started = time.perf_counter_ns()
            current = self._normalized[frame_index]
            previous = self._normalized[frame_index - 1] if frame_index > 0 else current
            stage_ns["normalize"] += time.perf_counter_ns() - normalize_started

            mix_started = time.perf_counter_ns()
            mix = np.float32(self.config.temporal_mix)
            mixed = current * np.float32(1.0 - float(mix)) + previous * mix
            mixed = mixed[np.ix_(self._y_index, self._x_index)]
            stage_ns["temporal_mix"] += time.perf_counter_ns() - mix_started

            project_started = time.perf_counter_ns()
            red = (
                np.float32(0.82) * mixed[..., 0]
                + np.float32(0.41) * mixed[..., 1]
                - np.float32(0.29) * mixed[..., 2]
                + np.float32(0.14) * mixed[..., 3]
            )
            green = (
                -np.float32(0.21) * mixed[..., 0]
                + np.float32(0.71) * mixed[..., 1]
                + np.float32(0.48) * mixed[..., 2]
                - np.float32(0.13) * mixed[..., 3]
            )
            blue = (
                np.float32(0.17) * mixed[..., 0]
                - np.float32(0.26) * mixed[..., 1]
                + np.float32(0.86) * mixed[..., 2]
                + np.float32(0.31) * mixed[..., 3]
            )
            rgb = np.stack(
                [
                    np.float32(128.0) + np.float32(104.0) * red,
                    np.float32(128.0) + np.float32(104.0) * green,
                    np.float32(128.0) + np.float32(100.0) * blue,
                ],
                axis=-1,
            )
            rgb = np.clip(rgb, np.float32(0.0), np.float32(255.0))
            stage_ns["project_rgb"] += time.perf_counter_ns() - project_started

            quantize_started = time.perf_counter_ns()
            if self.anchor is not None and frame_index > 0:
                rgb = self._apply_anchor_statistics(rgb, frame_index)
            quantized = np.floor(rgb + np.float32(0.5)).astype(np.uint8)
            if self.anchor is not None and frame_index == 0:
                quantized = self.anchor.copy()
            frames[output_index] = quantized
            stage_ns["quantize_anchor"] += time.perf_counter_ns() - quantize_started

        total_ns = time.perf_counter_ns() - total_started
        timing = DecodeTiming(
            total_ms=total_ns / 1_000_000.0,
            stage_ms={name: value / 1_000_000.0 for name, value in stage_ns.items()},
            frames=end - start,
        )
        return DecodedBlock(
            index=block_index,
            start_frame=start,
            end_frame=end,
            frames=frames,
            sha256=array_sha256(frames),
            timing=timing,
        )

    def _apply_anchor_statistics(self, rgb: np.ndarray, frame_index: int) -> np.ndarray:
        if self._ref_mean is None or self._ref_std is None:
            return rgb
        strength = np.float64(self.config.anchor_strength)
        adjusted = rgb.astype(np.float64, copy=True)
        for channel in range(3):
            plane = adjusted[..., channel]
            frame_mean = float(plane.mean())
            frame_std = max(float(plane.std()), 1e-6)
            matched = (
                (plane - frame_mean) / frame_std * float(self._ref_std[channel])
                + float(self._ref_mean[channel])
            )
            adjusted[..., channel] = (1.0 - strength) * plane + strength * matched
        # Temporal phase is intentional and depends only on the frame index.
        phase = np.float64((frame_index % 11) - 5) * 0.18
        adjusted[..., 0] = np.clip(adjusted[..., 0] + phase, 0.0, 255.0)
        return adjusted.astype(np.float32)

    def decode_all(self) -> WholeDecode:
        context = self.ensure_context()
        started = time.perf_counter_ns()
        frames = np.empty(
            (self.config.frames, self.config.height, self.config.width, 3),
            dtype=np.uint8,
        )
        stage_ns = {name: 0 for name in self.STAGE_NAMES}
        for frame_index in range(self.config.frames):
            block = self.decode_range(frame_index, frame_index + 1, block_index=frame_index)
            frames[frame_index] = block.frames[0]
            for name in self.STAGE_NAMES:
                stage_ns[name] += block.timing.stage_ms[name] * 1_000_000.0
        total_ns = time.perf_counter_ns() - started
        timing = DecodeTiming(
            total_ms=total_ns / 1_000_000.0,
            stage_ms={name: value / 1_000_000.0 for name, value in stage_ns.items()},
            frames=self.config.frames,
        )
        return WholeDecode(
            frames=frames,
            sha256=array_sha256(frames),
            timing=timing,
            context_setup_ms=self._context_setup_ms,
            context=context,
        )

    def decode_chunks(self, block_size: int) -> list[DecodedBlock]:
        if not 1 <= block_size <= self.config.frames:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                "块大小必须在 1..frames 范围内",
                {"block_size": block_size, "frames": self.config.frames},
            )
        blocks: list[DecodedBlock] = []
        for index, start in enumerate(range(0, self.config.frames, block_size)):
            end = min(self.config.frames, start + block_size)
            blocks.append(self.decode_range(start, end, block_index=index))
        return blocks


def assemble_blocks(blocks: list[DecodedBlock], frames: int) -> np.ndarray:
    if not blocks:
        raise BusinessError(ErrorCode.E_INVALID_STATE, "没有可拼接的解码块")
    ordered = sorted(blocks, key=lambda block: block.start_frame)
    expected_start = 0
    assembled: list[np.ndarray] = []
    for block in ordered:
        if block.start_frame != expected_start:
            raise BusinessError(
                ErrorCode.E_CHUNK_MISMATCH,
                "就绪块之间存在缺口或重叠",
                {
                    "expected_start_frame": expected_start,
                    "actual_start_frame": block.start_frame,
                    "block_index": block.index,
                },
            )
        assembled.append(block.frames)
        expected_start = block.end_frame
    if expected_start != frames:
        raise BusinessError(
            ErrorCode.E_CHUNK_MISMATCH,
            "就绪块未覆盖全部帧",
            {"expected_frames": frames, "covered_frames": expected_start},
        )
    return np.concatenate(assembled, axis=0)


def compare_frame_sequences(
    assembled: np.ndarray,
    reference: np.ndarray,
    block_size: int,
    comparison_label: str = "chunk_vs_whole",
) -> dict[str, Any]:
    if assembled.shape != reference.shape:
        raise BusinessError(
            ErrorCode.E_CHUNK_MISMATCH,
            "分块拼接序列与整段解码形状不一致",
            {
                "comparison": comparison_label,
                "assembled_shape": list(assembled.shape),
                "reference_shape": list(reference.shape),
                "block_index": 0,
                "frame_index": 0,
                "pixel_position": None,
            },
        )
    difference = assembled != reference
    if bool(np.any(difference)):
        frame_index, y, x, channel = (int(value) for value in np.argwhere(difference)[0])
        raise BusinessError(
            ErrorCode.E_CHUNK_MISMATCH,
            "分块拼接结果与整段解码不一致",
            {
                "comparison": comparison_label,
                "block_size": block_size,
                "block_index": frame_index // block_size,
                "frame_index": frame_index,
                "pixel_position": {"x": x, "y": y, "channel": channel},
                "expected": int(reference[frame_index, y, x, channel]),
                "actual": int(assembled[frame_index, y, x, channel]),
                "assembled_sha256": array_sha256(assembled),
                "reference_sha256": array_sha256(reference),
            },
        )
    return {
        "comparison": comparison_label,
        "equal": True,
        "frames": int(reference.shape[0]),
        "pixels_per_frame": int(reference.shape[1] * reference.shape[2]),
        "bytes_compared": int(reference.nbytes),
        "sha256": array_sha256(reference),
        "block_size": block_size,
    }


def verify_anchor_frame(decoded: np.ndarray, anchor: np.ndarray) -> dict[str, Any]:
    first = decoded[0]
    if np.array_equal(first, anchor):
        return {
            "equal": True,
            "sha256": array_sha256(anchor),
            "frame_index": 0,
            "bytes_compared": int(anchor.nbytes),
        }
    difference = first != anchor
    y, x, channel = (int(value) for value in np.argwhere(difference)[0])
    raise BusinessError(
        ErrorCode.E_ANCHOR_MISMATCH,
        "输出首帧与本地输入图不逐像素一致",
        {
            "frame_index": 0,
            "pixel_position": {"x": x, "y": y, "channel": channel},
            "expected": int(anchor[y, x, channel]),
            "actual": int(first[y, x, channel]),
            "anchor_sha256": array_sha256(anchor),
            "output_sha256": array_sha256(first),
        },
    )
