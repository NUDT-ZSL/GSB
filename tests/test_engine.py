import json
import tempfile
import time

from ocr_workbench.config import ConfigBundle
from ocr_workbench.engine import Engine, stable_task_key
from ocr_workbench.errors import E_INTERACT_BUDGET_PENDING
from ocr_workbench.loader import load_directory

CFG = ConfigBundle.load("configs/detection.json", "configs/recognition.json")


def _engine(max_workers=None):
    cfg = CFG
    if max_workers is not None:
        cfg = CFG.with_overrides(
            det_overrides={"max_workers": max_workers,
                           "interact_budget_ms": 5})
    records, failures = load_directory(
        "samples", cfg.detection["image_size"])
    return Engine(records, failures, cfg, tempfile.mkdtemp())


def test_stable_task_key_is_orderable():
    keys = [stable_task_key("b", 2, "recognition"),
            stable_task_key("a", 1, "detection"),
            stable_task_key("a", 0, "recognition")]
    def triple(key):
        name, line, stage = key.split("|")
        return name, int(line), stage
    # Ordering is filename, then line index, then stage name.
    assert sorted(keys, key=triple) == [keys[2], keys[1], keys[0]]


def test_initial_response_is_pending_and_non_blocking():
    engine = _engine()
    start = time.perf_counter()
    response = engine.submit_command({"seq": 0, "op": "snapshot"})
    elapsed = (time.perf_counter() - start) * 1000
    assert response["status"] == E_INTERACT_BUDGET_PENDING
    assert elapsed <= response["budget_ms"] + 50
    results = engine.wait_idle()
    assert results and len(results) == 6
    engine.request_exit()
    assert engine.wait_exit() == 0


def test_commands_never_block_beyond_budget():
    engine = _engine()
    engine.wait_idle()
    over_budget = []
    for seq in range(1, 6):
        response = engine.submit_command({
            "seq": seq, "op": "set_param",
            "stage": "recognition",
            "param": "confidence_threshold",
            "value": 0.4 + seq * 0.05,
        })
        assert response["elapsed_ms"] <= response["budget_ms"] + 50
        over_budget.append(response["over_budget"])
    engine.wait_idle()
    engine.request_exit()
    assert engine.wait_exit() == 0
    assert all(flag is False for flag in over_budget)


def test_concurrency_does_not_change_order():
    single = _engine(max_workers=1)
    single.wait_idle()
    single_results = [
        (r.name, [line["text"] for line in r.lines])
        for r in single._latest_results
    ]
    single.request_exit()
    single.wait_exit()

    parallel = _engine(max_workers=4)
    parallel.wait_idle()
    parallel_results = [
        (r.name, [line["text"] for line in r.lines])
        for r in parallel._latest_results
    ]
    parallel.request_exit()
    parallel.wait_exit()
    assert single_results == parallel_results


def test_graceful_exit_persists_snapshot():
    engine = _engine()
    engine.wait_idle()
    engine.request_exit()
    assert engine.wait_exit() == 0
    snapshot = engine.store.load()
    assert snapshot is not None
    assert snapshot["global_version"] >= 1
    assert len(snapshot["completed_images"]) == 6
