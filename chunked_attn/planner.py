"""Joint q_block_len / kv_block_len trade-off adjudication (requirement 4).

The two block sizes constrain each other:

* smaller Q blocks  -> lower first-block latency, but more Q blocks and
  therefore more online-normalisation merges;
* smaller KV blocks -> lower per-block resident memory, but more KV blocks
  and therefore more reduction/merge overhead.

The planner enumerates candidate combinations, estimates three cost axes
(first-block latency, resident memory, merge count) and adjudicates by an
explicit, declared priority rule:

  P1. feasibility: estimated first-block latency <= latency_budget_ms;
  P2. among feasible combos: minimise estimated first-block latency;
  P3. tie: minimise resident memory bytes;
  P4. tie: minimise total merge count;
  P5. tie: fewer Q blocks, then fewer KV blocks (ascending-index order).

If no combo is feasible under P1, the combo with the smallest estimated
first-block latency is chosen and the plan is flagged infeasible so the
runtime can surface E_LATENCY_BUDGET with full evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Calibrated cost constants of the local (documented estimates; the measured
# first-block latency is observed separately at runtime). Calibration was
# done on the local sample cases (see docs/TECHNICAL_NOTES.md): each KV
# block costs a fixed loop overhead plus a per-element score/PV cost.
COST_BLOCK_OVERHEAD_MS = 1e-1      # fixed per-KV-block loop overhead
COST_SCORE_MS_PER_ELEM = 1.25e-6   # QK^T element (multiply over head_dim)
COST_PV_MS_PER_ELEM = 1.25e-6      # P*V element
BYTES_PER_F64 = 8

PRIORITY_RULE = (
    "P1 feasible(est_first_block_ms<=latency_budget_ms) > "
    "P2 min(est_first_block_ms) > "
    "P3 min(resident_memory_bytes) > "
    "P4 min(merge_count) > "
    "P5 min(n_q_blocks), min(n_kv_blocks) [index_ascending]"
)


@dataclass
class CandidateCost:
    q_block_len: int
    kv_block_len: int
    n_q_blocks: int
    n_kv_blocks: int
    est_first_block_ms: float
    resident_memory_bytes: int
    merge_count: int
    feasible: bool = False

    def to_dict(self):
        return dict(self.__dict__)


@dataclass
class BlockPlan:
    case_id: str
    seq_len: int
    head_dim: int
    n_heads: int
    causal: bool
    latency_budget_ms: float
    requested_q_block_len: int
    requested_kv_block_len: int
    q_block_len: int
    kv_block_len: int
    feasible: bool
    rationale: str
    priority_rule: str = PRIORITY_RULE
    candidates: list = field(default_factory=list)

    def to_dict(self):
        return {
            "case_id": self.case_id,
            "seq_len": self.seq_len,
            "head_dim": self.head_dim,
            "n_heads": self.n_heads,
            "causal": self.causal,
            "latency_budget_ms": self.latency_budget_ms,
            "requested_q_block_len": self.requested_q_block_len,
            "requested_kv_block_len": self.requested_kv_block_len,
            "q_block_len": self.q_block_len,
            "kv_block_len": self.kv_block_len,
            "feasible": self.feasible,
            "rationale": self.rationale,
            "priority_rule": self.priority_rule,
            "candidates": [c.to_dict() for c in self.candidates],
        }


def _block_sizes(seq_len: int) -> list[int]:
    sizes = {b for b in (8, 16, 32, 64, 128, 256) if b <= seq_len}
    sizes.add(seq_len)
    return sorted(sizes)


def _n_blocks(seq_len: int, blk: int) -> int:
    return (seq_len + blk - 1) // blk


def estimate_first_block_ms(q_blk: int, kv_blk: int, n_kv_blocks: int,
                            head_dim: int) -> float:
    """Estimated latency of the first Q block: for each KV block one score
    pass of size (q_blk x kv_blk x head_dim), one PV pass of the same size
    and a fixed per-block loop overhead."""
    per_block = COST_BLOCK_OVERHEAD_MS + q_blk * kv_blk * head_dim * (
        COST_SCORE_MS_PER_ELEM + COST_PV_MS_PER_ELEM)
    return n_kv_blocks * per_block


def estimate_candidate(seq_len: int, head_dim: int, q_blk: int, kv_blk: int,
                       budget_ms: float) -> CandidateCost:
    n_q = _n_blocks(seq_len, q_blk)
    n_kv = _n_blocks(seq_len, kv_blk)
    first_ms = estimate_first_block_ms(q_blk, kv_blk, n_kv, head_dim)
    # Resident set of one (Q block, KV block) pair: q tile, k tile, v tile,
    # score tile and the output accumulator.
    resident = BYTES_PER_F64 * (
        q_blk * head_dim + 2 * kv_blk * head_dim
        + q_blk * kv_blk + q_blk * head_dim)
    merges = n_q * n_kv
    return CandidateCost(
        q_block_len=q_blk, kv_block_len=kv_blk,
        n_q_blocks=n_q, n_kv_blocks=n_kv,
        est_first_block_ms=first_ms,
        resident_memory_bytes=resident,
        merge_count=merges,
        feasible=first_ms <= budget_ms)


def plan_blocks(record: dict) -> BlockPlan:
    """Adjudicate the joint (q_block_len, kv_block_len) choice for one case."""
    seq = record["seq_len"]
    d = record["head_dim"]
    budget = float(record["latency_budget_ms"])
    candidates = [
        estimate_candidate(seq, d, qb, kb, budget)
        for qb in _block_sizes(seq) for kb in _block_sizes(seq)
    ]
    feasible = [c for c in candidates if c.feasible]
    pool = feasible if feasible else candidates
    if feasible:
        key = lambda c: (c.est_first_block_ms, c.resident_memory_bytes,
                         c.merge_count, c.n_q_blocks, c.n_kv_blocks)
        rationale = (
            f"{len(feasible)}/{len(candidates)} candidates feasible under "
            f"budget {budget}ms; selected minimal estimated first-block "
            f"latency, then minimal resident memory, then minimal merge "
            f"count, ties by ascending block index")
    else:
        key = lambda c: (c.est_first_block_ms, c.resident_memory_bytes,
                         c.merge_count, c.n_q_blocks, c.n_kv_blocks)
        rationale = (
            f"no candidate feasible under budget {budget}ms; selected "
            f"minimal estimated first-block latency")
    chosen = min(pool, key=key)
    return BlockPlan(
        case_id=record["case_id"], seq_len=seq, head_dim=d,
        n_heads=record["n_heads"], causal=record["causal"],
        latency_budget_ms=budget,
        requested_q_block_len=record["q_block_len"],
        requested_kv_block_len=record["kv_block_len"],
        q_block_len=chosen.q_block_len,
        kv_block_len=chosen.kv_block_len,
        feasible=bool(feasible),
        rationale=rationale,
        candidates=candidates)
