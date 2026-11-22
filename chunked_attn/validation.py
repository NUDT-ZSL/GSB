"""Input and configuration validation chain (requirement 7).

Every failure raises a structured BizError with the business code, the
offending field, the case_id and the pipeline stage; sample-level failures
are never silently dropped.
"""
from __future__ import annotations

from .errors import (BizError, E_DIM_MISMATCH, E_DUPLICATE_CASE_ID,
                     E_HEADS_MISMATCH, E_INVALID_BLOCK, E_INVALID_CAUSAL,
                     E_INVALID_DTYPE, E_MISSING_FIELD)

REQUIRED_FIELDS = (
    "case_id", "seq_len", "head_dim", "n_heads",
    "q_block_len", "kv_block_len", "causal", "dtype", "seed",
    "latency_budget_ms",
)

DTYPES = ("float32", "float64")


def validate_record(rec):
    """Validate one manifest record (stage: manifest_validate)."""
    stage = "manifest_validate"
    cid = rec.get("case_id")
    for f in REQUIRED_FIELDS:
        if f not in rec:
            raise BizError(E_MISSING_FIELD,
                           "missing required field '%s'" % f,
                           field=f, case_id=cid, stage=stage)
    for f in ("seq_len", "head_dim", "n_heads"):
        v = rec[f]
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise BizError(E_DIM_MISMATCH,
                           "field '%s' must be a positive integer, got %r"
                           % (f, v), field=f, case_id=cid, stage=stage)
    for f in ("q_block_len", "kv_block_len"):
        v = rec[f]
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise BizError(E_INVALID_BLOCK,
                           "field '%s' must be a positive integer, got %r"
                           % (f, v), field=f, case_id=cid, stage=stage)
    if not isinstance(rec["causal"], bool):
        raise BizError(E_INVALID_CAUSAL,
                       "field 'causal' must be a boolean, got %r"
                       % (rec["causal"],),
                       field="causal", case_id=cid, stage=stage)
    if rec["dtype"] not in DTYPES:
        raise BizError(E_INVALID_DTYPE,
                       "field 'dtype' must be one of %s, got %r"
                       % (DTYPES, rec["dtype"]),
                       field="dtype", case_id=cid, stage=stage)
    if not isinstance(rec["seed"], int) or isinstance(rec["seed"], bool):
        raise BizError(E_MISSING_FIELD,
                       "field 'seed' must be an integer, got %r"
                       % (rec["seed"],),
                       field="seed", case_id=cid, stage=stage)
    budget = rec["latency_budget_ms"]
    if not isinstance(budget, (int, float)) or isinstance(budget, bool) \
            or budget <= 0:
        raise BizError(E_INVALID_BLOCK,
                       "field 'latency_budget_ms' must be positive, got %r"
                       % (budget,),
                       field="latency_budget_ms", case_id=cid, stage=stage)
    return rec


def validate_manifest(records):
    """Validate the whole manifest, rejecting duplicate case_ids."""
    seen = set()
    out = []
    for rec in records:
        validate_record(rec)
        cid = rec["case_id"]
        if cid in seen:
            raise BizError(E_DUPLICATE_CASE_ID,
                           "duplicate case_id '%s'" % cid,
                           field="case_id", case_id=cid,
                           stage="manifest_validate")
        seen.add(cid)
        out.append(rec)
    return out


def validate_arrays(rec, q, k, v):
    """Validate array shapes against the manifest (stage: array_validate)."""
    stage = "array_validate"
    cid = rec["case_id"]
    expected = (rec["n_heads"], rec["seq_len"], rec["head_dim"])
    for name, arr in (("q", q), ("k", k), ("v", v)):
        if arr.ndim != 3:
            raise BizError(E_DIM_MISMATCH,
                           "array '%s' must be 3-D (n_heads, seq_len, "
                           "head_dim), got shape %s" % (name, arr.shape),
                           field=name, case_id=cid, stage=stage,
                           details={"expected": list(expected),
                                    "actual": list(arr.shape)})
        if arr.shape[0] != rec["n_heads"]:
            raise BizError(E_HEADS_MISMATCH,
                           "array '%s' heads %d != n_heads %d"
                           % (name, arr.shape[0], rec["n_heads"]),
                           field="n_heads", case_id=cid, stage=stage,
                           details={"expected": list(expected),
                                    "actual": list(arr.shape)})
        if tuple(arr.shape) != expected:
            raise BizError(E_DIM_MISMATCH,
                           "array '%s' shape %s != expected %s"
                           % (name, tuple(arr.shape), expected),
                           field=name, case_id=cid, stage=stage,
                           details={"expected": list(expected),
                                    "actual": list(arr.shape)})
