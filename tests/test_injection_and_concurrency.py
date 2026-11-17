from __future__ import annotations

from .conftest import execute


def test_injection_hits_and_reproducible_results(tmp_path, injection_path):
    first = execute(
        state_dir=tmp_path / "state-a",
        injection_path=injection_path,
        workers=3,
    )
    second = execute(
        state_dir=tmp_path / "state-b",
        injection_path=injection_path,
        workers=1,
    )
    assert first.results == second.results
    assert first.report["injection_hits"] == second.report["injection_hits"]
    assert first.report["failures"] == second.report["failures"]
    failures = {item["sample_id"]: item for item in first.report["failures"]}
    assert failures["C001"]["business_code"] == "E_EMPTY_SENTENCE"
    assert failures["C002"]["business_code"] == "E_BAD_RECORD"
    assert failures["C006"]["business_code"] == "E_NONFINITE_WEIGHT"
    c003 = next(item for item in first.results if item["sample_id"] == "C003")
    assert [slot["text"] for slot in c003["slots"]] == ["北京", "朝阳区"]
    assert c003["injection_ids"] == ["INJ-ENTITY"]
    c010 = next(item for item in first.results if item["sample_id"] == "C010")
    assert c010["confidence"] == 0.55
    assert c010["injection_ids"] == ["INJ-THRESHOLD"]
    hits = {item["injection_id"] for item in first.report["injection_hits"]}
    assert hits == {"INJ-EMPTY", "INJ-CHARS", "INJ-ENTITY", "INJ-CONFLICT", "INJ-THRESHOLD", "INJ-NONFINITE"}


def test_single_thread_and_concurrent_products_match(tmp_path):
    serial = execute(state_dir=tmp_path / "serial", workers=1)
    concurrent = execute(state_dir=tmp_path / "concurrent", workers=4)
    assert serial.results == concurrent.results
    assert serial.failures == concurrent.failures
    assert serial.report["intent_distribution"] == concurrent.report["intent_distribution"]
    assert serial.report["confidence_stats"] == concurrent.report["confidence_stats"]
    assert serial.report["task_sort_keys"] == concurrent.report["task_sort_keys"]
