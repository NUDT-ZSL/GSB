from __future__ import annotations

import copy

import pytest

from prefworkbench.canonical import content_hash
from prefworkbench.errors import BusinessError, E_RESOURCE_LIMIT
from prefworkbench.pipeline import PreferencePipeline


def _pipeline(config: dict, tmp_path, name: str = "cache") -> PreferencePipeline:
    return PreferencePipeline(config, tmp_path / name)


def test_bad_samples_are_isolated_and_reported(config: dict, samples: list[dict], tmp_path) -> None:
    broken = copy.deepcopy(samples[:8])
    broken.append(copy.deepcopy(broken[0]))
    broken.append(
        {
            "pair_id": "pair-bad-empty",
            "prompt": "prompt",
            "chosen": "",
            "rejected": "non-empty",
            "annotator_group": "g-alpha",
            "quality_notes": "bad",
        }
    )
    state = _pipeline(config, tmp_path).run(broken, use_cache=False)
    codes = {error["code"] for error in state["errors"]}
    assert "E_SAMPLE_DUPLICATE_ID" in codes
    assert "E_SAMPLE_EMPTY_FIELD" in codes
    assert "pair-0001" not in state["valid_pair_ids"]
    assert "pair-bad-empty" not in state["valid_pair_ids"]
    assert not set(state["valid_pair_ids"]) & set(state["invalid_pair_ids"])
    for tier in ("float64", "float32", "fixed"):
        selected = state["precision_results"][tier]["participation"]["selected"]
        assert "pair-0001" not in selected
        assert "pair-bad-empty" not in selected
        assert not set(selected) & set(state["invalid_pair_ids"])


def test_max_sample_resource_limit(config: dict, samples: list[dict], tmp_path) -> None:
    limited = copy.deepcopy(config)
    limited["limits"]["max_samples"] = 5
    with pytest.raises(BusinessError) as caught:
        _pipeline(limited, tmp_path).run(samples, use_cache=False)
    assert caught.value.code == E_RESOURCE_LIMIT
    assert caught.value.field == "limits.max_samples"


def test_signature_candidate_resource_limit(config: dict, samples: list[dict], tmp_path) -> None:
    limited = copy.deepcopy(config)
    limited["limits"]["max_signature_candidates"] = 1
    with pytest.raises(BusinessError) as caught:
        _pipeline(limited, tmp_path).run(samples, use_cache=False)
    assert caught.value.code == E_RESOURCE_LIMIT
    assert caught.value.stage == "near_duplicate"


def test_manifest_memory_limit(config: dict, samples: list[dict], tmp_path) -> None:
    limited = copy.deepcopy(config)
    limited["limits"]["max_manifest_bytes"] = 100
    with pytest.raises(BusinessError) as caught:
        _pipeline(limited, tmp_path).run(samples, use_cache=False)
    assert caught.value.code == E_RESOURCE_LIMIT
    assert caught.value.stage == "manifest"


def test_threshold_incremental_reuses_quality_and_matches_full(config: dict, samples: list[dict], tmp_path) -> None:
    pipeline = _pipeline(config, tmp_path)
    baseline = pipeline.run(samples, use_cache=False)
    changed = copy.deepcopy(config)
    changed["near_duplicate"]["similarity_threshold"] += 0.02
    incremental = _pipeline(changed, tmp_path).run(samples, previous=baseline, use_cache=True)
    assert incremental["execution"]["mode"] == "incremental"
    assert "quality" in incremental["execution"]["invalidation"]["reused_levels"]
    assert "dedup" in incremental["execution"]["invalidation"]["recomputed_levels"]
    full = _pipeline(changed, tmp_path).run(samples, previous=None, use_cache=False)
    assert incremental["execution"]["logic_digest"] == full["execution"]["logic_digest"]
    assert content_hash(incremental["error_spectrum"]) == content_hash(full["error_spectrum"])
    assert all(
        content_hash(incremental["precision_results"][tier]["manifest"])
        == content_hash(full["precision_results"][tier]["manifest"])
        for tier in ("float64", "float32", "fixed")
    )


def test_weight_and_sample_incremental_match_full(config: dict, samples: list[dict], tmp_path) -> None:
    baseline = _pipeline(config, tmp_path).run(samples, use_cache=False)
    changed = copy.deepcopy(config)
    changed["quality"]["weights"]["repetition"] += 0.03
    changed_samples = copy.deepcopy(samples)
    for sample in changed_samples:
        if sample["pair_id"] == "pair-0001":
            sample["chosen"] += " Independent correction with rollback evidence."
            sample["annotator_group"] = "g-beta"
    incremental = _pipeline(changed, tmp_path).run(
        changed_samples, previous=baseline, use_cache=True
    )
    invalidation = incremental["execution"]["invalidation"]
    assert "quality" in invalidation["recomputed_levels"]
    assert invalidation["affected_pair_ids"]["quality"]
    assert invalidation["changed_pair_ids"] == ["pair-0001"]
    full = _pipeline(changed, tmp_path).run(changed_samples, previous=None, use_cache=False)
    assert incremental["execution"]["logic_digest"] == full["execution"]["logic_digest"]
    assert content_hash(incremental["error_spectrum"]) == content_hash(full["error_spectrum"])
    assert incremental["precision_results"]["fixed"]["manifest"] == full["precision_results"]["fixed"]["manifest"]


def test_quota_underfill_has_reason(config: dict, samples: list[dict], tmp_path) -> None:
    state = _pipeline(config, tmp_path).run(samples, use_cache=False)
    plan = state["precision_results"]["float64"]["sampling_plan"]
    assert plan["achieved_total"] == plan["quota_results"]["total"]["actual"]
    all_quotas = plan["quota_results"]["groups"] + plan["quota_results"]["quality"]
    for quota in all_quotas:
        if quota["actual"] < quota["target"]:
            assert quota["reason"]

