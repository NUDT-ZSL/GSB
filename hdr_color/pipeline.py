"""Shared conversion resources and deterministic per-chunk frame processing."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Sequence

from .color import ColorPipeline
from .curves import (
    PQ_CONSTANTS,
    TransferFunctions,
    canonical_transfer,
    roundtrip_diagnostics,
)
from .errors import BusinessError
from .stats import summarize_luminance
from .task import Injection, TaskConfig


INJECTION_ERRORS = {
    "NAN": ("E_INJECT_NAN", "injected NaN"),
    "NEGATIVE": ("E_INJECT_NEGATIVE", "injected negative luminance"),
    "PEAK_OVERFLOW": (
        "E_INJECT_PEAK_OVERFLOW",
        "injected source peak overflow",
    ),
    "BAD_TRANSFER": (
        "E_INJECT_BAD_TRANSFER",
        "injected invalid transfer-function declaration",
    ),
}


@dataclass(frozen=True)
class SharedResources:
    config: TaskConfig
    source_transfer: TransferFunctions
    target_transfer: TransferFunctions
    source_validation: dict[str, Any]
    target_validation: dict[str, Any]
    color: ColorPipeline
    injections_by_frame: dict[int, tuple[Injection, ...]]

    @classmethod
    def create(cls, config: TaskConfig) -> "SharedResources":
        source_transfer = TransferFunctions.create(
            config.source_transfer,
            config.reference_white_nits,
            config.source_peak_nits,
            config.curve_lut_size,
        )
        target_transfer = TransferFunctions.create(
            config.target_transfer,
            config.reference_white_nits,
            config.target_peak_nits,
            config.curve_lut_size,
        )
        source_validation = roundtrip_diagnostics(source_transfer)
        target_validation = roundtrip_diagnostics(target_transfer)
        for label, validation in (
            ("source", source_validation),
            ("target", target_validation),
        ):
            if not validation["passes"]:
                raise BusinessError(
                    "E_TRANSFER_ROUNDTRIP",
                    "transfer function round trip exceeds numerical tolerance",
                    {
                        "role": label,
                        "transfer": (
                            source_transfer.kind
                            if label == "source"
                            else target_transfer.kind
                        ),
                        "validation": validation,
                    },
                )
        color = ColorPipeline.create(
            config.source_gamut,
            config.source_white,
            config.target_gamut,
            config.target_white,
        )
        grouped: dict[int, list[Injection]] = {}
        for injection in config.injections:
            grouped.setdefault(injection.frame_index, []).append(injection)
        return cls(
            config=config,
            source_transfer=source_transfer,
            target_transfer=target_transfer,
            source_validation=source_validation,
            target_validation=target_validation,
            color=color,
            injections_by_frame={
                key: tuple(value) for key, value in grouped.items()
            },
        )

    @property
    def shared_bytes(self) -> int:
        unique_luts: dict[int, int] = {}
        for functions in (self.source_transfer, self.target_transfer):
            unique_luts[id(functions.encode_lut)] = functions.shared_lut_bytes // 2
            unique_luts[id(functions.decode_lut)] = functions.shared_lut_bytes // 2
        # Matrices, constants and small control structures.
        return sum(unique_luts.values()) + 65536

    def report(self) -> dict[str, Any]:
        return {
            "source_transfer": self.source_transfer.report(),
            "target_transfer": self.target_transfer.report(),
            "source_transfer_validation": self.source_validation,
            "target_transfer_validation": self.target_validation,
            "colour": self.color.report(),
            "tone_mapping": self.config.tone_mapper.report(),
            "shared_read_only": {
                "curves_and_luts": True,
                "matrices": True,
                "shared_bytes_estimate": self.shared_bytes,
            },
        }


def process_chunk(
    shared: SharedResources,
    chunk_index: int,
    start_frame: int,
    end_frame: int,
    test_delay_seconds: float = 0.0,
) -> dict[str, Any]:
    if test_delay_seconds > 0.0:
        import time

        time.sleep(test_delay_seconds)
    frame_entries: list[dict[str, Any]] = []
    frame_stats: list[dict[str, Any]] = []
    injection_records: list[dict[str, Any]] = []
    totals = {
        "valid_pixel_count": 0,
        "pre_tone_luminance_sum": 0.0,
        "post_tone_luminance_sum": 0.0,
        "black_pixel_count": 0,
        "out_of_gamut_pixel_count": 0,
        "gamut_desaturated_pixel_count": 0,
        "channel_clipped_pixel_count": 0,
        "encoded_channel_clip_count": 0,
        "source_roundtrip_skipped_pixel_count": 0,
    }
    try:
        for frame_index in range(start_frame, end_frame):
            raw_frame = shared.config.frames[frame_index]
            working_frame, injected_errors, records = _apply_injections(
                raw_frame,
                frame_index,
                shared.injections_by_frame.get(frame_index, ()),
                shared.config,
            )
            injection_records.extend(records)
            if injected_errors:
                frame_entries.append(
                    {
                        "frame_index": frame_index,
                        "status": "error",
                        "errors": injected_errors,
                        "linear_nits": None,
                        "encoded_rgb": None,
                    }
                )
                frame_stats.append(
                    {
                        "frame_index": frame_index,
                        "valid": False,
                        "status": "error",
                        "error_codes": [item["code"] for item in injected_errors],
                        "pixel_count": shared.config.pixels_per_frame,
                        "luminance_mean_nits": None,
                        "luminance_peak_nits": None,
                        "over_target_peak_pixel_ratio": None,
                        "out_of_gamut_pixel_ratio": None,
                        "gamut_desaturated_pixel_ratio": None,
                        "channel_clipped_pixel_ratio": None,
                        "encoded_signal_clipped_channel_ratio": None,
                        "source_roundtrip_skipped_pixel_ratio": None,
                        "pre_tone_luminance_mean_nits": None,
                        "post_tone_luminance_mean_nits": None,
                        "luminance_retention_ratio": None,
                        "mapping_luminance_conservation": None,
                        "black_pixel_count": None,
                    }
                )
                continue

            entry, stats, frame_totals = _process_frame(
                shared, frame_index, working_frame
            )
            frame_entries.append(entry)
            frame_stats.append(stats)
            for key in totals:
                totals[key] += frame_totals[key]
    except BusinessError as exc:
        exc.details.setdefault("chunk_index", chunk_index)
        exc.details.setdefault("start_frame", start_frame)
        exc.details.setdefault("end_frame_exclusive", end_frame)
        raise
    except Exception as exc:  # defensive: includes unexpected math/domain errors
        raise BusinessError(
            "E_CHUNK_FAILURE",
            "unexpected failure while processing a frame chunk",
            {
                "chunk_index": chunk_index,
                "start_frame": start_frame,
                "end_frame_exclusive": end_frame,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        ) from exc

    return {
        "status": "complete",
        "chunk_index": chunk_index,
        "start_frame": start_frame,
        "end_frame_exclusive": end_frame,
        "frame_entries": frame_entries,
        "frame_stats": frame_stats,
        "injection_records": injection_records,
        "mapping_totals": totals,
    }


def _apply_injections(
    raw_frame: Sequence[Sequence[float]],
    frame_index: int,
    injections: Sequence[Injection],
    config: TaskConfig,
) -> tuple[list[list[float]], list[dict[str, Any]], list[dict[str, Any]]]:
    frame = [[float(channel) for channel in pixel] for pixel in raw_frame]
    errors: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for injection_index, injection in enumerate(injections):
        code, message = INJECTION_ERRORS[injection.kind]
        applied = True
        detail: dict[str, Any] = {
            "frame_index": frame_index,
            "kind": injection.kind,
            "channel": injection.channel,
            "error_code": code,
            "injection_ordinal": injection_index,
            "source_file_modified": False,
        }
        if injection.kind == "NAN":
            assert injection.channel is not None
            frame[0][injection.channel] = math.nan
            detail["pixel_index"] = 0
        elif injection.kind == "NEGATIVE":
            assert injection.channel is not None
            magnitude = injection.value if injection.value is not None else 1.0
            frame[0][injection.channel] = -max(
                abs(frame[0][injection.channel]), magnitude
            )
            detail["pixel_index"] = 0
            detail["value"] = frame[0][injection.channel]
        elif injection.kind == "PEAK_OVERFLOW":
            factor = injection.value if injection.value is not None else 4.0
            frame = [
                [channel * factor for channel in pixel]
                for pixel in frame
            ]
            minimum_over = config.source_peak_nits / config.reference_white_nits * 1.5
            current_peak = max(max(pixel) for pixel in frame)
            if current_peak < minimum_over:
                if current_peak > 0.0:
                    boost = minimum_over / current_peak
                    frame = [
                        [channel * boost for channel in pixel]
                        for pixel in frame
                    ]
                else:
                    frame = [
                        [minimum_over, minimum_over, minimum_over]
                        for _pixel in frame
                    ]
            detail["source_peak_nits"] = config.source_peak_nits
            detail["linear_peak_normalized"] = max(max(pixel) for pixel in frame)
        elif injection.kind == "BAD_TRANSFER":
            try:
                canonical_transfer("INVALID_TRANSFER_FROM_INJECTION")
            except BusinessError as exc:
                detail["declared_transfer"] = "INVALID_TRANSFER_FROM_INJECTION"
                detail["business_code"] = exc.code
            else:
                applied = False
        detail["applied"] = applied
        records.append(detail)
        errors.append(
            {
                "code": code,
                "message": message,
                "injection_kind": injection.kind,
                "injection_ordinal": injection_index,
            }
        )
    return frame, errors, records


def _process_frame(
    shared: SharedResources,
    frame_index: int,
    source_rgb_normalized: Sequence[Sequence[float]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, int | float]]:
    config = shared.config
    pixels = len(source_rgb_normalized)
    out_of_gamut_count = 0
    desaturated_count = 0
    channel_clipped_count = 0
    encoded_clip_count = 0
    source_roundtrip_skipped = 0
    pre_sum = 0.0
    post_sum = 0.0
    black_count = 0
    final_pixels: list[list[float]] = []
    encoded_pixels: list[list[float]] = []
    luminance_values: list[float] = []

    for pixel in source_rgb_normalized:
        source_nits = [
            channel * config.reference_white_nits for channel in pixel
        ]
        if not all(math.isfinite(channel) for channel in source_nits):
            raise BusinessError(
                "E_FRAME_PROCESSING",
                "source linear-light conversion overflowed",
                {"frame_index": frame_index},
            )
        if (
            shared.source_transfer.kind == "PQ"
            and any(
                channel > PQ_CONSTANTS["peak_nits"]
                for channel in source_nits
            )
        ):
            source_linear = source_nits
            source_roundtrip_skipped += 1
        else:
            source_signal = shared.source_transfer.encode_rgb(source_nits)
            source_linear = shared.source_transfer.decode_rgb(source_signal)
            if not all(math.isfinite(channel) for channel in source_linear):
                raise BusinessError(
                    "E_FRAME_PROCESSING",
                    "source transfer round trip produced a non-finite value",
                    {"frame_index": frame_index},
                )

        target_rgb = shared.color.convert_rgb(source_linear)
        if not all(math.isfinite(channel) for channel in target_rgb):
            raise BusinessError(
                "E_FRAME_PROCESSING",
                "colour conversion produced a non-finite value",
                {"frame_index": frame_index},
            )
        if shared.color.is_out_of_gamut(target_rgb):
            out_of_gamut_count += 1
        target_rgb, desaturated = shared.color.desaturate_to_gamut(target_rgb)
        if not all(math.isfinite(channel) for channel in target_rgb):
            raise BusinessError(
                "E_FRAME_PROCESSING",
                "gamut mapping produced a non-finite value",
                {"frame_index": frame_index},
            )
        if desaturated:
            desaturated_count += 1

        pre_luminance = shared.color.luminance(target_rgb)
        if pre_luminance <= 0.0:
            final_rgb = [0.0, 0.0, 0.0]
            channel_clamped = False
            black_count += 1
            pre_metric = 0.0
        else:
            final_rgb, channel_clamped = config.tone_mapper.apply_to_rgb(
                target_rgb,
                pre_luminance,
                config.target_peak_nits,
            )
            pre_metric = pre_luminance
            if channel_clamped:
                channel_clipped_count += 1
        final_rgb = [
            min(max(channel, 0.0), config.target_peak_nits)
            for channel in final_rgb
        ]
        if not all(math.isfinite(channel) for channel in final_rgb):
            raise BusinessError(
                "E_FRAME_PROCESSING",
                "tone mapping produced a non-finite value",
                {"frame_index": frame_index},
            )

        encoded = shared.target_transfer.encode_rgb(final_rgb)
        for channel_index, channel in enumerate(encoded):
            if channel < 0.0 or channel > 1.0:
                encoded_clip_count += 1
                encoded[channel_index] = min(max(channel, 0.0), 1.0)
        if not all(math.isfinite(channel) for channel in encoded):
            raise BusinessError(
                "E_FRAME_PROCESSING",
                "target transfer produced a non-finite value",
                {"frame_index": frame_index},
            )

        final_luminance = shared.color.luminance(final_rgb)
        final_pixels.append(final_rgb)
        encoded_pixels.append(encoded)
        luminance_values.append(final_luminance)
        pre_sum += pre_metric
        post_sum += final_luminance

    summary = summarize_luminance(luminance_values, config.target_peak_nits)
    pre_mean = pre_sum / pixels
    post_mean = post_sum / pixels
    if pre_sum > 0.0:
        retention = post_sum / pre_sum
        conservation = max(0.0, 1.0 - abs(post_sum - pre_sum) / pre_sum)
    else:
        retention = 1.0 if post_sum == 0.0 else 0.0
        conservation = retention
    stats: dict[str, Any] = {
        "frame_index": frame_index,
        "valid": True,
        "status": "ok",
        "pixel_count": pixels,
        "luminance_mean_nits": summary["luminance_mean_nits"],
        "luminance_peak_nits": summary["luminance_peak_nits"],
        "over_target_peak_pixel_ratio": summary["over_target_peak_pixel_ratio"],
        "out_of_gamut_pixel_ratio": out_of_gamut_count / pixels,
        "gamut_desaturated_pixel_ratio": desaturated_count / pixels,
        "channel_clipped_pixel_ratio": channel_clipped_count / pixels,
        "encoded_signal_clipped_channel_ratio": encoded_clip_count / (pixels * 3),
        "source_roundtrip_skipped_pixel_ratio": source_roundtrip_skipped / pixels,
        "pre_tone_luminance_mean_nits": pre_mean,
        "post_tone_luminance_mean_nits": post_mean,
        "luminance_retention_ratio": retention,
        "mapping_luminance_conservation": conservation,
        "black_pixel_count": black_count,
        "_pixel_count": summary["pixel_count"],
        "_over_count": summary["over_target_peak_pixel_count"],
        "_luminance_sum": summary["luminance_sum_nits"],
    }
    entry = {
        "frame_index": frame_index,
        "status": "ok",
        "linear_nits": final_pixels,
        "encoded_rgb": encoded_pixels,
    }
    frame_totals = {
        "valid_pixel_count": pixels,
        "pre_tone_luminance_sum": pre_sum,
        "post_tone_luminance_sum": post_sum,
        "black_pixel_count": black_count,
        "out_of_gamut_pixel_count": out_of_gamut_count,
        "gamut_desaturated_pixel_count": desaturated_count,
        "channel_clipped_pixel_count": channel_clipped_count,
        "encoded_channel_clip_count": encoded_clip_count,
        "source_roundtrip_skipped_pixel_count": source_roundtrip_skipped,
    }
    return entry, stats, frame_totals


def validate_chunk_result(
    result: dict[str, Any], expected_start: int, expected_end: int
) -> None:
    if (
        result.get("status") != "complete"
        or result.get("start_frame") != expected_start
        or result.get("end_frame_exclusive") != expected_end
    ):
        raise BusinessError(
            "E_CHECKPOINT",
            "persisted chunk metadata does not match the current task",
            {
                "expected_start": expected_start,
                "expected_end": expected_end,
                "chunk_index": result.get("chunk_index"),
            },
        )
