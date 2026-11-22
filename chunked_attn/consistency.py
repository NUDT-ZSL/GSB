"""Byte-exact consistency adjudication (requirement 3).

The chunked run and the whole-segment run share the identical KV merge
sequence (same kv_block_len, ascending KV block order), so their outputs
must be bit-identical. Adjudication compares raw bytes; the first deviation
is reported in ascending (head, row, col) index order together with its Q
block and the merge deviation.
"""
from __future__ import annotations

import numpy as np

from .errors import BizError, E_BLOCK_MISMATCH


def adjudicate(chunked_run, whole_run, case_id=None):
    """Compare chunked concatenation against the whole-segment output.

    Returns a verdict dict on success; raises BizError(E_BLOCK_MISMATCH)
    with the first deviation position, its Q block index and the merge
    deviation otherwise.
    """
    a = np.ascontiguousarray(chunked_run.output)
    b = np.ascontiguousarray(whole_run.output)
    if a.shape != b.shape:
        raise BizError(
            E_BLOCK_MISMATCH,
            "shape mismatch: chunked %s vs whole %s" % (a.shape, b.shape),
            case_id=case_id, stage="consistency",
            details={"chunked_shape": list(a.shape),
                     "whole_shape": list(b.shape)})
    if a.tobytes() == b.tobytes():
        return {
            "verdict": "OK",
            "byte_identical": True,
            "elements": int(a.size),
            "output_checksum": chunked_run.output_checksum(),
        }
    neq = a.view(np.uint64) != b.view(np.uint64)
    flat = int(np.argmax(neq))
    head, row, col = np.unravel_index(flat, a.shape)
    q_block_idx = int(row) // chunked_run.q_block_len
    abs_diff = np.abs(a - b)
    raise BizError(
        E_BLOCK_MISMATCH,
        "chunked/whole outputs differ at head=%d row=%d col=%d"
        % (head, row, col),
        case_id=case_id, stage="consistency",
        details={
            "first_deviation": {"head": int(head), "row": int(row),
                                "col": int(col)},
            "q_block_idx": int(q_block_idx),
            "deviation_at_first": float(abs_diff[head, row, col]),
            "max_merge_deviation": float(abs_diff.max()),
            "mismatch_count": int(neq.sum()),
        })
