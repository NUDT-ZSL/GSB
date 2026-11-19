import json

from pipeline_tool.pipeline import load_pipeline
from pipeline_tool.scheduler import Scheduler
from pipeline_tool.store import STATE_ABANDONED, STATE_COMMITTED, STATE_FAILED, Ledger


def run_demo(demo_pipeline_path, input_records, workspace):
    pipe = load_pipeline(demo_pipeline_path)
    sched = Scheduler(pipe, input_records, workspace["db"], workspace["out"])
    summary = sched.run()
    sched.close()
    return summary


def test_full_run_states_and_abandonment(demo_pipeline_path, input_records, workspace):
    run_demo(demo_pipeline_path, input_records, workspace)
    ledger = Ledger(workspace["db"])
    states = {n["name"]: n for n in ledger.all_nodes()}
    ledger.close()
    assert states["filter_high"]["state"] == STATE_COMMITTED
    assert states["filter_low"]["state"] == STATE_COMMITTED
    assert states["sum_qty"]["state"] == STATE_COMMITTED
    assert states["sum_score"]["state"] == STATE_COMMITTED
    assert states["pass_stats"]["state"] == STATE_COMMITTED
    # heavy_export needs 200 MiB but stage 2 budget is 128 MiB: explicit abandonment.
    assert states["heavy_export"]["state"] == STATE_ABANDONED
    assert "exceeds stage budget" in states["heavy_export"]["reason"]


def test_results_file_matches_ledger(demo_pipeline_path, input_records, workspace):
    run_demo(demo_pipeline_path, input_records, workspace)
    results = json.loads(workspace["out"].read_text(encoding="utf-8"))["nodes"]
    ledger = Ledger(workspace["db"])
    committed = {n["name"] for n in ledger.all_nodes() if n["state"] == STATE_COMMITTED}
    ledger.close()
    assert set(results) == committed
    assert "heavy_export" not in results


def test_two_runs_bit_identical(demo_pipeline_path, input_records, tmp_path):
    outputs = []
    for i in range(2):
        db = tmp_path / f"run{i}.db"
        out = tmp_path / f"run{i}.json"
        pipe = load_pipeline(demo_pipeline_path)
        sched = Scheduler(pipe, input_records, db, out)
        sched.run()
        sched.close()
        outputs.append(out.read_bytes())
    assert outputs[0] == outputs[1]


def test_failed_node_does_not_block_others(input_records, tmp_path):
    raw = {
        "name": "fail_demo",
        "clock": {"step_ms": 100, "tolerance_ms": 10},
        "stages": [{"stage": 0, "max_concurrency": 2, "memory_budget_mib": 256, "window_ms": 100}],
        "nodes": [
            {"name": "ok_node", "stage": 0, "parents": [], "cost_ms": 1, "memory_mib": 10,
             "parallelism": 1,
             "op": {"type": "filter", "field": "value", "op": ">=", "threshold": 0}},
            {"name": "bad_node", "stage": 0, "parents": [], "cost_ms": 1, "memory_mib": 10,
             "parallelism": 1, "op": {"type": "does_not_exist"}},
        ],
    }
    path = tmp_path / "pipe.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    pipe = load_pipeline(path)
    sched = Scheduler(pipe, input_records, tmp_path / "l.db", tmp_path / "r.json")
    sched.run()
    sched.close()
    ledger = Ledger(tmp_path / "l.db")
    states = {n["name"]: n for n in ledger.all_nodes()}
    ledger.close()
    assert states["ok_node"]["state"] == STATE_COMMITTED
    assert states["bad_node"]["state"] == STATE_FAILED
    results = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["nodes"]
    assert set(results) == {"ok_node"}  # failure did not pollute the result file
