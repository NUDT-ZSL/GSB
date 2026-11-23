import pytest

from mt_pipeline.budget import (BudgetConfig, Observation, check_coverage,
                                decide_path)
from mt_pipeline.errors import Codes, PipelineError

OBS = Observation(n_pairs=1000, vocab_size=1024, avg_align_len=8.0,
                  avg_candidates=15.0, mem_headroom_bytes=268435456)
IDS = [f"p{i:06d}" for i in range(1000)]


def test_exact_path_when_budgets_ample():
    cfg = BudgetConfig(compare_budget=10**9, memory_limit=10**9)
    record, batches = decide_path(OBS, cfg, IDS)
    assert record["decision"]["path"] == "exact"
    assert record["decision"]["degradation"] == "none"
    assert sorted(pid for b in batches for pid in b) == IDS


def test_approx_path_when_exact_too_expensive():
    cfg = BudgetConfig(compare_budget=20_000, memory_limit=10**9)
    record, _ = decide_path(OBS, cfg, IDS)
    assert record["decision"]["path"] == "approx"


def test_streaming_downgrade_marks_category_and_covers_all():
    cfg = BudgetConfig(compare_budget=12_000, memory_limit=10**9)
    record, batches = decide_path(OBS, cfg, IDS)
    dec = record["decision"]
    assert dec["path"] == "streaming"
    assert dec["degradation"] == "STREAM_BATCH"
    assert dec["batch_size"] < OBS.n_pairs
    assert sorted(pid for b in batches for pid in b) == IDS


def test_decision_record_deterministic():
    cfg = BudgetConfig(compare_budget=12_000, memory_limit=10**9)
    r1, _ = decide_path(OBS, cfg, IDS)
    r2, _ = decide_path(OBS, cfg, IDS)
    assert r1 == r2


def test_compare_budget_rejection():
    cfg = BudgetConfig(compare_budget=10, memory_limit=10**9)
    with pytest.raises(PipelineError) as exc:
        decide_path(OBS, cfg, IDS)
    assert exc.value.code == Codes.COMPARE_BUDGET


def test_resource_limit_rejection():
    cfg = BudgetConfig(compare_budget=10**9, memory_limit=8)
    with pytest.raises(PipelineError) as exc:
        decide_path(OBS, cfg, IDS)
    assert exc.value.code == Codes.RESOURCE_LIMIT


def test_path_incomplete_reports_missing_pair():
    with pytest.raises(PipelineError) as exc:
        check_coverage([["p000", "p001"], ["p003"]],
                       ["p000", "p001", "p002", "p003"])
    assert exc.value.code == Codes.PATH_INCOMPLETE
    assert exc.value.pair_id == "p002"
    assert exc.value.detail["missing_pair_ids"] == ["p002"]
