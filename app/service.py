from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import (
    DB_PATH,
    PHASES,
    default_weights,
    validate_weights,
)
from app.db import (
    connect,
    fetch_athlete,
    fetch_clip,
    fetch_request,
    json_dump,
    json_load,
)
from app.errors import BusinessError
from app.ranking import rank_candidates


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clip_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "clip_id": row["clip_id"],
        "athlete_id": row["athlete_id"],
        "action_category": row["action_category"],
        "repetition_count": row["repetition_count"],
        "duration_sec": row["duration_sec"],
        "manual_quality_score": row["manual_quality_score"],
        "sampling_rate_hz": row["sampling_rate_hz"],
        "acceleration": json_load(row["acceleration"], {}),
        "angular_velocity": json_load(row["angular_velocity"], {}),
        "metadata": json_load(row["metadata"], {}),
        "created_at": row["created_at"],
    }


def athlete_row_to_dict(row: Any) -> dict[str, Any]:
    profile = json_load(row["profile_snapshot"], {})
    return {
        "athlete_id": row["athlete_id"],
        "name": row["name"],
        "specialty": row["specialty"],
        "training_years": row["training_years"],
        "injury_tags": json_load(row["injury_tags"], []),
        "profile_snapshot": profile,
        "has_profile_snapshot": bool(profile),
    }


def request_row_to_dict(
    row: Any,
    *,
    include_result: bool = False,
    connection: Any = None,
) -> dict[str, Any]:
    payload = {
        "request_id": row["request_id"],
        "athlete_id": row["athlete_id"],
        "phase": row["phase"],
        "weights": json_load(row["weights"], {}),
        "profile_snapshot": json_load(row["profile_snapshot"], {}),
        "candidate_ids": json_load(row["candidate_ids"], []),
        "prior_rank": json_load(row["prior_rank"], []),
        "config": json_load(row["config"], {}),
        "created_at": row["created_at"],
    }
    if include_result and connection is not None:
        result_row = connection.execute(
            "SELECT result, updated_at FROM ranking_results WHERE request_id = ?",
            (row["request_id"],),
        ).fetchone()
        payload["result"] = json_load(result_row["result"], None) if result_row else None
        payload["result_updated_at"] = result_row["updated_at"] if result_row else None
    return payload


