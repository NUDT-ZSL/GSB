from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class WeightsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quality: float
    risk: float
    profile: float


class RequestUpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: str | None = None
    weights: WeightsInput | None = None


class RankRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: str | None = None
    weights: WeightsInput | None = None


class RequestCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    athlete_id: str
    phase: str
    candidate_clip_ids: list[str] | None = None
    weights: WeightsInput | None = None
    request_id: str | None = None


class GenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool = False
