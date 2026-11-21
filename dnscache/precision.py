"""Error-bound accumulation and precision-sensitive marking.

A finding is produced when a decision ("expired?" / "refetch?" / which level
wins the min fold) depends on a remaining-lifetime difference smaller than
the configured tolerance AND the difference is within the accumulated error
bound. Each finding carries the full error source chain (domain, rtype, the
conversion steps with their discarded remainders, the comparison steps) plus
a flip analysis: would a millisecond-exact representation change the verdict?
"""
import hashlib
import json

# A millisecond-exact representation still carries <=1ms quantization per side.
MS_EXACT_BOUND_MS = 1.0


def _finding_id(domain, rtype, decision):
    raw = f"{domain}|{rtype}|{decision}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _flip_analysis(decision, diff_ms, combined_bound_ms):
    """Determine whether the decision could flip inside the error bound, and
    whether a millisecond-exact representation would settle it."""
    unreliable = abs(diff_ms) <= combined_bound_ms
    would_flip = unreliable
    settled_by_ms_exact = abs(diff_ms) > 2 * MS_EXACT_BOUND_MS
    if would_flip and settled_by_ms_exact:
        conclusion = ("flip_possible_at_current_precision; "
                      "ms_exact_representation_would_settle")
    elif would_flip:
        conclusion = "flip_possible_even_at_ms_precision"
    else:
        conclusion = "decision_firm"
    return {
        "decision": decision,
        "unreliable": unreliable,
        "would_flip": would_flip,
        "settled_by_ms_exact": settled_by_ms_exact,
        "conclusion": conclusion,
    }


def analyze_expiry(entry, now_ms, tolerance_ms, conversions):
    """Check the is-expired / should-refetch decision for one effective entry."""
    remaining_ms = entry.expiry_ms - now_ms
    findings = []
    expired = remaining_ms <= 0
    decision = "expired" if expired else "fresh"
    if abs(remaining_ms) <= max(tolerance_ms, entry.error_bound_ms):
        conv_chain = [c.to_json() for c in conversions
                      if c.domain == entry.domain and c.rtype == entry.rtype]
        flip = _flip_analysis(decision, remaining_ms, entry.error_bound_ms)
        findings.append({
            "id": _finding_id(entry.domain, entry.rtype, "expiry"),
            "domain": entry.domain,
            "rtype": entry.rtype,
            "decision_kind": "expiry",
            "decision": decision,
            "remaining_ms": remaining_ms,
            "tolerance_ms": tolerance_ms,
            "error_bound_ms": entry.error_bound_ms,
            "chain": {
                "domain": entry.domain,
                "rtype": entry.rtype,
                "level": entry.level,
                "source_version": entry.source_version,
                "original_repr": entry.original_repr,
                "arbitration": entry.arbitration,
                "conversions": conv_chain,
                "comparisons": [],
            },
            "flip": flip,
        })
    return findings


def analyze_comparisons(domain, rtype, comparisons, tolerance_ms, conversions):
    """Check min-fold comparisons whose outcome is decision-relevant."""
    findings = []
    for cmp_rec in comparisons:
        if abs(cmp_rec.diff_ms) > tolerance_ms and cmp_rec.reliable:
            continue
        conv_chain = [c.to_json() for c in conversions
                      if c.domain == domain and c.rtype == rtype]
        flip = _flip_analysis("min_select", cmp_rec.diff_ms,
                              cmp_rec.combined_bound_ms)
        findings.append({
            "id": _finding_id(domain, rtype,
                              f"min_select_{cmp_rec.order_index}"),
            "domain": domain,
            "rtype": rtype,
            "decision_kind": "min_select",
            "decision": f"winner={cmp_rec.winner_level}",
            "remaining_ms": cmp_rec.diff_ms,
            "tolerance_ms": tolerance_ms,
            "error_bound_ms": cmp_rec.combined_bound_ms,
            "chain": {
                "domain": domain,
                "rtype": rtype,
                "conversions": conv_chain,
                "comparisons": [cmp_rec.to_json()],
            },
            "flip": flip,
        })
    return findings
