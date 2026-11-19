import json
import sqlite3

import pytest

from pipeline_tool.errors import E_CLOCK_SKEW, E_DIGEST_MISMATCH, E_STATE_CONFLICT, PipelineError
from pipeline_tool.pipeline import load_pipeline
from pipeline_tool.scheduler import Scheduler
from pipeline_tool.store import Ledger
from pipeline_tool.verify import verify


def test_idempotent_commit(tmp_path):
    ledger = Ledger(tmp_path / "l.db")
    pipe = load_pipeline("data/pipeline.json")
    ledger.init_run(pipe)
    ledger.mark_running("filter_high")
    assert ledger.commit_node("filter_high", "d1", [{"id": "x"}], {"x": {"node": "filter_high", "inputs": []}}, 0)
    # Same digest again: no-op, still exactly one committed row.
    assert ledger.commit_node("filter_high", "d1", [{"id": "x"}], {"x": {"node": "filter_high", "inputs": []}}, 0) is False
    conn = sqlite3.connect(tmp_path / "l.db")
    count = conn.execute("SELECT COUNT(*) FROM nodes WHERE name='filter_high' AND state='COMMITTED'").fetchone()[0]
    conn.close()
    assert count == 1
    # Conflicting digest on a committed node: rejected.
    with pytest.raises(PipelineError) as exc:
        ledger.commit_node("filter_high", "d2", [], {}, 0)
    assert exc.value.code == E_STATE_CONFLICT
    ledger.close()


def test_clock_skew_rejected(tmp_path):
    ledger = Ledger(tmp_path / "l.db")
    pipe = load_pipeline("data/pipeline.json")
    ledger.init_run(pipe)
    ledger.advance_clock(0, 100, 50, 0, 2, 0, 0, 200)
    # Tamper: roll the persisted clock backwards beyond tolerance.
    conn = sqlite3.connect(tmp_path / "l.db")
    conn.execute("UPDATE meta SET value='-10000' WHERE key='clock'")
    conn.commit()
    conn.close()
    with pytest.raises(PipelineError) as exc:
        ledger.advance_clock(1, 100, 50, 100, 0, 0, 0, 0)
    assert exc.value.code == E_CLOCK_SKEW
    assert exc.value.fields["persisted_clock"] == -10000
    ledger.close()


def test_digest_mismatch_refuses_advance(tmp_path, input_records, demo_pipeline_path):
    pipe = load_pipeline(demo_pipeline_path)
    sched = Scheduler(pipe, input_records, tmp_path / "l.db", tmp_path / "r.json")
    sched.ledger.init_run(pipe)
    # Commit a parent, then delete its committed row so the child's input
    # digest no longer matches committed state.
    sched.ledger.mark_running("filter_high")
    sched.ledger.commit_node("filter_high", "d", [{"id": "a", "qty": 1}], {}, 0)
    conn = sqlite3.connect(tmp_path / "l.db")
    conn.execute("DELETE FROM nodes WHERE name='filter_high'")
    conn.commit()
    conn.close()
    with pytest.raises(PipelineError) as exc:
        sched._execute_node(pipe.nodes["sum_qty"])
    assert exc.value.code == E_DIGEST_MISMATCH
    sched.close()


def test_verify_detects_tampered_results(demo_pipeline_path, input_records, tmp_path):
    pipe = load_pipeline(demo_pipeline_path)
    db, out = tmp_path / "l.db", tmp_path / "r.json"
    sched = Scheduler(pipe, input_records, db, out)
    sched.run()
    sched.close()
    assert verify(pipe, input_records, db, out)["ok"] is True
    # Tamper: drop a committed node from the result file.
    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["nodes"].pop("sum_qty")
    out.write_text(json.dumps(payload), encoding="utf-8")
    report = verify(pipe, input_records, db, out)
    assert report["ok"] is False
    failed = {c["check"] for c in report["checks"] if not c["ok"]}
    assert "ledger_results_same_committed_set" in failed
