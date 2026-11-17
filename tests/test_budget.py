import tempfile
import time

from ocr_workbench.config import ConfigBundle
from ocr_workbench.engine import Engine
from ocr_workbench.errors import E_INTERACT_BUDGET_PENDING
from ocr_workbench.loader import load_directory
from ocr_workbench.report import serialize_results
from ocr_workbench.runner import run_full, compare_payloads

BASE = ConfigBundle.load("configs/detection.json", "configs/recognition.json")


def test_zero_budget_always_returns_pending_without_blocking():
    cfg = BASE.with_overrides(det_overrides={"interact_budget_ms": 0})
    records, failures = load_directory(
        "samples", cfg.detection["image_size"])
    engine = Engine(records, failures, cfg, tempfile.mkdtemp())
    responses = []
    for seq in range(1, 8):
        start = time.perf_counter()
        response = engine.submit_command({
            "seq": seq, "op": "set_param",
            "stage": "recognition", "param": "confidence_threshold",
            "value": 0.4 + 0.03 * seq,
        }, budget_ms=0)
        # A budgeted response must return effectively immediately and must
        # never be described as a terminated/timeout computation.
        assert (time.perf_counter() - start) < 1.0
        assert response["status"] == E_INTERACT_BUDGET_PENDING
        assert response["over_budget"] in (True, False)
        responses.append(response)
    engine.wait_idle(timeout=60)
    final = engine.wait_version(engine.global_version, timeout=60)
    assert final is not None
    engine.request_exit()
    assert engine.wait_exit() == 0

    _, _, full = run_full("samples", engine.config)
    equal, diffs = compare_payloads(
        serialize_results(full), serialize_results(final))
    assert equal, diffs


def test_pending_then_ready_transition():
    cfg = BASE.with_overrides(det_overrides={"interact_budget_ms": 1})
    records, failures = load_directory(
        "samples", cfg.detection["image_size"])
    engine = Engine(records, failures, cfg, tempfile.mkdtemp())
    pending = engine.submit_command({"seq": 0, "op": "snapshot"})
    assert pending["status"] == E_INTERACT_BUDGET_PENDING
    engine.wait_idle(timeout=60)
    ready = engine.submit_command({"seq": 1, "op": "snapshot"})
    assert ready["status"] == "OK"
    assert ready["completed_lines"] > 0
    engine.request_exit()
    engine.wait_exit()
