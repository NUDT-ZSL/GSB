"""Non-overlapping severity bands, aggregate grades, and critical-band flags."""

from __future__ import annotations


GRADE_ORDER = {"clean": 0, "light": 1, "medium": 2, "heavy": 3}
GRADE_NAMES_ZH = {"clean": "无", "light": "轻", "medium": "中", "heavy": "重"}

CLIP_LIGHT_MAX = 0.02
CLIP_MEDIUM_MAX = 0.15

DC_LIGHT_MAX = 0.04
DC_MEDIUM_MAX = 0.12

NOISE_LIGHT_MAX_DBFS = -56.0
NOISE_MEDIUM_MAX_DBFS = -46.0

IMPULSE_DENSITY_LIGHT_MAX = 0.4
IMPULSE_DENSITY_MEDIUM_MAX = 2.0
IMPULSE_PEAK_LIGHT_MAX = 0.30
IMPULSE_PEAK_MEDIUM_MAX = 0.60


THRESHOLD_TABLE = {
    "clipping": {
        "metric": "segment_clip_coverage",
        "light": "[0, 0.02)",
        "medium": "[0.02, 0.15)",
        "heavy": "[0.15, +inf)",
        "note": "削波帧数 × 帧移 / 段时长；与帧长无关，便于双帧长复核。",
    },
    "dc_offset": {
        "metric": "abs_median_dc_offset",
        "light": "[0.015, 0.04)",
        "medium": "[0.04, 0.12)",
        "heavy": "[0.12, +inf)",
        "note": "检测门槛 0.015；分级用段内各 DC 段中位绝对直流。",
    },
    "noise": {
        "metric": "median_noise_floor_dbfs",
        "light": "(-66, -56)",
        "medium": "[-56, -46)",
        "heavy": "[-46, 0]",
        "note": "自适应低能量分位噪声底；检测门槛为基线 +6 dB，另有 -66 dBFS 全段兜底。",
    },
    "impulse": {
        "metric": "density_per_second / peak_amplitude",
        "light": "density < 0.4/s and peak < 0.30",
        "medium": "0.4/s <= density < 2.0/s or 0.30 <= peak < 0.60",
        "heavy": "density >= 2.0/s or peak >= 0.60",
        "note": "两个子指标取较严等级；密度按段时长计算。",
    },
}


def grade_clipping(coverage: float) -> str:
    if coverage >= CLIP_MEDIUM_MAX:
        return "heavy"
    if coverage >= CLIP_LIGHT_MAX:
        return "medium"
    return "light"


def grade_dc_offset(abs_dc: float) -> str:
    if abs_dc >= DC_MEDIUM_MAX:
        return "heavy"
    if abs_dc >= DC_LIGHT_MAX:
        return "medium"
    return "light"


def grade_noise(noise_dbfs: float) -> str:
    if noise_dbfs >= NOISE_MEDIUM_MAX_DBFS:
        return "heavy"
    if noise_dbfs >= NOISE_LIGHT_MAX_DBFS:
        return "medium"
    return "light"


def grade_impulse(density_per_second: float, peak_amplitude: float) -> tuple[str, dict]:
    if density_per_second >= IMPULSE_DENSITY_MEDIUM_MAX:
        density_grade = "heavy"
    elif density_per_second >= IMPULSE_DENSITY_LIGHT_MAX:
        density_grade = "medium"
    else:
        density_grade = "light"
    if peak_amplitude >= IMPULSE_PEAK_MEDIUM_MAX:
        peak_grade = "heavy"
    elif peak_amplitude >= IMPULSE_PEAK_LIGHT_MAX:
        peak_grade = "medium"
    else:
        peak_grade = "light"
    grade = max((density_grade, peak_grade), key=lambda item: GRADE_ORDER[item])
    return grade, {"density_grade": density_grade, "peak_grade": peak_grade}


def segment_overall_grade(type_grades: dict[str, str | None]) -> tuple[str, str]:
    present = {key: value for key, value in type_grades.items() if value and value != "clean"}
    if not present:
        return "clean", "未检出受损类型"
    if any(value == "heavy" for value in present.values()):
        return "heavy", "存在重度受损类型，录音级取最严等级"
    mediums = [key for key, value in present.items() if value == "medium"]
    if len(mediums) >= 2:
        return "heavy", "两类及以上中度受损并存，按组合风险提升为重度"
    if mediums:
        return "medium", "存在一类中度受损，录音级取该等级"
    return "light", "仅有轻度受损类型"


def critical_conditions(name: str, value: float | None, thresholds: list[tuple[float, float]]) -> list[dict]:
    flags: list[dict] = []
    if value is None:
        return flags
    for threshold, margin in thresholds:
        lo = threshold - margin
        hi = threshold + margin
        if lo <= value <= hi:
            flags.append(
                {
                    "metric": name,
                    "value": float(value),
                    "threshold": float(threshold),
                    "critical_band": [float(lo), float(hi)],
                    "condition": (
                        f"{name}={value:.6g} 落在分级阈值 {threshold:g} 的"
                        f" ±{margin:g} 临界带 [{lo:g}, {hi:g}] 内，需人工复核分级。"
                    ),
                }
            )
    return flags


def critical_flags_for_intervals(intervals: dict[str, list[dict]], segment_duration: int, sample_rate: int) -> list[dict]:
    flags: list[dict] = []
    for band in intervals.get("clipping", []):
        flags.extend(
            critical_conditions(
                "segment_clip_coverage",
                float(band.get("segment_coverage", 0.0)),
                [(CLIP_LIGHT_MAX, 0.003), (CLIP_MEDIUM_MAX, 0.015)],
            )
        )
    for band in intervals.get("dc_offset", []):
        flags.extend(
            critical_conditions(
                "abs_median_dc_offset",
                float(band.get("abs_dc_offset", 0.0)),
                [(DC_LIGHT_MAX, 0.004), (DC_MEDIUM_MAX, 0.0144)],
            )
        )
    for band in intervals.get("noise", []):
        flags.extend(
            critical_conditions(
                "median_noise_floor_dbfs",
                float(band.get("median_noise_floor_dbfs", 0.0)),
                [(NOISE_LIGHT_MAX_DBFS, 2.0), (NOISE_MEDIUM_MAX_DBFS, 2.0)],
            )
        )
    impulses = intervals.get("impulse", [])
    if impulses:
        duration_seconds = max(1.0 / float(sample_rate), segment_duration / float(sample_rate))
        density = len(impulses) / duration_seconds
        peak = max(float(item.get("peak_amplitude", 0.0)) for item in impulses)
        flags.extend(
            critical_conditions(
                "impulse_density_per_second",
                density,
                [(IMPULSE_DENSITY_LIGHT_MAX, 0.08), (IMPULSE_DENSITY_MEDIUM_MAX, 0.30)],
            )
        )
        flags.extend(
            critical_conditions(
                "impulse_peak_amplitude",
                peak,
                [(IMPULSE_PEAK_LIGHT_MAX, 0.035), (IMPULSE_PEAK_MEDIUM_MAX, 0.06)],
            )
        )
    # De-duplicate identical metric/threshold pairs.
    seen = set()
    unique: list[dict] = []
    for flag in flags:
        key = (flag["metric"], round(flag["threshold"], 12), round(float(flag["value"]), 9))
        if key in seen:
            continue
        seen.add(key)
        unique.append(flag)
    return unique
