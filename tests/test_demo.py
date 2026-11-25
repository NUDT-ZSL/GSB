from __future__ import annotations

from pathlib import Path

from moe_router.demo import run_demo


def test_fixed_seed_end_to_end_demo(tmp_path) -> None:
    report = run_demo(
        cases_path=Path("samples/cases.jsonl"),
        workdir=tmp_path / "demo",
        reset=True,
    )
    assert report["all_consistent"] is True
    assert report["case_count"] == 6
    assert all(item["consistency"]["max_abs_diff_output"] == 0.0 for item in report["cases"])
    assert report["cache_object_integrity"]["ok"] is True
    invalidation = report["invalidation"]
    assert invalidation is not None
    plan = invalidation["plan"]
    assert plan["reused_blocks"]
    assert plan["recompute_blocks"]
    assert invalidation["consistency"]["consistent"] is True
    assert invalidation["stale_result_probe"]["code"] == "E_STALE_RESULT"
    assert invalidation["stale_result_probe"]["error_case_id"] == "moe-case-04"
    assert invalidation["stale_result_probe"]["error_block_index"] == 0
    assert invalidation["snapshot"]["v2_version"] > invalidation["snapshot"]["v1_version"]
    assert invalidation["snapshot"]["rollback_attempt_code"] in {
        "E_SNAPSHOT_ROLLBACK",
        "E_STAGE_BUSY",
    }
