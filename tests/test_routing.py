"""Requirement 2: scale-based routing adjudication and deterministic
decision chains."""
from gateway import models
from gateway.engine import Engine
from gateway.store import Store


def _run(loaded, db_path, **kwargs):
    services, nodes, trace = loaded
    store = Store(str(db_path))
    try:
        return Engine(services, nodes, trace, store, **kwargs).run()
    finally:
        store.close()


def test_mode_switches_across_threshold(loaded, tmp_path):
    report = _run(loaded, tmp_path / "r.db")
    modes = {}
    for rnd in report["rounds"]:
        for d in rnd["decisions"]:
            modes[(rnd["version"], d["service"])] = d["mode"]
    # low-load windows route per-request
    assert modes[(0, "chat")] == models.MODE_PER_REQUEST
    assert modes[(1, "summarizer")] == models.MODE_PER_REQUEST
    # high-load windows switch to window-level batch routing
    assert modes[(2, "chat")] == models.MODE_WINDOW_BATCH
    assert modes[(5, "embed")] == models.MODE_WINDOW_BATCH


def test_decision_chain_fields(loaded, tmp_path):
    report = _run(loaded, tmp_path / "r.db")
    for rnd in report["rounds"]:
        for d in rnd["decisions"]:
            assert d["mode"] in (models.MODE_PER_REQUEST,
                                 models.MODE_WINDOW_BATCH)
            assert d["trigger"]
            assert d["rejected"]["mode"] != d["mode"]
            assert d["rejected"]["reason"]
            assert d["demand_tokens"] == (d["request_count"]
                                          * d["tokens_per_request"])
            # mode is consistent with the load ratio vs threshold
            if d["load_ratio"] < d["threshold"]:
                assert d["mode"] == models.MODE_PER_REQUEST
            else:
                assert d["mode"] == models.MODE_WINDOW_BATCH


def test_decision_chain_bit_identical_across_runs(loaded, tmp_path):
    import json
    r1 = _run(loaded, tmp_path / "a.db")
    r2 = _run(loaded, tmp_path / "b.db")
    assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)
