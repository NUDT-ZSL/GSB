from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any

import numpy as np

from app.config import (
    ACCELERATION_CHANNELS,
    ACTION_CATEGORIES,
    ACTION_PROFILES,
    ARTIFACTS_DIR,
    DB_PATH,
    DEFAULT_PHASE_WEIGHTS,
    GENERATED_AT,
    GENERATOR_SEED,
    GENERATOR_VERSION,
    GYROSCOPE_CHANNELS,
    INJURY_TAG_BODY_PART,
    SPECIALTY_ACTION_AFFINITY,
    default_weights,
)
from app.db import init_db, json_dump, transaction

ATHLETE_SPECS = (
    {
        "athlete_id": "A001",
        "name": "林越",
        "specialty": "短跑",
        "training_years": 9,
        "injury_tags": ["右膝半月板", "左膝前叉韧带"],
        "phases": ("baseline", "deload"),
    },
    {
        "athlete_id": "A002",
        "name": "周擎",
        "specialty": "举重",
        "training_years": 6,
        "injury_tags": ["腰椎间盘", "下背"],
        "phases": ("peak", "recovery"),
    },
    {
        "athlete_id": "A003",
        "name": "沈岚",
        "specialty": "体操",
        "training_years": 11,
        "injury_tags": ["左肩", "肩袖"],
        "phases": ("build", "deload"),
    },
    {
        "athlete_id": "A004",
        "name": "许澄",
        "specialty": "游泳",
        "training_years": 4,
        "injury_tags": ["肩袖"],
        "phases": ("build", "recovery"),
    },
    {
        "athlete_id": "A005",
        "name": "顾骁",
        "specialty": "跳远",
        "training_years": 7,
        "injury_tags": ["左踝扭伤", "跟腱炎"],
        "phases": ("deload", "peak"),
    },
    {
        "athlete_id": "A006",
        "name": "唐沐",
        "specialty": "排球",
        "training_years": 5,
        "injury_tags": ["右肩盂唇", "右踝距腓韧带"],
        "phases": ("baseline", "recovery"),
    },
    {
        "athlete_id": "A007",
        "name": "陆川",
        "specialty": "赛艇",
        "training_years": 8,
        "injury_tags": ["腰肌劳损", "肘部劳损"],
        "phases": ("baseline", "peak"),
    },
    {
        "athlete_id": "A008",
        "name": "叶青",
        "specialty": "篮球",
        "training_years": 3,
        "injury_tags": ["左膝前叉韧带", "右膝半月板", "髋屈肌"],
        "phases": ("build", "peak"),
    },
)


def _profile_snapshot(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "athlete_id": spec["athlete_id"],
        "name": spec["name"],
        "specialty": spec["specialty"],
        "training_years": spec["training_years"],
        "injury_tags": sorted(spec["injury_tags"]),
        "snapshot_version": 1,
    }


def _choice_pool(specialty: str) -> list[str]:
    affinity = SPECIALTY_ACTION_AFFINITY[specialty]
    return sorted(
        ACTION_CATEGORIES,
        key=lambda action: (-affinity.get(action, 0.0), action),
    )


def _injury_related_actions(injury_tags: list[str]) -> list[str]:
    injured_parts = {
        INJURY_TAG_BODY_PART[tag]
        for tag in injury_tags
        if tag in INJURY_TAG_BODY_PART
    }
    return sorted(
        action
        for action, profile in ACTION_PROFILES.items()
        if injured_parts & set(profile["body_parts"])
    )


