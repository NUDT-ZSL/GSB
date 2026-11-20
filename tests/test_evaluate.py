"""Requirement 8 (evaluation): comparable metrics across arrival-rate
sample points for both routing modes, plus deterministic output."""
import json

from gateway import models
from gateway.evaluate import evaluate


def test_evaluate_metrics_and_selection(loaded, tmp_path):
    services, nodes, trace = loaded
    report = evaluate(services, nodes, trace, str(tmp_path / "eval"),
                      scales=(0.25, 1.0, 4.0))
    assert report["schema"] == "gateway-eval/v1"
    selected = {row["scale"]: row["selected_mode"]
                for row in report["results"]}
    # low scale selects per-request, high scale selects window-batch
    assert selected[0.25] == models.MODE_PER_REQUEST
    assert selected[4.0] == models.MODE_WINDOW_BATCH
    for row in report["results"]:
        assert row["rationale"]
        for mode in (models.MODE_PER_REQUEST, models.MODE_WINDOW_BATCH):
            metrics = row["modes"][mode]
            assert metrics["completed"] > 0
            assert 0.0 <= metrics["replica_utilization"] <= 1.0
            q = metrics["queue_wait_quantiles_ms"]
            assert q["p50"] <= q["p90"] <= q["p99"]
            assert metrics["failures"] >= 0
    assert "truth" in report["sources"] and "observation" in report["sources"]


def test_evaluate_deterministic(loaded, tmp_path):
    services, nodes, trace = loaded
    r1 = evaluate(services, nodes, trace, str(tmp_path / "e1"),
                  scales=(0.5, 1.0))
    r2 = evaluate(services, nodes, trace, str(tmp_path / "e2"),
                  scales=(0.5, 1.0))
    assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)
