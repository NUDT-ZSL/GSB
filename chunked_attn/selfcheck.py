"""Cross-module consistency self-check (requirement 8).

Verifies, across the modules of one pipeline run:

1. the case_id sets of chunked processing and whole-segment processing are
   identical;
2. the merged per-block statistics recompute the whole-segment softmax
   denominator (within the declared tolerance);
3. the ready-block list and the produced outputs cross-check: ranges tile
   [0, seq_len) exactly and each ready block's output checksum matches the
   corresponding slice of the full output.

Any disagreement raises E_INCONSISTENT.
"""
from __future__ import annotations

import hashlib

import numpy as np

from .errors import BizError, E_INCONSISTENT
from .stability import reconstruct_denominator


def _checksum_array(a):
    return hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest()


def self_check(case_results, cfg):
    """Run the cross-module self-check over per-case result dicts.

    Each entry must provide: case_id, chunked_run, whole_run, ready_blocks
    (list of {q_block_idx, q_start, q_end, output_checksum}).
    """
    chunked_ids = sorted(r["case_id"] for r in case_results)
    whole_ids = sorted(
        r["case_id"] for r in case_results if r.get("whole_run") is not None)
    if chunked_ids != whole_ids or len(set(chunked_ids)) != len(chunked_ids):
        raise BizError(
            E_INCONSISTENT,
            "case_id sets differ between chunked and whole processing",
            stage="selfcheck",
            details={"chunked_case_ids": chunked_ids,
                     "whole_case_ids": whole_ids})
    checked = []
    for r in case_results:
        cid = r["case_id"]
        chunked, whole = r["chunked_run"], r["whole_run"]
        max_dev = 0.0
        for head in range(chunked.n_heads):
            whole_denom = whole.q_blocks[head][0].denom
            for qb in chunked.q_blocks[head]:
                recon, _ = reconstruct_denominator(qb.kv_stats)
                ref = whole_denom[qb.q_start:qb.q_end]
                if recon.size:
                    max_dev = max(max_dev,
                                  float(np.max(np.abs(recon - ref))))
        if max_dev > cfg.tolerance:
            raise BizError(
                E_INCONSISTENT,
                "case %s: merged statistics cannot recompute the whole "
                "denominator (dev %.3e)" % (cid, max_dev),
                case_id=cid, stage="selfcheck",
                details={"max_deviation": max_dev,
                         "tolerance": cfg.tolerance})
        ready = sorted(r["ready_blocks"], key=lambda b: b["q_block_idx"])
        expected_idx = list(range(len(chunked.q_blocks[0])))
        if [b["q_block_idx"] for b in ready] != expected_idx:
            raise BizError(
                E_INCONSISTENT,
                "case %s: ready block list does not cover all Q blocks"
                % cid,
                case_id=cid, stage="selfcheck",
                details={"ready_idx": [b["q_block_idx"] for b in ready],
                         "expected_idx": expected_idx})
        cursor = 0
        for b in ready:
            if b["q_start"] != cursor or b["q_end"] <= b["q_start"]:
                raise BizError(
                    E_INCONSISTENT,
                    "case %s: ready block ranges do not tile the sequence"
                    % cid,
                    case_id=cid, stage="selfcheck",
                    details={"block": b, "expected_q_start": cursor})
            cursor = b["q_end"]
            slice_sum = _checksum_array(
                chunked.output[:, b["q_start"]:b["q_end"], :])
            if slice_sum != b["output_checksum"]:
                raise BizError(
                    E_INCONSISTENT,
                    "case %s: ready block %d output checksum mismatch"
                    % (cid, b["q_block_idx"]),
                    case_id=cid, stage="selfcheck",
                    details={"q_block_idx": b["q_block_idx"],
                             "ready_checksum": b["output_checksum"],
                             "output_checksum": slice_sum})
        if cursor != chunked.seq_len:
            raise BizError(
                E_INCONSISTENT,
                "case %s: ready blocks end at %d, seq_len is %d"
                % (cid, cursor, chunked.seq_len),
                case_id=cid, stage="selfcheck",
                details={"covered": cursor, "seq_len": chunked.seq_len})
        checked.append({"case_id": cid,
                        "denominator_max_deviation": max_dev,
                        "ready_blocks_verified": len(ready)})
    return {"verdict": "OK", "cases_checked": checked}
