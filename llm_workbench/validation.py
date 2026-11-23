"""Input & config validation chain with stage attribution.

Every rejected item produces a structured error record (never silently
dropped) carrying the business error code, the offending field, the
request_id and the pipeline stage. Invalid requests are quarantined and
cannot pollute other requests' results.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .errors import ErrorCode, WorkbenchError
from .sampling import SamplingParams

STAGE_INGEST = "ingest"
STAGE_VALIDATE = "validate"
STAGE_VOCAB = "vocab_load"
STAGE_CONFIG = "config_validate"


@dataclass
class Request:
    request_id: str
    prompt_tokens: int
    sampling_params: SamplingParams
    target_len: int
    seed: int
    priority: int = 0

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "prompt_tokens": self.prompt_tokens,
            "sampling_params": self.sampling_params.to_dict(),
            "target_len": self.target_len,
            "seed": self.seed,
            "priority": self.priority,
        }


@dataclass
class ValidationOutcome:
    requests: list = field(default_factory=list)  # list[Request], input order
    errors: list = field(default_factory=list)    # list[WorkbenchError]


def _err(code: str, message: str, stage: str, field_name: str, request_id) -> WorkbenchError:
    return WorkbenchError(
        code, message, stage=stage, field=field_name, request_id=request_id
    )


def validate_request_dict(raw: dict, seen_ids: set) -> Request:
    """Validate one JSONL record; raises WorkbenchError with attribution."""
    rid = raw.get("request_id")
    if rid is None or not isinstance(rid, str) or not rid:
        raise _err(ErrorCode.E_DUP_REQUEST, "missing or non-string request_id",
                   STAGE_VALIDATE, "request_id", rid)
    if rid in seen_ids:
        raise _err(ErrorCode.E_DUP_REQUEST, f"duplicate request_id {rid!r}",
                   STAGE_VALIDATE, "request_id", rid)

    sp = raw.get("sampling_params")
    if not isinstance(sp, dict):
        raise _err(ErrorCode.E_SAMPLING_INVALID, "sampling_params must be an object",
                   STAGE_VALIDATE, "sampling_params", rid)
    temp = sp.get("temperature", 1.0)
    if not isinstance(temp, (int, float)) or isinstance(temp, bool) or not (temp > 0):
        raise _err(ErrorCode.E_SAMPLING_INVALID,
                   f"temperature must be > 0, got {temp!r}",
                   STAGE_VALIDATE, "sampling_params.temperature", rid)
    top_k = sp.get("top_k")
    if top_k is not None and (not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1):
        raise _err(ErrorCode.E_SAMPLING_INVALID,
                   f"top_k must be a positive integer or null, got {top_k!r}",
                   STAGE_VALIDATE, "sampling_params.top_k", rid)
    top_p = sp.get("top_p", 1.0)
    if not isinstance(top_p, (int, float)) or isinstance(top_p, bool) or not (0 < top_p <= 1):
        raise _err(ErrorCode.E_SAMPLING_INVALID,
                   f"top_p must be in (0, 1], got {top_p!r}",
                   STAGE_VALIDATE, "sampling_params.top_p", rid)

    target_len = raw.get("target_len")
    if not isinstance(target_len, int) or isinstance(target_len, bool) or target_len <= 0:
        raise _err(ErrorCode.E_LENGTH_INVALID,
                   f"target_len must be a positive integer, got {target_len!r}",
                   STAGE_VALIDATE, "target_len", rid)

    seed = raw.get("seed")
    if isinstance(seed, bool):
        seed_ok = False
    elif isinstance(seed, int):
        seed_ok = True
    elif isinstance(seed, str):
        try:
            seed = int(seed, 10)
            seed_ok = True
        except ValueError:
            seed_ok = False
    else:
        seed_ok = False
    if not seed_ok:
        raise _err(ErrorCode.E_SEED_INVALID,
                   f"seed is not parseable as an integer: {raw.get('seed')!r}",
                   STAGE_VALIDATE, "seed", rid)

    prompt_tokens = raw.get("prompt_tokens", 0)
    if not isinstance(prompt_tokens, int) or isinstance(prompt_tokens, bool) or prompt_tokens < 0:
        raise _err(ErrorCode.E_SAMPLING_INVALID,
                   f"prompt_tokens must be a non-negative integer, got {prompt_tokens!r}",
                   STAGE_VALIDATE, "prompt_tokens", rid)

    priority = raw.get("priority", 0)
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise _err(ErrorCode.E_SAMPLING_INVALID,
                   f"priority must be an integer, got {priority!r}",
                   STAGE_VALIDATE, "priority", rid)

    return Request(
        request_id=rid,
        prompt_tokens=prompt_tokens,
        sampling_params=SamplingParams(temperature=float(temp), top_k=top_k, top_p=float(top_p)),
        target_len=target_len,
        seed=seed,
        priority=priority,
    )


def load_tasks_jsonl(path: str) -> ValidationOutcome:
    """Parse + validate the task manifest. One bad line never drops others."""
    outcome = ValidationOutcome()
    seen: set = set()
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                outcome.errors.append(WorkbenchError(
                    ErrorCode.E_SAMPLING_INVALID,
                    f"line {lineno}: invalid JSON: {exc}",
                    stage=STAGE_INGEST, field="jsonl", request_id=None,
                ))
                continue
            try:
                req = validate_request_dict(raw, seen)
            except WorkbenchError as err:
                outcome.errors.append(err)
                continue
            seen.add(req.request_id)
            outcome.requests.append(req)
    return outcome


def load_vocab(path: str) -> dict:
    """Load vocab mapping; missing file or missing version -> E_VOCAB_MISSING."""
    if not os.path.exists(path):
        raise WorkbenchError(
            ErrorCode.E_VOCAB_MISSING, f"vocab file not found: {path}",
            stage=STAGE_VOCAB, field="vocab_path", request_id=None,
        )
    with open(path, "r", encoding="utf-8") as fh:
        vocab = json.load(fh)
    if "version" not in vocab:
        raise WorkbenchError(
            ErrorCode.E_VOCAB_MISSING, "vocab file has no 'version' field",
            stage=STAGE_VOCAB, field="version", request_id=None,
        )
    if "tokens" not in vocab or not isinstance(vocab["tokens"], list) or not vocab["tokens"]:
        raise WorkbenchError(
            ErrorCode.E_VOCAB_MISSING, "vocab file has no usable 'tokens' list",
            stage=STAGE_VOCAB, field="tokens", request_id=None,
        )
    return vocab


DEFAULT_CONFIG = {
    "max_slots": 4,            # concurrent worker slots per wave
    "batch_size": 4,           # requests per dispatch chunk
    "max_waves": 8,            # hard cap on sequential waves (slot budget)
    "step_budget": 100000,     # total sampling steps across admitted requests
    "kv_budget": 1000000,      # max resident KV entries per wave
    "candidate_budget": 128,   # per-step candidate count estimate ceiling
    "topp_floor": 0.5,         # tightened top_p when arbitration says so
    # explicit arbitration priority among the three contested resources
    "tradeoff_priority": ["memory", "progress", "quality"],
}

_TRADEOFF_DIMS = ("progress", "memory", "quality")


def validate_config(cfg: dict) -> dict:
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg or {})
    for key in ("max_slots", "batch_size", "max_waves"):
        if not isinstance(merged[key], int) or merged[key] < 1:
            raise WorkbenchError(
                ErrorCode.E_SAMPLING_INVALID, f"config {key} must be a positive integer",
                stage=STAGE_CONFIG, field=key, request_id=None,
            )
    for key in ("step_budget", "kv_budget", "candidate_budget"):
        if not isinstance(merged[key], int) or merged[key] < 0:
            raise WorkbenchError(
                ErrorCode.E_SAMPLING_INVALID, f"config {key} must be a non-negative integer",
                stage=STAGE_CONFIG, field=key, request_id=None,
            )
    if not (0 < merged["topp_floor"] <= 1):
        raise WorkbenchError(
            ErrorCode.E_SAMPLING_INVALID, "config topp_floor must be in (0, 1]",
            stage=STAGE_CONFIG, field="topp_floor", request_id=None,
        )
    tp = merged["tradeoff_priority"]
    if sorted(tp) != sorted(_TRADEOFF_DIMS):
        raise WorkbenchError(
            ErrorCode.E_SAMPLING_INVALID,
            f"tradeoff_priority must be a permutation of {_TRADEOFF_DIMS}",
            stage=STAGE_CONFIG, field="tradeoff_priority", request_id=None,
        )
    return merged
