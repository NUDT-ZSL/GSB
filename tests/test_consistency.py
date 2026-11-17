import json
import tempfile

from ocr_workbench.config import ConfigBundle
from ocr_workbench.report import serialize_results
from ocr_workbench.runner import compare_payloads, run_full, run_incremental

BASE = ConfigBundle.load("configs/detection.json", "configs/recognition.json")


def _commands():
    with open("configs/commands.json") as handle:
        return json.load(handle)["commands"]


def test_incremental_matches_full_at_final_version():
    commands = _commands()
    engine, exit_code = run_incremental(
        "samples", BASE, commands, tempfile.mkdtemp())
    assert exit_code == 0
    _, _, full_results = run_full("samples", engine.config)
    equal, diffs = compare_payloads(
        serialize_results(full_results),
        serialize_results(engine._latest_results))
    assert equal, diffs
    set_params = [c for c in commands if c["op"] == "set_param"]
    assert engine.global_version == 1 + len(set_params)


def test_reuse_actually_occurred_during_edit_sequence():
    commands = _commands()
    engine, _ = run_incremental(
        "samples", BASE, commands, tempfile.mkdtemp())
    reused = set()
    for result in engine._latest_results:
        reused.update(result.reused_stages)
    # Recognition-only edits in the command stream must reuse detection.
    assert "detection" in reused


def test_line_and_char_ordering_is_stable():
    engine, _ = run_incremental(
        "samples", BASE, _commands(), tempfile.mkdtemp())
    names = [r.name for r in engine._latest_results]
    assert names == sorted(names)
    for result in engine._latest_results:
        for line in result.lines:
            boxes = [c["bbox"][0] for c in line["chars"]]
            assert boxes == sorted(boxes)
