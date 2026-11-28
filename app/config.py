from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
ARTIFACTS_DIR = BASE_DIR / "artifacts"
STATIC_DIR = BASE_DIR / "app" / "static"

DB_PATH = Path(os.environ.get("MOTION_DB_PATH", DATA_DIR / "motion_ranking.db"))
GENERATOR_SEED = 2026_11_28
GENERATOR_VERSION = "1.0.0"
GENERATED_AT = "2026-01-01T00:00:00+00:00"

PHASES = ("baseline", "build", "deload", "peak", "recovery")
CHANNELS = ("ax", "ay", "az", "gx", "gy", "gz")
ACCELERATION_CHANNELS = CHANNELS[:3]
GYROSCOPE_CHANNELS = CHANNELS[3:]
CANONICAL_SAMPLE_COUNT = 128
SCORE_DECIMALS = 6
RANK_THRESHOLD = 0
SCORE_TOLERANCE = 1e-6
TOP_K = 3
FOCUS_RANK_DROP_THRESHOLD = 3

ACTION_CATEGORIES = (
    "sprint_run",
    "jump",
    "squat",
    "lunge",
    "deadlift",
    "clean_jerk",
    "medicine_ball_throw",
    "gymnastics_tumble",
    "swim_pull",
    "rowing_drive",
    "balance_hold",
    "agility_cut",
)

SPECIALTIES = (
    "短跑",
    "举重",
    "体操",
    "游泳",
    "跳远",
    "排球",
    "赛艇",
    "篮球",
)

BODY_PARTS = (
    "ankle",
    "knee",
    "hip",
    "lower_back",
    "shoulder",
    "elbow",
    "wrist",
    "neck",
)

INJURY_TAG_BODY_PART = {
    "左膝前叉韧带": "knee",
    "右膝半月板": "knee",
    "髌腱炎": "knee",
    "左踝扭伤": "ankle",
    "右踝距腓韧带": "ankle",
    "跟腱炎": "ankle",
    "下背": "lower_back",
    "腰椎间盘": "lower_back",
    "腰肌劳损": "lower_back",
    "肩袖": "shoulder",
    "左肩": "shoulder",
    "右肩盂唇": "shoulder",
    "肘部劳损": "elbow",
    "腕关节": "wrist",
    "髋屈肌": "hip",
    "颈部劳损": "neck",
}

INJURY_TAG_SEVERITY = {
    "左膝前叉韧带": 0.95,
    "右膝半月板": 0.78,
    "髌腱炎": 0.60,
    "左踝扭伤": 0.58,
    "右踝距腓韧带": 0.73,
    "跟腱炎": 0.66,
    "下背": 0.62,
    "腰椎间盘": 0.92,
    "腰肌劳损": 0.54,
    "肩袖": 0.86,
    "左肩": 0.70,
    "右肩盂唇": 0.77,
    "肘部劳损": 0.56,
    "腕关节": 0.48,
    "髋屈肌": 0.60,
    "颈部劳损": 0.50,
}

ACTION_PROFILES = {
    "sprint_run": {"load": 0.78, "body_parts": ("knee", "ankle", "hip"), "demand_years": 5},
    "jump": {"load": 0.86, "body_parts": ("knee", "ankle", "hip"), "demand_years": 6},
    "squat": {"load": 0.72, "body_parts": ("knee", "hip", "lower_back"), "demand_years": 4},
    "lunge": {"load": 0.64, "body_parts": ("knee", "hip", "ankle"), "demand_years": 4},
    "deadlift": {"load": 0.90, "body_parts": ("lower_back", "hip", "knee"), "demand_years": 6},
    "clean_jerk": {"load": 0.94, "body_parts": ("lower_back", "knee", "shoulder", "wrist"), "demand_years": 7},
    "medicine_ball_throw": {"load": 0.67, "body_parts": ("shoulder", "elbow", "wrist"), "demand_years": 4},
    "gymnastics_tumble": {"load": 0.83, "body_parts": ("shoulder", "wrist", "elbow", "ankle"), "demand_years": 7},
    "swim_pull": {"load": 0.58, "body_parts": ("shoulder", "elbow", "neck"), "demand_years": 3},
    "rowing_drive": {"load": 0.70, "body_parts": ("lower_back", "knee", "elbow"), "demand_years": 4},
    "balance_hold": {"load": 0.35, "body_parts": ("ankle", "knee"), "demand_years": 2},
    "agility_cut": {"load": 0.76, "body_parts": ("knee", "ankle", "hip"), "demand_years": 5},
}

SPECIALTY_ACTION_AFFINITY = {
    "短跑": {"sprint_run": 1.0, "jump": 0.84, "agility_cut": 0.88, "lunge": 0.63, "balance_hold": 0.48},
    "举重": {"clean_jerk": 1.0, "deadlift": 0.96, "squat": 0.90, "lunge": 0.58, "rowing_drive": 0.42},
    "体操": {"gymnastics_tumble": 1.0, "balance_hold": 0.85, "jump": 0.72, "lunge": 0.48, "squat": 0.53},
    "游泳": {"swim_pull": 1.0, "rowing_drive": 0.62, "medicine_ball_throw": 0.55, "balance_hold": 0.42},
    "跳远": {"jump": 1.0, "sprint_run": 0.91, "agility_cut": 0.76, "squat": 0.65, "lunge": 0.56},
    "排球": {"jump": 0.94, "agility_cut": 0.91, "medicine_ball_throw": 0.80, "sprint_run": 0.65, "balance_hold": 0.54},
    "赛艇": {"rowing_drive": 1.0, "deadlift": 0.72, "swim_pull": 0.51, "squat": 0.60},
    "篮球": {"agility_cut": 1.0, "sprint_run": 0.87, "jump": 0.85, "lunge": 0.60, "medicine_ball_throw": 0.48},
}

DEFAULT_PHASE_WEIGHTS = {
    "baseline": {"quality": 0.45, "risk": 0.25, "profile": 0.30},
    "build": {"quality": 0.40, "risk": 0.30, "profile": 0.30},
    "deload": {"quality": 0.30, "risk": 0.40, "profile": 0.30},
    "peak": {"quality": 0.50, "risk": 0.25, "profile": 0.25},
    "recovery": {"quality": 0.30, "risk": 0.50, "profile": 0.20},
}

PHASE_LOAD_MULTIPLIER = {
    "baseline": 1.00,
    "build": 1.08,
    "deload": 0.82,
    "peak": 1.16,
    "recovery": 0.74,
}

ACTION_CATEGORY_CODE = {name: index for index, name in enumerate(sorted(ACTION_CATEGORIES))}
SPECIALTY_CODE = {name: index for index, name in enumerate(sorted(SPECIALTIES))}
BODY_PART_CODE = {name: index for index, name in enumerate(sorted(BODY_PARTS))}


def default_weights(phase: str) -> dict[str, float]:
    if phase not in DEFAULT_PHASE_WEIGHTS:
        raise KeyError(phase)
    return dict(DEFAULT_PHASE_WEIGHTS[phase])


def validate_weights(weights: dict[str, float]) -> dict[str, float]:
    expected = {"quality", "risk", "profile"}
    if set(weights) != expected:
        raise ValueError("权重必须且只能包含 quality、risk、profile")
    normalized = {key: float(weights[key]) for key in ("quality", "risk", "profile")}
    if any(value < 0 or value > 1 for value in normalized.values()):
        raise ValueError("权重必须位于 [0, 1]")
    if abs(sum(normalized.values()) - 1.0) > 1e-6:
        raise ValueError("三项权重之和必须为 1")
    return normalized
