"""Input & config validation chain.

Every failure is a structured business error carrying job_id, the
offending field and the DAG node the failure is attributed to. A job
that fails validation is excluded from execution but never silently
drops other jobs.
"""
from __future__ import annotations

from .config import SUPPORTED_ACC_DTYPES
from .errors import Code, error_dict
from .nodes import QUANT_INPUT_PREP, REDUCE_DEQUANT, WEIGHT_QUANT
from .quant import INT8_MAX, INT8_MIN
from .sample_gen import JOB_FIELDS


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def validate_job(job, registry=None):
    """Validate one job manifest. Returns a list of structured errors."""
    errs = []
    jid = job.get("job_id", "<missing>")

    def err(code, msg, field, node=QUANT_INPUT_PREP, **kw):
        errs.append(error_dict(code, msg, job_id=jid, field=field,
                               node=node, **kw))

    if not isinstance(job.get("job_id"), str) or not job.get("job_id"):
        errs.append(error_dict(Code.E_BAD_MANIFEST, "job_id missing",
                               job_id="<missing>", field="job_id",
                               node=QUANT_INPUT_PREP))
        return errs
    for f in JOB_FIELDS:
        if f not in job:
            err(Code.E_BAD_MANIFEST, f"required field '{f}' missing", f)
    if errs:
        return errs

    ish = job["input_shape"]
    if (not isinstance(ish, list) or len(ish) != 4
            or any(not _is_int(d) or d <= 0 for d in ish)):
        err(Code.E_SHAPE_MISMATCH,
            "input_shape must be 4 positive ints (N,C,H,W)", "input_shape",
            actual=ish)
    ksh = job["kernel_shape"]
    if (not isinstance(ksh, list) or len(ksh) != 4
            or any(not _is_int(d) or d <= 0 for d in ksh)):
        err(Code.E_SHAPE_MISMATCH,
            "kernel_shape must be 4 positive ints (K,C,Kh,Kw)",
            "kernel_shape", actual=ksh)
    if not errs and ksh[1] != ish[1]:
        err(Code.E_SHAPE_MISMATCH,
            f"kernel C ({ksh[1]}) != input C ({ish[1]})", "kernel_shape",
            input_c=ish[1], kernel_c=ksh[1])
    if not errs:
        n, c, h, w = ish
        k, _, kh, kw = ksh
        pad = job["padding"] if _is_int(job["padding"]) else 0
        if kh > h + 2 * pad or kw > w + 2 * pad:
            err(Code.E_SHAPE_MISMATCH,
                "kernel exceeds padded input spatial dims", "kernel_shape",
                input_hw=[h, w], kernel_hw=[kh, kw], padding=pad)

    if not _is_int(job["stride"]) or job["stride"] <= 0:
        err(Code.E_BAD_MANIFEST, "stride must be a positive int", "stride")
    if not _is_int(job["padding"]) or job["padding"] < 0:
        err(Code.E_BAD_MANIFEST, "padding must be a non-negative int",
            "padding")
    if not _is_int(job["seed"]):
        err(Code.E_BAD_MANIFEST, "seed must be an int", "seed")
    if not _is_int(job["block_size"]) or job["block_size"] <= 0:
        err(Code.E_BAD_MANIFEST, "block_size must be a positive int",
            "block_size", node="blocked_mac")

    qs = job["quant_scale"]
    if not isinstance(qs, (int, float)) or isinstance(qs, bool) or qs <= 0:
        err(Code.E_INVALID_QUANT_SCALE,
            "quant_scale must be a positive number", "quant_scale",
            actual=qs)
    zp = job["zero_point"]
    if not _is_int(zp) or not (INT8_MIN <= zp <= INT8_MAX):
        err(Code.E_INVALID_ZERO_POINT,
            f"zero_point must be in [{INT8_MIN},{INT8_MAX}]", "zero_point",
            actual=zp)
    if job["acc_dtype"] not in SUPPORTED_ACC_DTYPES:
        err(Code.E_UNSUPPORTED_ACC_DTYPE,
            f"acc_dtype must be one of {list(SUPPORTED_ACC_DTYPES)}",
            "acc_dtype", node="blocked_mac", actual=job["acc_dtype"])

    if registry is not None:
        cur = registry.get(jid)
        if cur is not None:
            if _is_int(job["weight_revision"]) \
                    and job["weight_revision"] < cur["weight_revision"]:
                err(Code.E_REVISION_ROLLBACK,
                    "weight_revision rolls back registered version",
                    "weight_revision", node=WEIGHT_QUANT,
                    current=cur["weight_revision"],
                    arrived=job["weight_revision"])
            if _is_int(job["calib_revision"]) \
                    and job["calib_revision"] < cur["calib_revision"]:
                err(Code.E_REVISION_ROLLBACK,
                    "calib_revision rolls back registered version",
                    "calib_revision", node=REDUCE_DEQUANT,
                    current=cur["calib_revision"],
                    arrived=job["calib_revision"])
    return errs


def validate_jobs(jobs, registry=None):
    """Validate a job list. Returns (valid_jobs, errors)."""
    valid, errs, seen = [], [], set()
    for job in jobs:
        jid = job.get("job_id", "<missing>")
        if jid in seen:
            errs.append(error_dict(
                Code.E_DUPLICATE_JOB_ID, "duplicate job_id in manifest",
                job_id=jid, field="job_id", node=QUANT_INPUT_PREP))
            continue
        seen.add(jid)
        job_errs = validate_job(job, registry)
        if job_errs:
            errs.extend(job_errs)
        else:
            valid.append(job)
    return valid, errs


def validate_param_row(row, kind, registry=None):
    """Validate a weight_params / calib_stats update row."""
    errs = []
    jid = row.get("job_id", "<missing>")
    if kind == "weight":
        rev_f, scale_f, zp_f = "weight_revision", "weight_scale", \
            "weight_zero_point"
        node = WEIGHT_QUANT
    else:
        rev_f, scale_f, zp_f = "calib_revision", "out_scale", "out_zero_point"
        node = REDUCE_DEQUANT
    for f in ("job_id", rev_f, scale_f, zp_f):
        if f not in row:
            errs.append(error_dict(Code.E_BAD_MANIFEST,
                                   f"required field '{f}' missing",
                                   job_id=jid, field=f, node=node))
    if errs:
        return errs
    if not isinstance(row[scale_f], (int, float)) or row[scale_f] <= 0:
        errs.append(error_dict(Code.E_INVALID_QUANT_SCALE,
                               "scale must be a positive number",
                               job_id=jid, field=scale_f, node=node,
                               actual=row[scale_f]))
    zp = row[zp_f]
    if not _is_int(zp) or not (INT8_MIN <= zp <= INT8_MAX):
        errs.append(error_dict(Code.E_INVALID_ZERO_POINT,
                               f"zero_point must be in [{INT8_MIN},{INT8_MAX}]",
                               job_id=jid, field=zp_f, node=node, actual=zp))
    if registry is not None:
        cur = registry.get(jid)
        if cur is None:
            errs.append(error_dict(Code.E_BAD_MANIFEST,
                                   "update for unknown job_id",
                                   job_id=jid, field="job_id", node=node))
        elif _is_int(row[rev_f]) and row[rev_f] <= cur[rev_f]:
            errs.append(error_dict(
                Code.E_REVISION_ROLLBACK,
                f"{rev_f} must be strictly greater than registered version",
                job_id=jid, field=rev_f, node=node,
                current=cur[rev_f], arrived=row[rev_f]))
    return errs
