from __future__ import annotations

from copy import deepcopy

from prefworkbench.consistency import compute_consistency
from prefworkbench.errors import E_BUDGET_FLOP_EXCEEDED, W_DEGRADED_GROUPING
from prefworkbench.generator import default_config, generate_pairs
from prefworkbench.neardup import compute_neardup
from prefworkbench.quality import score_pair
from prefworkbench.sampling import compute_sampling
from prefworkbench.scheduler import build_schedule


def _quality_map(pairs):
    config = default_config()["quality"]
    records = {
        pair["pair_id"]: score_pair(pair, config) for pair in pairs
    }
    return records


def test_sampling_is_deterministic_and_attributes_shortfall() -> None:
    config = default_config()
    pairs = generate_pairs(count=18)
    quality = _quality_map(pairs)
    neardup = compute_neardup(
        pairs,
        quality,
        config["neardup"],
        candidate_mode="all",
        comparison_limit=1000,
    )
    consistency = compute_consistency(pairs, config["consistency"])
    sampling = deepcopy(config["sampling"])
    for quota in sampling["quotas"]:
        quota["target"] = 20
    first = compute_sampling(
        pairs,
        quality,
        neardup,
        consistency,
        sampling,
        seed=config["seed"],
    )
    second = compute_sampling(
        pairs,
        quality,
        neardup,
        consistency,
        sampling,
        seed=config["seed"],
    )
    assert first == second
    assert len(first["selected_pair_ids"]) == len(set(first["selected_pair_ids"]))
    assert any(
        quota["shortfall_reason"] in {
            "NO_ELIGIBLE_CANDIDATES",
            "ELIGIBLE_SUPPLY_BELOW_TARGET",
        }
        for quota in first["quota_stats"]
    )
    assert sum(item["selected_count"] for item in first["quota_stats"]) == len(
        first["sequence"]
    )


def test_scheduler_is_stable_and_exposes_budget_adjudication() -> None:
    config = default_config()
    pairs = generate_pairs(count=120)
    config["scheduler"]["mac_budget"] = 125000
    config["scheduler"]["memory_budget_bytes"] = 3145728
    first = build_schedule(
        pairs,
        config["scheduler"],
        config["neardup"],
        config["priority_order"],
    )
    second = build_schedule(
        pairs,
        config["scheduler"],
        config["neardup"],
        config["priority_order"],
    )
    assert first["plan_fingerprint"] == second["plan_fingerprint"]
    assert first["selected_plan"]["rejected_pair_ids"]
    assert first["degraded_grouping"]
    assert W_DEGRADED_GROUPING in first["pair_paths"][
        first["selected_plan"]["admitted_pair_ids"][0]
    ]["code"]
    assert E_BUDGET_FLOP_EXCEEDED in first["rejected_codes"]
    assert set(first["pair_paths"]) == {pair["pair_id"] for pair in pairs}
    assert any(
        item["action"] == "degrade_grouping"
        for item in first["adjudications"]
    )
    assert any(
        item["action"] == "reject_pairs" for item in first["adjudications"]
    )

