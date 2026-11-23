"""Stages 4+6: scale-adaptive path selection and dual-budget admission."""

from dataclasses import dataclass

from .errors import Codes, PipelineError
from .util import checksum_obj


@dataclass(frozen=True)
class Observation:
    n_pairs: int
    vocab_size: int
    avg_align_len: float
    avg_candidates: float
    mem_headroom_bytes: int


@dataclass(frozen=True)
class BudgetConfig:
    compare_budget: int = 50_000_000
    memory_limit: int = 268_435_456
    approx_top_k: int = 4
    bytes_per_vocab_entry: int = 32
    bytes_per_align_feature: int = 16


def estimate(obs: Observation, cfg: BudgetConfig):
    n = obs.n_pairs
    a = obs.avg_align_len
    c = obs.avg_candidates
    vocab_bytes = obs.vocab_size * cfg.bytes_per_vocab_entry
    return {
        "exact_compares": int(n * a * a + n * c + n * (n - 1) / 2),
        "approx_compares": int(n * a + n * cfg.approx_top_k + n),
        "exact_memory": vocab_bytes + int(n * a * cfg.bytes_per_align_feature),
        "approx_memory": vocab_bytes + int(min(n, 1024) * a * cfg.bytes_per_align_feature),
        "vocab_resident_bytes": vocab_bytes,
    }


def admit(obs: Observation, cfg: BudgetConfig):
    """Dual-budget admission gate: rejects when even the cheapest path
    cannot process a single pair within the comparison budget, or when the
    resident vocab alone exceeds the memory limit."""
    est = estimate(obs, cfg)
    if per_pair_min_compares(obs, cfg) > cfg.compare_budget:
        raise PipelineError(
            Codes.COMPARE_BUDGET,
            f"minimum per-pair compare estimate "
            f"{per_pair_min_compares(obs, cfg)} exceeds budget {cfg.compare_budget}",
            detail={"estimates": est},
        )
    if est["vocab_resident_bytes"] > cfg.memory_limit:
        raise PipelineError(
            Codes.RESOURCE_LIMIT,
            f"resident vocab {est['vocab_resident_bytes']} bytes exceeds memory "
            f"limit {cfg.memory_limit}",
            detail={"estimates": est},
        )
    return est


def per_pair_min_compares(obs: Observation, cfg: BudgetConfig):
    return int(obs.avg_align_len) + cfg.approx_top_k + 1


def decide_path(obs: Observation, cfg: BudgetConfig, pair_ids):
    """Choose exact / approx / streaming; emit a deterministic decision record
    whose batches cover every input pair_id."""
    est = admit(obs, cfg)
    n = obs.n_pairs
    if est["exact_compares"] <= cfg.compare_budget and est["exact_memory"] <= cfg.memory_limit:
        path, degradation = "exact", "none"
        batch_size = n
    elif est["approx_compares"] <= cfg.compare_budget and est["approx_memory"] <= cfg.memory_limit:
        path, degradation = "approx", "none"
        batch_size = n
    else:
        path, degradation = "streaming", "STREAM_BATCH"
        per_pair = max(per_pair_min_compares(obs, cfg), 1)
        batch_size = max(1, min(n, cfg.compare_budget // per_pair))
        mem_per_pair = max(int(obs.avg_align_len * cfg.bytes_per_align_feature), 1)
        mem_batch = max(1, (cfg.memory_limit - est["vocab_resident_bytes"]) // mem_per_pair)
        batch_size = max(1, min(batch_size, mem_batch))

    batches = [sorted(pair_ids)[i:i + batch_size] for i in range(0, n, batch_size)] if n else []
    check_coverage(batches, pair_ids)
    record = {
        "stage": "selector",
        "observations": {
            "n_pairs": obs.n_pairs,
            "vocab_size": obs.vocab_size,
            "avg_align_len": obs.avg_align_len,
            "avg_candidates": obs.avg_candidates,
            "mem_headroom_bytes": obs.mem_headroom_bytes,
        },
        "estimates": est,
        "decision": {
            "path": path,
            "degradation": degradation,
            "batch_size": batch_size,
            "batch_count": len(batches),
            "first_pair_id": sorted(pair_ids)[0] if pair_ids else None,
            "last_pair_id": sorted(pair_ids)[-1] if pair_ids else None,
        },
    }
    record["record_checksum"] = checksum_obj(record)
    return record, batches


def check_coverage(batches, pair_ids):
    covered = set()
    for batch in batches:
        covered.update(batch)
    missing = sorted(set(pair_ids) - covered)
    if missing:
        raise PipelineError(
            Codes.PATH_INCOMPLETE,
            f"selected path misses {len(missing)} pair(s)",
            pair_id=missing[0],
            detail={"missing_pair_ids": missing},
        )