def _pick_action(
    rng: random.Random,
    specialty: str,
    injury_tags: list[str],
    candidate_index: int,
) -> str:
    pool = _choice_pool(specialty)
    injury_actions = _injury_related_actions(injury_tags)
    if candidate_index % 3 == 1 and injury_actions:
        return injury_actions[(candidate_index // 3) % len(injury_actions)]
    top = pool[:6]
    weights = [max(1.0, 7.0 - index * 0.95) for index in range(len(top))]
    return rng.choices(top, weights=weights, k=1)[0]


def _make_channel_series(
    *,
    rng: np.random.Generator,
    sample_count: int,
    duration: float,
    frequency: float,
    mean: float,
    amplitude: float,
    phase: float,
    noise: float,
    spike_boost: float,
) -> list[float]:
    t = np.linspace(0.0, duration, sample_count, endpoint=False)
    carrier = np.sin(2.0 * math.pi * frequency * t + phase)
    harmonic = 0.24 * np.sin(4.0 * math.pi * frequency * t + phase * 0.53)
    drift = 0.10 * np.sin(2.0 * math.pi * 0.31 * t + phase * 0.2)
    series = mean + amplitude * (carrier + harmonic + drift)
    series = series + rng.normal(0.0, noise, size=sample_count)

    spike_position = int(rng.integers(sample_count // 5, max(sample_count // 5 + 1, sample_count - 5)))
    spike_width = max(1.0, sample_count / 55.0)
    spike = np.exp(-0.5 * ((np.arange(sample_count) - spike_position) / spike_width) ** 2)
    series = series + spike_boost * amplitude * spike
    return np.round(series, 6).tolist()


def _generate_sensor_arrays(
    seed: int,
    *,
    repetition_count: int,
    duration: float,
    sample_count: int,
    load_bias: float,
    quality_score: float,
) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    rng = np.random.default_rng(seed)
    frequency = repetition_count / duration
    quality_factor = max(0.30, min(1.20, quality_score / 78.0))
    base_noise = 0.095 / quality_factor
    acceleration: dict[str, list[float]] = {}
    angular_velocity: dict[str, list[float]] = {}
    for index, channel in enumerate(ACCELERATION_CHANNELS):
        acceleration[channel] = _make_channel_series(
            rng=rng,
            sample_count=sample_count,
            duration=duration,
            frequency=frequency * (1.0 + index * 0.012),
            mean=float(rng.uniform(-0.65, 0.65)),
            amplitude=(3.2 + 5.6 * load_bias) * float(rng.uniform(0.82, 1.18)),
            phase=float(rng.uniform(0.0, 2.0 * math.pi)),
            noise=base_noise * 2.6,
            spike_boost=float(rng.uniform(0.55, 1.30)),
        )
    for index, channel in enumerate(GYROSCOPE_CHANNELS):
        angular_velocity[channel] = _make_channel_series(
            rng=rng,
            sample_count=sample_count,
            duration=duration,
            frequency=frequency * (1.0 + index * 0.018),
            mean=float(rng.uniform(-0.20, 0.20)),
            amplitude=(0.55 + 2.65 * load_bias) * float(rng.uniform(0.78, 1.22)),
            phase=float(rng.uniform(0.0, 2.0 * math.pi)),
            noise=base_noise * 0.75,
            spike_boost=float(rng.uniform(0.30, 0.90)),
        )
    return acceleration, angular_velocity


def _request_candidates(
    *,
    spec: dict[str, Any],
    phase: str,
    request_index: int,
    athlete_index: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    rng = random.Random(GENERATOR_SEED + athlete_index * 10_000 + request_index * 100)
    request_id = f"R-{spec['athlete_id']}-{phase.upper()}"
    candidates: list[dict[str, Any]] = []
    for local_index in range(1, 13):
        action = _pick_action(rng, spec["specialty"], spec["injury_tags"], local_index)
        set_index = (local_index - 1) // 4 + 1
        sequence_index = (local_index - 1) % 4 + 1
        phase_load = {
            "baseline": 1.00,
            "build": 1.10,
            "deload": 0.82,
            "peak": 1.20,
            "recovery": 0.76,
        }[phase]
        load_bias = rng.uniform(0.78, 1.40) * phase_load
        if local_index % 3 == 1:
            load_bias *= 1.12
        repetition_count = 6 + sequence_index * 2 + rng.randint(0, 3)
        duration = round(rng.uniform(4.2, 10.8), 3)
        sample_count = rng.choice((96, 128, 160, 192, 224, 256))
        sampling_rate = round(sample_count / duration, 3)

        manual_quality = rng.uniform(72.0, 94.0)
        if local_index % 3 == 1:
            manual_quality -= rng.uniform(1.0, 5.0)
        if set_index == 3:
            manual_quality -= rng.uniform(11.0, 20.0)
        manual_quality += (rng.random() - 0.5) * 4.0
        manual_quality = round(max(42.0, min(99.0, manual_quality)), 2)

        seed = (
            GENERATOR_SEED
            + athlete_index * 1_000_000
            + request_index * 100_000
            + local_index * 1_000
        )
        acceleration, angular_velocity = _generate_sensor_arrays(
            seed,
            repetition_count=repetition_count,
            duration=duration,
            sample_count=sample_count,
            load_bias=load_bias,
            quality_score=manual_quality,
        )
        clip_id = f"{request_id}-C{local_index:02d}"
        candidates.append(
            {
                "clip_id": clip_id,
                "athlete_id": spec["athlete_id"],
                "action_category": action,
                "repetition_count": repetition_count,
                "duration_sec": duration,
                "manual_quality_score": manual_quality,
                "sampling_rate_hz": sampling_rate,
                "acceleration": acceleration,
                "angular_velocity": angular_velocity,
                "metadata": {
                    "set_index": set_index,
                    "sequence_index": sequence_index,
                    "is_repeat_group_tail": set_index == 3,
                    "source": "deterministic_generator",
                },
            }
        )

    prior_order = [clip["clip_id"] for clip in candidates]
    rng.shuffle(prior_order)
    return candidates, prior_order


def generate_dataset(
    db_path: str | Path | None = None,
    *,
    seed: int = GENERATOR_SEED,
    write_artifacts: bool = True,
) -> dict[str, Any]:
    if seed != GENERATOR_SEED:
        raise ValueError(f"题内生成器仅支持固定种子 {GENERATOR_SEED}")
    target_path = Path(db_path or DB_PATH)
    init_db(target_path, reset=True)
    counts = {
        "athletes": 0,
        "training_clips": 0,
        "ranking_requests": 0,
        "request_candidates": 0,
    }
    phase_counts: dict[str, int] = {}

    with transaction(target_path) as connection:
        for athlete_index, spec in enumerate(ATHLETE_SPECS):
            profile = _profile_snapshot(spec)
            connection.execute(
                """
                INSERT INTO athletes
                (athlete_id, name, specialty, training_years, injury_tags, profile_snapshot, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    spec["athlete_id"],
                    spec["name"],
                    spec["specialty"],
                    spec["training_years"],
                    json_dump(profile["injury_tags"]),
                    json_dump(profile),
                    GENERATED_AT,
                ),
            )
            counts["athletes"] += 1

            for request_index, phase in enumerate(spec["phases"]):
                request_id = f"R-{spec['athlete_id']}-{phase.upper()}"
                candidates, prior_order = _request_candidates(
                    spec=spec,
                    phase=phase,
                    request_index=request_index,
                    athlete_index=athlete_index,
                )
                prior_ranks = {
                    clip_id: index + 1 for index, clip_id in enumerate(prior_order)
                }
                weights = default_weights(phase)
                request_config = {
                    "request_id": request_id,
                    "selection_basis": ["action_category", "duration_sec"],
                    "phase_weight_profiles": DEFAULT_PHASE_WEIGHTS,
                    "default_weights": weights,
                    "weights_declared": True,
                    "candidate_count": len(candidates),
                    "tie_break": "clip_id_ascending",
                    "profile_snapshot_version": 1,
                }
                connection.execute(
                    """
                    INSERT INTO ranking_requests
                    (request_id, athlete_id, phase, weights, profile_snapshot,
                     candidate_ids, prior_rank, config, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        spec["athlete_id"],
                        phase,
                        json_dump(weights),
                        json_dump(profile),
                        json_dump([clip["clip_id"] for clip in candidates]),
                        json_dump(prior_order),
                        json_dump(request_config),
                        GENERATED_AT,
                    ),
                )
                counts["ranking_requests"] += 1
                phase_counts[phase] = phase_counts.get(phase, 0) + 1

                for clip in candidates:
                    connection.execute(
                        """
                        INSERT INTO training_clips
                        (clip_id, athlete_id, action_category, repetition_count,
                         duration_sec, manual_quality_score, sampling_rate_hz,
                         acceleration, angular_velocity, metadata, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            clip["clip_id"],
                            clip["athlete_id"],
                            clip["action_category"],
                            clip["repetition_count"],
                            clip["duration_sec"],
                            clip["manual_quality_score"],
                            clip["sampling_rate_hz"],
                            json_dump(clip["acceleration"]),
                            json_dump(clip["angular_velocity"]),
                            json_dump(clip["metadata"]),
                            GENERATED_AT,
                        ),
                    )
                    counts["training_clips"] += 1
                    connection.execute(
                        """
                        INSERT INTO request_candidates
                        (request_id, clip_id, prior_rank, input_order)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            request_id,
                            clip["clip_id"],
                            prior_ranks[clip["clip_id"]],
                            int(clip["clip_id"].rsplit("C", 1)[1]),
                        ),
                    )
                    counts["request_candidates"] += 1

    summary = {
        "athletes": counts["athletes"],
        "candidates_per_request": 12,
        "counts": {
            "athletes": counts["athletes"],
            "training_clips": counts["training_clips"],
            "ranking_requests": counts["ranking_requests"],
            "request_candidates": counts["request_candidates"],
        },
        "db_path": str(target_path.resolve()),
        "generated_at": GENERATED_AT,
        "generator_version": GENERATOR_VERSION,
        "phases": list(DEFAULT_PHASE_WEIGHTS),
        "request_phase_counts": dict(sorted(phase_counts.items())),
        "requests": counts["ranking_requests"],
        "seed": seed,
    }
    if write_artifacts:
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        summary_path = ARTIFACTS_DIR / "generation_summary.json"
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return summary
