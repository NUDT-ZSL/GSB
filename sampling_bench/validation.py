"""输入与配置校验链路。

校验按阶段顺序进行（``parse`` -> ``validate`` -> ``admission``），每个非法
请求产生一条结构化错误记录，不阻断其余请求的继续执行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import (
    E_DUP_REQUEST,
    E_LENGTH_INVALID,
    E_SAMPLING_INVALID,
    E_SEED_INVALID,
)
from .models import Request


@dataclass
class ValidationReport:
    valid_requests: list[Request]
    errors: list[dict[str, Any]]

    @property
    def rejected_ids(self) -> list[str]:
        return [
            err["request_id"]
            for err in self.errors
            if err.get("request_id") is not None
        ]


def _error(code: str, reason: str, request_id: str, field: str) -> dict[str, Any]:
    return {
        "code": code,
        "reason": reason,
        "request_id": request_id,
        "stage": "validate",
        "field": field,
        "detail": None,
    }


def validate_seed(value: Any, request_id: str) -> tuple[int | None, dict[str, Any] | None]:
    try:
        seed = int(str(value), 10)
    except (TypeError, ValueError):
        return None, _error(
            E_SEED_INVALID, f"seed not parseable as integer: {value!r}", request_id, "seed"
        )
    if not (-(2**63) <= seed <= 2**63 - 1):
        return None, _error(E_SEED_INVALID, "seed out of int64 range", request_id, "seed")
    return seed, None


def validate_request(raw: dict[str, Any]) -> tuple[Request | None, list[dict[str, Any]]]:
    """校验单个请求字典，返回请求与错误列表（可能同时为空/非空）。"""
    request_id = str(raw.get("request_id", "<unknown>"))
    errors: list[dict[str, Any]] = []

    seed, seed_err = validate_seed(raw.get("seed"), request_id)
    if seed_err:
        errors.append(seed_err)

    target_length = raw.get("target_length")
    try:
        target_length_int = int(target_length)
        length_ok = target_length_int > 0
    except (TypeError, ValueError):
        target_length_int = 0
        length_ok = False
    if not length_ok:
        errors.append(
            _error(
                E_LENGTH_INVALID,
                f"target_length must be positive integer, got {target_length!r}",
                request_id,
                "target_length",
            )
        )

    params_ok = True
    params_raw = raw.get("sampling_params", {})
    try:
        temperature = float(params_raw.get("temperature"))
        top_k = int(params_raw.get("top_k"))
        top_p = float(params_raw.get("top_p"))
        if not (temperature > 0.0):
            params_ok = False
            errors.append(
                _error(
                    E_SAMPLING_INVALID,
                    f"temperature must be > 0, got {temperature}",
                    request_id,
                    "sampling_params.temperature",
                )
            )
        if top_k < 0:
            params_ok = False
            errors.append(
                _error(
                    E_SAMPLING_INVALID,
                    f"top_k must be >= 0, got {top_k}",
                    request_id,
                    "sampling_params.top_k",
                )
            )
        if not (0.0 < top_p <= 1.0):
            params_ok = False
            errors.append(
                _error(
                    E_SAMPLING_INVALID,
                    f"top_p must be within (0, 1], got {top_p}",
                    request_id,
                    "sampling_params.top_p",
                )
            )
    except (TypeError, ValueError):
        params_ok = False
        errors.append(
            _error(
                E_SAMPLING_INVALID,
                "sampling_params contains non-numeric value",
                request_id,
                "sampling_params",
            )
        )

    if errors:
        return None, errors

    # 走到这里说明基础字段合法，再构造完整对象。
    from .models import SamplingParams

    request = Request(
        request_id=request_id,
        prompt_tokens=tuple(int(t) for t in raw.get("prompt_tokens", [])),
        sampling_params=SamplingParams(
            temperature=temperature, top_k=top_k, top_p=top_p
        ),
        target_length=target_length_int,
        seed=seed,  # type: ignore[arg-type]
        priority=int(raw.get("priority", 0)),
    )
    _ = params_ok
    return request, []


def validate_requests(raw_requests: list[dict[str, Any]]) -> ValidationReport:
    """校验请求清单，处理重复标识并收集所有非法请求。"""
    seen: set[str] = set()
    valid: list[Request] = []
    errors: list[dict[str, Any]] = []

    for raw in raw_requests:
        request_id = str(raw.get("request_id", "<unknown>"))
        if request_id in seen:
            errors.append(
                _error(
                    E_DUP_REQUEST,
                    "duplicate request_id detected",
                    request_id,
                    "request_id",
                )
            )
            continue
        seen.add(request_id)

        request, request_errors = validate_request(raw)
        if request_errors:
            errors.extend(request_errors)
            continue
        assert request is not None
        valid.append(request)

    return ValidationReport(valid_requests=valid, errors=errors)
