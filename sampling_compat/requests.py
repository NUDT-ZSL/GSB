"""Inference request JSONL loading."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config_io import detect_config_version
from .errors import BusinessError, ErrorCode
from .models import InferenceRequest

DEFAULT_SAMPLING_SEED = 0


def load_requests_jsonl(path: Path) -> dict[str, InferenceRequest]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            f"unable to read request JSONL: {exc}",
            field="requests",
        ) from exc

    requests: dict[str, InferenceRequest] = {}
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"request JSONL line {line_number} is malformed",
                field="jsonl",
                details={"line": line_number, "column": exc.colno, "reason": exc.msg},
            ) from exc
        request = _parse_request(payload, line_number=line_number)
        if request.request_id in requests:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"duplicate request_id '{request.request_id}'",
                request_id=request.request_id,
                field="request_id",
                details={"line": line_number},
            )
        requests[request.request_id] = request
    if not requests:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "request JSONL contains no requests",
            field="requests",
        )
    return requests


def _parse_request(payload: Any, *, line_number: int) -> InferenceRequest:
    if not isinstance(payload, dict):
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "request JSONL line must contain an object",
            field="request",
            details={"line": line_number},
        )
    request_id_raw = payload.get("request_id")
    if not isinstance(request_id_raw, str) or not request_id_raw.strip():
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "required field 'request_id' is missing or invalid",
            field="request_id",
            details={"line": line_number},
        )
    request_id = request_id_raw.strip()

    prompt = payload.get("prompt_tokens")
    if not isinstance(prompt, list) or not all(
        isinstance(token, int) and not isinstance(token, bool) for token in prompt
    ):
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "required field 'prompt_tokens' must be an integer array",
            request_id=request_id,
            field="prompt_tokens",
            details={"line": line_number},
        )

    tokenizer_version = payload.get("tokenizer_version")
    if not isinstance(tokenizer_version, str) or not tokenizer_version.strip():
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "required field 'tokenizer_version' is missing or invalid",
            request_id=request_id,
            field="tokenizer_version",
            details={"line": line_number},
        )

    if "sampling_profile" not in payload:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "required field 'sampling_profile' is missing",
            request_id=request_id,
            field="sampling_profile",
            details={"line": line_number},
        )
    profiles = _parse_profiles(payload["sampling_profile"], request_id=request_id)

    seed_raw = payload.get("sampling_seed", DEFAULT_SAMPLING_SEED)
    if isinstance(seed_raw, bool) or not isinstance(seed_raw, (int, str)):
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "field 'sampling_seed' must be an integer",
            request_id=request_id,
            field="sampling_seed",
        )
    try:
        sampling_seed = int(seed_raw)
    except ValueError as exc:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "field 'sampling_seed' must be an integer",
            request_id=request_id,
            field="sampling_seed",
            original_value=seed_raw,
        ) from exc
    seed_source = "request.sampling_seed" if "sampling_seed" in payload else "default_zero"

    logits = payload.get("logits")
    if logits is not None and not isinstance(logits, list):
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "optional field 'logits' must be a matrix",
            request_id=request_id,
            field="logits",
        )

    return InferenceRequest(
        request_id=request_id,
        prompt_tokens=tuple(prompt),
        tokenizer_version=tokenizer_version.strip(),
        sampling_profiles=profiles,
        sampling_seed=sampling_seed,
        seed_source=seed_source,
        logits=logits,
        source_line=line_number,
    )


def _parse_profiles(raw: Any, *, request_id: str) -> dict[str, str]:
    profiles: dict[str, str] = {}
    if isinstance(raw, str):
        version = detect_config_version(raw)
        profiles[version] = raw
    elif isinstance(raw, dict):
        if "format" in raw and "body" in raw:
            version = str(raw["format"])
            body = raw["body"]
            if not isinstance(body, str):
                raise BusinessError(
                    ErrorCode.FORMAT_CORRUPT,
                    "sampling_profile.body must be raw configuration text",
                    request_id=request_id,
                    field="sampling_profile.body",
                )
            profiles[version] = body
        elif "version" in raw and any(
            key in raw for key in ("temperature", "top_k", "top_p")
        ):
            version = raw.get("version")
            if not isinstance(version, str):
                raise BusinessError(
                    ErrorCode.FORMAT_CORRUPT,
                    "sampling_profile version is invalid",
                    request_id=request_id,
                    field="sampling_profile.version",
                )
            profiles[version] = json.dumps(raw, ensure_ascii=False)
        else:
            for version, body in raw.items():
                if not isinstance(version, str) or not isinstance(body, str):
                    raise BusinessError(
                        ErrorCode.FORMAT_CORRUPT,
                        "sampling_profile must map version keys to raw text",
                        request_id=request_id,
                        field="sampling_profile",
                    )
                profiles[version] = body
    else:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "sampling_profile must be raw text or a version-to-text map",
            request_id=request_id,
            field="sampling_profile",
        )

    if not profiles:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "sampling_profile contains no configuration views",
            request_id=request_id,
            field="sampling_profile",
        )
    for version, body in profiles.items():
        if version not in {"v1", "v2", "v3"}:
            raise BusinessError(
                ErrorCode.VERSION_UNSUPPORTED,
                f"unsupported sampling profile view '{version}'",
                request_id=request_id,
                field="sampling_profile",
                version=version,
            )
        detected = detect_config_version(body)
        if detected != version:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"sampling_profile view '{version}' contains a '{detected}' header",
                request_id=request_id,
                field=f"sampling_profile.{version}.version",
                details={"declared": version, "detected": detected},
            )
    return profiles
