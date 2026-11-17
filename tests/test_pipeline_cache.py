import tempfile

from ocr_workbench.config import ConfigBundle
from ocr_workbench.loader import load_directory
from ocr_workbench.pipeline import StageCache, run_image

CFG = ConfigBundle.load("configs/detection.json", "configs/recognition.json")


def _records():
    records, _ = load_directory(
        "samples", CFG.detection["image_size"])
    return records[:2]


def test_full_recompute_then_full_reuse():
    records = _records()
    cache = StageCache()
    first = run_image(records[0], CFG, cache)
    assert set(first.recomputed_stages) == {
        "detection", "aggregation", "segmentation", "recognition"}
    second = run_image(records[0], CFG, cache)
    assert second.recomputed_stages == []
    assert set(second.reused_stages) == {
        "detection", "aggregation", "segmentation", "recognition"}


def test_recognition_param_only_invalidates_downstream():
    records = _records()
    cache = StageCache()
    run_image(records[0], CFG, cache)
    changed = CFG.with_overrides(rec_overrides={"confidence_threshold": 0.7})
    result = run_image(records[0], changed, cache)
    assert result.recomputed_stages == ["recognition"]
    assert set(result.reused_stages) == {
        "detection", "aggregation", "segmentation"}


def test_metric_change_keeps_segmentation():
    records = _records()
    cache = StageCache()
    run_image(records[0], CFG, cache)
    changed = CFG.with_overrides(rec_overrides={"metric": "xor"})
    result = run_image(records[0], changed, cache)
    assert result.recomputed_stages == ["recognition"]


def test_segmentation_param_invalidates_segmentation_and_recognition():
    records = _records()
    cache = StageCache()
    run_image(records[0], CFG, cache)
    changed = CFG.with_overrides(rec_overrides={"min_char_width": 9})
    result = run_image(records[0], changed, cache)
    assert set(result.recomputed_stages) == {
        "segmentation", "recognition"}
    assert set(result.reused_stages) == {"detection", "aggregation"}


def test_detection_param_invalidates_all_stages():
    records = _records()
    cache = StageCache()
    run_image(records[0], CFG, cache)
    changed = CFG.with_overrides(det_overrides={"binary_threshold": 150})
    result = run_image(records[0], changed, cache)
    assert set(result.recomputed_stages) == {
        "detection", "aggregation", "segmentation", "recognition"}


def test_returning_to_original_params_restores_reuse():
    records = _records()
    cache = StageCache()
    run_image(records[0], CFG, cache)
    changed = CFG.with_overrides(det_overrides={"binary_threshold": 150})
    run_image(records[0], changed, cache)
    restored = run_image(records[0], CFG, cache)
    assert restored.recomputed_stages == []
    assert len(restored.reused_stages) == 4
