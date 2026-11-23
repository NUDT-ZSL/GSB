"""Stage 2: dual subword candidate segmentation + shared vocab construction.

Exact path: full (capped) candidate enumeration per token, global piece stats.
Approx path: per-token candidate pruning to top-k (block/stream friendly).
Scoring: frequency and segment-length combination; ties broken by boundary
count descending, then piece tuple lexicographic ascending.
"""

import math
from dataclasses import dataclass

from .errors import Codes, PipelineError
from .util import checksum_obj


@dataclass(frozen=True)
class SubwordConfig:
    min_len: int = 1
    max_len: int = 4
    freq_weight: float = 0.1
    len_weight: float = 1.0
    exact_candidate_cap: int = 4096
    approx_top_k: int = 4
    block_size: int = 1000

    def validate(self):
        bad = None
        if not isinstance(self.min_len, int) or self.min_len < 1:
            bad = "min_len must be a positive integer"
        elif not isinstance(self.max_len, int) or self.max_len < self.min_len:
            bad = "max_len must be an integer >= min_len"
        elif not isinstance(self.exact_candidate_cap, int) or self.exact_candidate_cap < 1:
            bad = "exact_candidate_cap must be a positive integer"
        elif not isinstance(self.approx_top_k, int) or self.approx_top_k < 1:
            bad = "approx_top_k must be a positive integer"
        elif not isinstance(self.block_size, int) or self.block_size < 1:
            bad = "block_size must be a positive integer"
        elif not (math.isfinite(self.freq_weight) and self.freq_weight >= 0):
            bad = "freq_weight must be a finite non-negative number"
        elif not (math.isfinite(self.len_weight) and self.len_weight >= 0):
            bad = "len_weight must be a finite non-negative number"
        if bad:
            raise PipelineError(Codes.CONFIG_INVALID, f"invalid subword config: {bad}")


def token_candidates(token, cfg: SubwordConfig):
    """All splits of token into pieces of length in [min_len, max_len],
    canonically ordered by (piece count, pieces), capped deterministically."""
    n = len(token)
    results = []

    def rec(pos, pieces):
        if pos == n:
            results.append(tuple(pieces))
            return
        for length in range(cfg.min_len, cfg.max_len + 1):
            if pos + length <= n:
                rec(pos + length, pieces + [token[pos:pos + length]])

    rec(0, [])
    results.sort(key=lambda c: (len(c), c))
    return results[: cfg.exact_candidate_cap]


def prune_candidates(candidates, top_k):
    """Approx-path pruning: keep the top_k candidates by the canonical order
    (fewest pieces first, then lexicographic)."""
    return list(candidates[:top_k])


def build_piece_freq(token_stream, cfg: SubwordConfig):
    """freq[piece] = number of token occurrences containing the piece as a
    candidate piece (i.e. as a substring aligned to a valid split)."""
    freq = {}
    for token in token_stream:
        seen = set()
        for cand in token_candidates(token, cfg):
            seen.update(cand)
        for piece in seen:
            freq[piece] = freq.get(piece, 0) + 1
    return freq


def quantize(score):
    return round(score, 9)


def score_candidate(pieces, freq, cfg: SubwordConfig):
    score = 0.0
    for piece in pieces:
        score += (cfg.freq_weight * math.log1p(freq.get(piece, 0))
                  + cfg.len_weight * len(piece) ** 2)
    return quantize(score)


def best_candidate(candidates, freq, cfg: SubwordConfig):
    """Score desc, then boundary count desc, then pieces lexicographic asc."""
    best, best_key = None, None
    for cand in candidates:
        key = (-score_candidate(cand, freq, cfg), -(len(cand) - 1), cand)
        if best_key is None or key < best_key:
            best, best_key = cand, key
    return best


def segment_token(token, cfg, freq, path):
    cands = token_candidates(token, cfg)
    if path == "approx":
        cands = prune_candidates(cands, cfg.approx_top_k)
    return best_candidate(cands, freq, cfg)


def segment_pairs(records, cfg: SubwordConfig, path="exact"):
    """Segment every pair's src/tgt token sequences; build the shared vocab."""
    cfg.validate()
    if path not in ("exact", "approx"):
        raise PipelineError(Codes.CONFIG_INVALID, f"unknown segmentation path: {path}")
    freq = build_piece_freq(
        (tok for rec in records for tok in (*rec["src_tokens"], *rec["tgt_tokens"])), cfg
    )
    segmented = []
    for rec in records:
        src_pieces, tgt_pieces = [], []
        for tok in rec["src_tokens"]:
            src_pieces.extend(segment_token(tok, cfg, freq, path))
        for tok in rec["tgt_tokens"]:
            tgt_pieces.extend(segment_token(tok, cfg, freq, path))
        segmented.append((rec["pair_id"], src_pieces, tgt_pieces))

    used = set()
    for _, src_pieces, tgt_pieces in segmented:
        used.update(src_pieces)
        used.update(tgt_pieces)
    vocab = sorted(used)
    if not vocab:
        raise PipelineError(Codes.VOCAB_EMPTY, "no subword pieces produced from corpus")
    piece_to_id = {p: i for i, p in enumerate(vocab)}

    pair_results = []
    for pair_id, src_pieces, tgt_pieces in segmented:
        pair_results.append({
            "pair_id": pair_id,
            "src_pieces": src_pieces,
            "tgt_pieces": tgt_pieces,
            "src_ids": [piece_to_id[p] for p in src_pieces],
            "tgt_ids": [piece_to_id[p] for p in tgt_pieces],
        })

    summary = {
        "stage": "subword",
        "path": path,
        "vocab_size": len(vocab),
        "vocab_checksum": checksum_obj(vocab),
        "pairs": pair_results,
    }
    return summary, vocab, freq
