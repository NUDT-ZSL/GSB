from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import (
    ACTION_CATEGORIES,
    ARTIFACTS_DIR,
    CHANNELS,
    DB_PATH,
    DEFAULT_PHASE_WEIGHTS,
    PHASES,
    SPECIALTIES,
    STATIC_DIR,
)
from app.db import connect
from app.errors import BusinessError
from app.generator import generate_dataset
from app.schemas import (
    GenerateBody,
    RankRequestBody,
    RequestCreateBody,
    RequestUpdateBody,
)
from app.service import (
    create_request,
    get_athlete,
    get_request,
    list_athlete_clips,
    list_athletes,
    list_requests,
    rank_request,
    update_request,
)
from app.verifier import verify_all, verify_request

logger = logging.getLogger("motion-ranking")


def _ensure_data() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists():
        generate_dataset(DB_PATH)
        return
    with connect(DB_PATH) as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM ranking_requests"
        ).fetchone()["count"]
    if count == 0:
        generate_dataset(DB_PATH)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _ensure_data()
    yield


app = FastAPI(
    title="运动动作精排台",
    version=__version__,
    description="可逐请求核对的训练片段精排、风险解释与一致性分析服务",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(BusinessError)
async def business_error_handler(_: Request, error: BusinessError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content=error.as_dict())


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    _: Request,
    error: RequestValidationError,
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "INVALID_REQUEST",
                "message": "请求参数校验失败",
                "details": json.loads(
                    json.dumps(error.errors(), ensure_ascii=False, default=str)
                ),
            }
        },
    )


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, error: HTTPException) -> JSONResponse:
    if isinstance(error.detail, dict) and "code" in error.detail:
        return JSONResponse(status_code=error.status_code, content={"error": error.detail})
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {
                "code": "HTTP_ERROR",
                "message": str(error.detail),
            }
        },
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(_: Request, error: Exception) -> JSONResponse:
    logger.exception("Unhandled application error", exc_info=error)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "服务内部错误，请查看本地服务日志",
            }
        },
    )


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    with connect(DB_PATH) as connection:
        counts = {
            table: connection.execute(
                f"SELECT COUNT(*) AS count FROM {table}"
            ).fetchone()["count"]
            for table in ("athletes", "training_clips", "ranking_requests")
        }
    return {
        "status": "ok",
        "version": __version__,
        "db_path": str(DB_PATH),
        "counts": counts,
    }


@app.get("/api/meta")
async def meta() -> dict[str, Any]:
    return {
        "phases": list(PHASES),
        "phase_weights": DEFAULT_PHASE_WEIGHTS,
        "action_categories": list(ACTION_CATEGORIES),
        "specialties": list(SPECIALTIES),
        "channels": list(CHANNELS),
        "score_contract": {
            "quality": "越高越好",
            "load_risk": "越高风险越大；融合时使用 100 - load_risk",
            "profile_match": "越高越好",
            "tie_break": "融合得分保留 6 位小数后，clip_id 升序",
        },
        "error_codes": [
            "ATHLETE_NOT_FOUND",
            "REQUEST_NOT_FOUND",
            "PROFILE_SNAPSHOT_MISSING",
            "CANDIDATE_CLIP_MISSING",
            "CANDIDATE_ATHLETE_MISMATCH",
            "INVALID_PHASE",
            "INVALID_WEIGHTS",
            "INVALID_REQUEST",
            "REQUEST_EXISTS",
            "EMPTY_CANDIDATE_SET",
        ],
    }


@app.get("/api/athletes")
async def athletes() -> dict[str, Any]:
    items = list_athletes()
    return {"items": items, "count": len(items)}


@app.get("/api/athletes/{athlete_id}")
async def athlete_detail(athlete_id: str) -> dict[str, Any]:
    return get_athlete(athlete_id)


@app.get("/api/athletes/{athlete_id}/clips")
async def athlete_clips(athlete_id: str) -> dict[str, Any]:
    items = list_athlete_clips(athlete_id)
    return {"items": items, "count": len(items)}


@app.get("/api/requests")
async def requests_index(
    athlete_id: str | None = Query(default=None),
    phase: str | None = Query(default=None),
) -> dict[str, Any]:
    items = list_requests(athlete_id=athlete_id, phase=phase)
    return {"items": items, "count": len(items)}


@app.post("/api/requests", status_code=201)
async def request_create(body: RequestCreateBody) -> dict[str, Any]:
    return create_request(
        athlete_id=body.athlete_id,
        phase=body.phase,
        candidate_clip_ids=body.candidate_clip_ids,
        weights=body.weights.model_dump() if body.weights else None,
        request_id=body.request_id,
    )


@app.get("/api/requests/{request_id}")
async def request_detail(request_id: str) -> dict[str, Any]:
    return get_request(request_id)


@app.patch("/api/requests/{request_id}")
async def request_update(
    request_id: str,
    body: RequestUpdateBody,
) -> dict[str, Any]:
    return update_request(
        request_id,
        phase=body.phase,
        weights=body.weights.model_dump() if body.weights else None,
    )


@app.post("/api/requests/{request_id}/rank")
async def request_rank(
    request_id: str,
    body: RankRequestBody | None = None,
) -> dict[str, Any]:
    phase = body.phase if body else None
    weights = body.weights.model_dump() if body and body.weights else None
    if phase is not None or weights is not None:
        updated = update_request(
            request_id,
            phase=phase,
            weights=weights,
        )
        return updated["result"]
    return rank_request(request_id)


@app.get("/api/requests/{request_id}/result")
async def request_result(request_id: str) -> dict[str, Any]:
    detail = get_request(request_id)
    result = detail.get("result")
    if result is None:
        result = rank_request(request_id)
    return result


@app.get("/api/requests/{request_id}/focus")
async def request_focus(request_id: str) -> dict[str, Any]:
    result = rank_request(request_id, persist=False)
    return {
        "request_id": request_id,
        "focus_review": result["focus_review"],
        "injury_uplift_candidates": result["injury_uplift_candidates"],
        "rank_drop_candidates": result["rank_drop_candidates"],
        "rank_drop_threshold": result["rank_drop_threshold"],
    }


@app.get("/api/requests/{request_id}/verification")
async def request_verification(request_id: str) -> dict[str, Any]:
    return verify_request(request_id)


@app.get("/api/consistency")
async def consistency() -> dict[str, Any]:
    path = ARTIFACTS_DIR / "verification.json"
    if path.exists():
        report = json.loads(path.read_text(encoding="utf-8"))
        return report["consistency"]
    report = verify_all()
    return report["consistency"]


@app.get("/api/verification")
async def verification() -> dict[str, Any]:
    path = ARTIFACTS_DIR / "verification.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return verify_all()


@app.post("/api/verify")
async def run_verification() -> dict[str, Any]:
    return verify_all()


@app.post("/api/admin/generate")
async def regenerate(body: GenerateBody) -> dict[str, Any]:
    if not body.confirm:
        raise BusinessError(
            "CONFIRMATION_REQUIRED",
            "重新生成会覆盖现有请求，需提交 confirm=true",
            status_code=422,
        )
    summary = generate_dataset(DB_PATH)
    for item in list_requests():
        rank_request(item["request_id"])
    report = verify_all()
    return {
        "generation": summary,
        "verification": {
            "passed": report["passed"],
            "request_count": report["request_count"],
        },
    }
