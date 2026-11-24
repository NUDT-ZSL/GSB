"""Per-access boundary judgement. Integer offsets only, never floats."""

from __future__ import annotations

from . import codes


def judge_access(rec, regions):
    """Judge one normalized TraceRecord. Returns a violation code or None.

    Precedence (deterministic): E_AFTER_LIFE > E_ANCESTOR_OVER > E_BOUND_OVER.
    """
    decl = regions.get(rec.region_id)
    if decl is None:
        return None  # unknown / tombstoned region: handled at import level
    if decl.life_end_ms is not None and rec.ts_ms > decl.life_end_ms:
        return codes.E_AFTER_LIFE
    lo = rec.base_offset
    hi = rec.base_offset + rec.access_size
    # Ancestor intersection; without a parent the arena itself bounds access.
    anc_lo, anc_hi = 0, decl.arena_size
    node = decl.parent_id
    while node is not None and node in regions:
        anc = regions[node]
        anc_lo = max(anc_lo, anc.start)
        anc_hi = min(anc_hi, anc.end)
        node = anc.parent_id
    if lo < anc_lo or hi > anc_hi:
        return codes.E_ANCESTOR_OVER
    if lo < decl.start or hi > decl.end:
        return codes.E_BOUND_OVER
    return None
