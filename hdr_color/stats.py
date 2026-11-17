"""Frame/window luminance statistics derived only from final RGB frames."""

from __future__ import annotations

from typing import Sequence


PEAK_TOLERANCE = 1e-9


def summarize_luminance(
    luminance_values: Sequence[float],
    target_peak_nits: float,
) -> dict[str, float | int]:
    count = len(luminance_values)
    if count == 0:
        return {
            "pixel_count": 0,
            "luminance_mean_nits": None,
            "luminance_peak_nits": None,
            "over_target_peak_pixel_ratio": None,
            "luminance_sum_nits": 0.0,
            "over_target_peak_pixel_count": 0,
        }
    total = float(sum(luminance_values))
    peak = float(max(luminance_values))
    threshold = target_peak_nits * (1.0 + PEAK_TOLERANCE) + 1e-12
    over_count = sum(1 for value in luminance_values if value > threshold)
    return {
        "pixel_count": int(count),
        "luminance_mean_nits": total / count,
        "luminance_peak_nits": peak,
        "over_target_peak_pixel_ratio": over_count / count,
        "luminance_sum_nits": total,
        "over_target_peak_pixel_count": int(over_count),
    }


def build_windows(
    frame_stats: Sequence[dict[str, object]],
    total_frames: int,
    window_frames: int,
    tail_mode: str,
) -> list[dict[str, object]]:
    """Build [start_frame, end_frame_exclusive) windows.

    tail_mode="include" emits the final short window; "drop" omits it.
    Invalid frames stay inside the index range but are excluded from values.
    """

    windows: list[dict[str, object]] = []
    start = 0
    window_index = 0
    while start < total_frames:
        end = min(start + window_frames, total_frames)
        is_partial = (end - start) < window_frames
        if is_partial and tail_mode == "drop":
            break
        valid = [
            frame_stats[index]
            for index in range(start, end)
            if bool(frame_stats[index].get("valid"))
        ]
        pixel_count = sum(int(item["_pixel_count"]) for item in valid)
        over_count = sum(int(item["_over_count"]) for item in valid)
        luminance_sum = sum(float(item["_luminance_sum"]) for item in valid)
        if pixel_count:
            mean = luminance_sum / pixel_count
            peak = max(float(item["luminance_peak_nits"]) for item in valid)
            ratio = over_count / pixel_count
        else:
            mean = None
            peak = None
            ratio = None
        windows.append(
            {
                "window_index": window_index,
                "start_frame": start,
                "end_frame_exclusive": end,
                "frame_count": end - start,
                "valid_frame_count": len(valid),
                "invalid_frame_count": (end - start) - len(valid),
                "is_partial_tail": is_partial,
                "luminance_mean_nits": mean,
                "luminance_peak_nits": peak,
                "over_target_peak_pixel_ratio": ratio,
            }
        )
        window_index += 1
        start = end
    return windows


def aggregate_frames(
    frame_stats: Sequence[dict[str, object]]
) -> dict[str, object]:
    valid = [item for item in frame_stats if bool(item.get("valid"))]
    pixel_count = sum(int(item["_pixel_count"]) for item in valid)
    over_count = sum(int(item["_over_count"]) for item in valid)
    luminance_sum = sum(float(item["_luminance_sum"]) for item in valid)
    if pixel_count:
        mean = luminance_sum / pixel_count
        peak = max(float(item["luminance_peak_nits"]) for item in valid)
        ratio = over_count / pixel_count
    else:
        mean = None
        peak = None
        ratio = None
    return {
        "frame_count": len(frame_stats),
        "valid_frame_count": len(valid),
        "invalid_frame_count": len(frame_stats) - len(valid),
        "pixel_count": pixel_count,
        "luminance_mean_nits": mean,
        "luminance_peak_nits": peak,
        "over_target_peak_pixel_ratio": ratio,
    }


def normalize_frame_stat(item: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in item.items()
        if not key.startswith("_")
    }
