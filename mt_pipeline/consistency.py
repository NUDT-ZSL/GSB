"""Stage 5: cross-path (exact vs approx) consistency adjudication."""

from .errors import Codes, PipelineError
from .subword import (best_candidate, prune_candidates, score_candidate,
                      token_candidates)

DIVERGENCE_PRUNING_MISS = "PRUNING_MISS"
DIVERGENCE_TIE_RULE = "TIE_RULE"


def classify_divergence(token, cfg, freq):
    """For one token whose exact/approx segmentations differ, decide whether
    the divergence comes from candidate pruning or from tie-rule application."""
    full = token_candidates(token, cfg)
    pruned = prune_candidates(full, cfg.approx_top_k)
    exact_best = best_candidate(full, freq, cfg)
    approx_best = best_candidate(pruned, freq, cfg)
    if exact_best == approx_best:
        return None
    if exact_best not in pruned:
        return DIVERGENCE_PRUNING_MISS
    exact_score = score_candidate(exact_best, freq, cfg)
    approx_score = score_candidate(approx_best, freq, cfg)
    return DIVERGENCE_TIE_RULE if exact_score == approx_score else DIVERGENCE_PRUNING_MISS


def adjudicate(records, exact_seg, approx_seg, exact_kept, approx_kept, cfg, freq,
               exact_vocab_size, approx_vocab_size):
    """Compare both paths; raise E_PATH_DIVERGENCE when kept-set divergence
    makes corpus statistics incomparable."""
    exact_by_id = {p["pair_id"]: p for p in exact_seg}
    approx_by_id = {p["pair_id"]: p for p in approx_seg}
    divergences = []
    seg_same = 0
    total = 0
    for rec in records:
        pair_id = rec["pair_id"]
        e, a = exact_by_id[pair_id], approx_by_id[pair_id]
        total += 1
        if e["src_pieces"] == a["src_pieces"] and e["tgt_pieces"] == a["tgt_pieces"]:
            seg_same += 1
            continue
        dtype = None
        for side, e_pieces, a_pieces, tokens in (
            ("src", e["src_pieces"], a["src_pieces"], rec["src_tokens"]),
            ("tgt", e["tgt_pieces"], a["tgt_pieces"], rec["tgt_tokens"]),
        ):
            if e_pieces != a_pieces:
                for tok in tokens:
                    dtype = classify_divergence(tok, cfg, freq)
                    if dtype:
                        break
            if dtype:
                break
        divergences.append({
            "pair_id": pair_id,
            "type": dtype or DIVERGENCE_TIE_RULE,
        })

    kept_diff = sorted(set(exact_kept) ^ set(approx_kept))
    report = {
        "stage": "consistency",
        "pair_count": total,
        "seg_agreement_rate": (seg_same / total) if total else 1.0,
        "vocab_size_deviation": abs(exact_vocab_size - approx_vocab_size),
        "kept_set_diff": kept_diff,
        "divergences": divergences,
        "divergence_count": len(divergences),
    }
    if kept_diff:
        first = kept_diff[0]
        dtype = next((d["type"] for d in divergences if d["pair_id"] == first),
                     DIVERGENCE_PRUNING_MISS)
        raise PipelineError(
            Codes.PATH_DIVERGENCE,
            f"kept-set divergence makes statistics incomparable ({dtype})",
            pair_id=first,
            detail={"divergence_type": dtype, "report": report},
        )
    return report