def list_athletes(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    with connect(db_path or DB_PATH) as connection:
        rows = connection.execute(
            "SELECT * FROM athletes ORDER BY athlete_id"
        ).fetchall()
        return [athlete_row_to_dict(row) for row in rows]


def get_athlete(athlete_id: str, db_path: str | Path | None = None) -> dict[str, Any]:
    with connect(db_path or DB_PATH) as connection:
        row = fetch_athlete(connection, athlete_id)
        if row is None:
            raise BusinessError(
                "ATHLETE_NOT_FOUND",
                f"运动员 {athlete_id} 不存在",
                status_code=404,
                details={"athlete_id": athlete_id},
            )
        return athlete_row_to_dict(row)


def list_athlete_clips(
    athlete_id: str,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    with connect(db_path or DB_PATH) as connection:
        athlete_row = fetch_athlete(connection, athlete_id)
        if athlete_row is None:
            raise BusinessError(
                "ATHLETE_NOT_FOUND",
                f"运动员 {athlete_id} 不存在",
                status_code=404,
                details={"athlete_id": athlete_id},
            )
        rows = connection.execute(
            """
            SELECT clip_id, action_category, repetition_count, duration_sec,
                   manual_quality_score, sampling_rate_hz, metadata
            FROM training_clips
            WHERE athlete_id = ?
            ORDER BY clip_id
            """,
            (athlete_id,),
        ).fetchall()
        return [
            {
                "clip_id": row["clip_id"],
                "action_category": row["action_category"],
                "repetition_count": row["repetition_count"],
                "duration_sec": row["duration_sec"],
                "manual_quality_score": row["manual_quality_score"],
                "sampling_rate_hz": row["sampling_rate_hz"],
                "metadata": json_load(row["metadata"], {}),
            }
            for row in rows
        ]


def list_requests(
    *,
    athlete_id: str | None = None,
    phase: str | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    if phase is not None and phase not in PHASES:
        raise BusinessError(
            "INVALID_PHASE",
            f"不支持的训练周期阶段: {phase}",
            status_code=422,
            details={"allowed": list(PHASES)},
        )
    clauses: list[str] = []
    params: list[Any] = []
    if athlete_id:
        clauses.append("athlete_id = ?")
        params.append(athlete_id)
    if phase:
        clauses.append("phase = ?")
        params.append(phase)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect(db_path or DB_PATH) as connection:
        rows = connection.execute(
            f"SELECT * FROM ranking_requests {where} ORDER BY athlete_id, phase",
            params,
        ).fetchall()
        items = []
        for row in rows:
            item = request_row_to_dict(row, include_result=True, connection=connection)
            result = item.get("result")
            item["has_result"] = result is not None
            item["result_summary"] = (
                {
                    "ranked_order": result["ranked_order"],
                    "focus_review_count": result["focus_review_count"],
                    "tie_group_count": len(result["tie_groups"]),
                    "weights": result["weights"],
                }
                if result
                else None
            )
            item.pop("result", None)
            items.append(item)
        return items


def get_request(
    request_id: str,
    *,
    include_result: bool = True,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    with connect(db_path or DB_PATH) as connection:
        row = fetch_request(connection, request_id)
        if row is None:
            raise BusinessError(
                "REQUEST_NOT_FOUND",
                f"排序请求 {request_id} 不存在",
                status_code=404,
                details={"request_id": request_id},
            )
        payload = request_row_to_dict(
            row,
            include_result=include_result,
            connection=connection,
        )
        slots = connection.execute(
            """
            SELECT rc.clip_id, rc.prior_rank, rc.input_order,
                   tc.action_category, tc.repetition_count, tc.duration_sec,
                   tc.manual_quality_score, tc.metadata
            FROM request_candidates rc
            JOIN training_clips tc ON tc.clip_id = rc.clip_id
            WHERE rc.request_id = ?
            ORDER BY rc.prior_rank, rc.clip_id
            """,
            (request_id,),
        ).fetchall()
        payload["candidate_summaries"] = [
            {
                "clip_id": slot["clip_id"],
                "prior_rank": slot["prior_rank"],
                "input_order": slot["input_order"],
                "action_category": slot["action_category"],
                "repetition_count": slot["repetition_count"],
                "duration_sec": slot["duration_sec"],
                "manual_quality_score": slot["manual_quality_score"],
                "metadata": json_load(slot["metadata"], {}),
            }
            for slot in slots
        ]
        return payload


def _load_request_for_ranking(
    connection: Any,
    request_id: str,
) -> tuple[Any, dict[str, Any], list[dict[str, Any]], dict[str, int]]:
    request_row = fetch_request(connection, request_id)
    if request_row is None:
        raise BusinessError(
            "REQUEST_NOT_FOUND",
            f"排序请求 {request_id} 不存在",
            status_code=404,
            details={"request_id": request_id},
        )
    profile = json_load(request_row["profile_snapshot"], None)
    if not profile:
        athlete_row = fetch_athlete(connection, request_row["athlete_id"])
        profile = (
            json_load(athlete_row["profile_snapshot"], None) if athlete_row else None
        )
    if not profile:
        raise BusinessError(
            "PROFILE_SNAPSHOT_MISSING",
            f"运动员 {request_row['athlete_id']} 缺少档案快照，无法精排",
            status_code=409,
            details={"athlete_id": request_row["athlete_id"]},
        )

    candidate_ids = json_load(request_row["candidate_ids"], [])
    prior_order = json_load(request_row["prior_rank"], [])
    prior_ranks = {
        clip_id: index + 1 for index, clip_id in enumerate(prior_order)
    }
    clips: list[dict[str, Any]] = []
    missing: list[str] = []
    for clip_id in candidate_ids:
        clip_row = fetch_clip(connection, clip_id)
        if clip_row is None:
            missing.append(clip_id)
            continue
        clips.append(clip_row_to_dict(clip_row))
    if missing:
        raise BusinessError(
            "CANDIDATE_CLIP_MISSING",
            "请求包含不存在的候选片段",
            status_code=409,
            details={"missing_clip_ids": missing},
        )
    return request_row, profile, clips, prior_ranks


def load_request_inputs(
    request_id: str,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    with connect(db_path or DB_PATH) as connection:
        request_row, profile, clips, prior_ranks = _load_request_for_ranking(
            connection, request_id
        )
        return {
            "request_id": request_row["request_id"],
            "athlete_id": request_row["athlete_id"],
            "phase": request_row["phase"],
            "weights": json_load(
                request_row["weights"], default_weights(request_row["phase"])
            ),
            "config": json_load(request_row["config"], {}),
            "profile": profile,
            "candidates": clips,
            "prior_ranks": prior_ranks,
        }


def rank_request(
    request_id: str,
    *,
    phase: str | None = None,
    weights: dict[str, float] | None = None,
    persist: bool = True,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    if phase is not None and phase not in PHASES:
        raise BusinessError(
            "INVALID_PHASE",
            f"不支持的训练周期阶段: {phase}",
            status_code=422,
            details={"allowed": list(PHASES)},
        )
    try:
        normalized_weights = (
            validate_weights(weights) if weights is not None else None
        )
    except (ValueError, TypeError) as error:
        raise BusinessError(
            "INVALID_WEIGHTS",
            str(error),
            status_code=422,
            details={"expected": {"quality": 0.4, "risk": 0.3, "profile": 0.3}},
        ) from error

    with connect(db_path or DB_PATH) as connection:
        request_row, profile, clips, prior_ranks = _load_request_for_ranking(
            connection, request_id
        )
        effective_phase = phase or request_row["phase"]
        effective_weights = normalized_weights
        if effective_weights is None:
            effective_weights = (
                default_weights(effective_phase)
                if phase is not None and phase != request_row["phase"]
                else json_load(request_row["weights"], default_weights(effective_phase))
            )
        result = rank_candidates(
            request_id=request_id,
            athlete_id=request_row["athlete_id"],
            phase=effective_phase,
            candidates=clips,
            profile=profile,
            weights=effective_weights,
            prior_ranks=prior_ranks,
        )
        if persist:
            connection.execute(
                """
                INSERT INTO ranking_results (request_id, result, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    result = excluded.result,
                    updated_at = excluded.updated_at
                """,
                (request_id, json_dump(result), _now()),
            )
            connection.commit()
        return result


def update_request(
    request_id: str,
    *,
    phase: str | None = None,
    weights: dict[str, float] | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    if phase is None and weights is None:
        raise BusinessError(
            "EMPTY_UPDATE",
            "至少需要提供 phase 或 weights",
            status_code=422,
        )
    if phase is not None and phase not in PHASES:
        raise BusinessError(
            "INVALID_PHASE",
            f"不支持的训练周期阶段: {phase}",
            status_code=422,
            details={"allowed": list(PHASES)},
        )
    with connect(db_path or DB_PATH) as connection:
        row = fetch_request(connection, request_id)
        if row is None:
            raise BusinessError(
                "REQUEST_NOT_FOUND",
                f"排序请求 {request_id} 不存在",
                status_code=404,
                details={"request_id": request_id},
            )
        effective_phase = phase or row["phase"]
        current_weights = json_load(row["weights"], default_weights(effective_phase))
        effective_weights = weights
        if effective_weights is None:
            effective_weights = (
                default_weights(effective_phase)
                if phase is not None and phase != row["phase"]
                else current_weights
            )
        try:
            effective_weights = validate_weights(effective_weights)
        except (ValueError, TypeError) as error:
            raise BusinessError(
                "INVALID_WEIGHTS",
                str(error),
                status_code=422,
                details={"expected_sum": 1.0},
            ) from error
        config = json_load(row["config"], {})
        config["default_weights"] = default_weights(effective_phase)
        config["weights_declared"] = True
        connection.execute(
            """
            UPDATE ranking_requests
            SET phase = ?, weights = ?, config = ?
            WHERE request_id = ?
            """,
            (
                effective_phase,
                json_dump(effective_weights),
                json_dump(config),
                request_id,
            ),
        )
        connection.commit()
    rank_request(
        request_id,
        weights=effective_weights,
        phase=effective_phase,
        db_path=db_path,
    )
    return get_request(request_id, db_path=db_path)


def create_request(
    *,
    athlete_id: str,
    phase: str,
    candidate_clip_ids: list[str] | None = None,
    weights: dict[str, float] | None = None,
    request_id: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    if phase not in PHASES:
        raise BusinessError(
            "INVALID_PHASE",
            f"不支持的训练周期阶段: {phase}",
            status_code=422,
            details={"allowed": list(PHASES)},
        )
    try:
        effective_weights = validate_weights(weights or default_weights(phase))
    except (ValueError, TypeError) as error:
        raise BusinessError(
            "INVALID_WEIGHTS",
            str(error),
            status_code=422,
        ) from error

    with connect(db_path or DB_PATH) as connection:
        athlete_row = fetch_athlete(connection, athlete_id)
        if athlete_row is None:
            raise BusinessError(
                "ATHLETE_NOT_FOUND",
                f"运动员 {athlete_id} 不存在",
                status_code=404,
                details={"athlete_id": athlete_id},
            )
        profile = json_load(athlete_row["profile_snapshot"], None)
        if not profile:
            raise BusinessError(
                "PROFILE_SNAPSHOT_MISSING",
                f"运动员 {athlete_id} 缺少档案快照",
                status_code=409,
                details={"athlete_id": athlete_id},
            )

        if candidate_clip_ids is None:
            rows = connection.execute(
                "SELECT clip_id FROM training_clips WHERE athlete_id = ? ORDER BY clip_id",
                (athlete_id,),
            ).fetchall()
            candidate_clip_ids = [row["clip_id"] for row in rows[:12]]
        candidate_clip_ids = list(dict.fromkeys(candidate_clip_ids))
        if not candidate_clip_ids:
            raise BusinessError(
                "EMPTY_CANDIDATE_SET",
                "候选片段集合不能为空",
                status_code=422,
            )
        clips: list[Any] = []
        missing: list[str] = []
        foreign: list[str] = []
        for clip_id in candidate_clip_ids:
            clip_row = fetch_clip(connection, clip_id)
            if clip_row is None:
                missing.append(clip_id)
            elif clip_row["athlete_id"] != athlete_id:
                foreign.append(clip_id)
            else:
                clips.append(clip_row)
        if missing:
            raise BusinessError(
                "CANDIDATE_CLIP_MISSING",
                "候选片段不存在",
                status_code=404,
                details={"missing_clip_ids": missing},
            )
        if foreign:
            raise BusinessError(
                "CANDIDATE_ATHLETE_MISMATCH",
                "候选片段与请求运动员不匹配",
                status_code=422,
                details={"clip_ids": foreign},
            )

        if request_id is None:
            prefix = f"R-{athlete_id}-{phase.upper()}-CUSTOM"
            sequence = 1
            while connection.execute(
                "SELECT 1 FROM ranking_requests WHERE request_id = ?",
                (f"{prefix}-{sequence:02d}",),
            ).fetchone():
                sequence += 1
            request_id = f"{prefix}-{sequence:02d}"
        elif fetch_request(connection, request_id) is not None:
            raise BusinessError(
                "REQUEST_EXISTS",
                f"排序请求 {request_id} 已存在",
                status_code=409,
            )

        prior_order = list(candidate_clip_ids)
        config = {
            "request_id": request_id,
            "selection_basis": ["manual_candidate_submission"],
            "phase_weight_profiles": {
                item: default_weights(item) for item in PHASES
            },
            "default_weights": default_weights(phase),
            "weights_declared": True,
            "candidate_count": len(candidate_clip_ids),
            "tie_break": "clip_id_ascending",
            "profile_snapshot_version": profile.get("snapshot_version", 1),
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
                athlete_id,
                phase,
                json_dump(effective_weights),
                json_dump(profile),
                json_dump(candidate_clip_ids),
                json_dump(prior_order),
                json_dump(config),
                _now(),
            ),
        )
        for rank, clip_id in enumerate(prior_order, start=1):
            connection.execute(
                """
                INSERT INTO request_candidates
                (request_id, clip_id, prior_rank, input_order)
                VALUES (?, ?, ?, ?)
                """,
                (request_id, clip_id, rank, rank),
            )
        connection.commit()
    rank_request(request_id, weights=effective_weights, phase=phase, db_path=db_path)
    return get_request(request_id, db_path=db_path)
