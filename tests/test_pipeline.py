import json
from pathlib import Path

import pytest

from mt_pipeline.datagen import generate_corpus
from mt_pipeline.errors import Codes, PipelineError
from mt_pipeline.export import self_check
from mt_pipeline.pipeline import run_pipeline
from mt_pipeline.util import read_json


def _config():
    return read_json("config/pipeline_config.json")


def _workspace(tmp_path, n=300):
    data = tmp_path / "data"
    generate_corpus(data, seed=20240501, n_pairs=n)
    return data


def test_datagen_deterministic(tmp_path):
    m1 = generate_corpus(tmp_path / "d1", seed=7, n_pairs=50)
    m2 = generate_corpus(tmp_path / "d2", seed=7, n_pairs=50)
    assert m1["files"] == m2["files"]
    m3 = generate_corpus(tmp_path / "d3", seed=8, n_pairs=50)
    assert m3["files"]["corpus.jsonl"] != m1["files"]["corpus.jsonl"]


def test_end_to_end_pass_and_byte_identical(tmp_path):
    data = _workspace(tmp_path)
    cfg = _config()
    run_pipeline(cfg, data, tmp_path / "r1", tmp_path / "s1")
    run_pipeline(cfg, data, tmp_path / "r2", tmp_path / "s2")
    b1 = (tmp_path / "r1" / "pipeline_report.json").read_bytes()
    b2 = (tmp_path / "r2" / "pipeline_report.json").read_bytes()
    assert b1 == b2  # same input + same fresh state => byte-identical
    report = json.loads(b1.decode("utf-8"))
    assert report["self_check"] == "PASS"
    assert report["business_codes"] == []
    assert report["consistency"]["kept_set_diff"] == []


def test_export_excludes_dropped_and_order_declared(tmp_path):
    data = _workspace(tmp_path)
    report = run_pipeline(_config(), data, tmp_path / "r", tmp_path / "s")
    lines = (tmp_path / "r" / "export.tsv").read_text(encoding="utf-8").splitlines()
    exported_ids = [ln.split("\t")[0] for ln in lines]
    assert exported_ids == sorted(exported_ids)
    kept = {d["pair_id"] for d in report["per_sample_details"] if d["decision"] == "keep"}
    dropped = {d["pair_id"] for d in report["per_sample_details"] if d["decision"] == "drop"}
    assert set(exported_ids) == kept
    assert not (set(exported_ids) & dropped)
    assert len(exported_ids) == report["metrics"]["kept"]


def test_self_check_detects_inconsistency():
    ctx = {
        "ingest_ids": ["p0", "p1"],
        "subword_ids": ["p0"],  # missing p1
        "filter_ids": ["p0", "p1"],
        "kept_ids": [], "dropped_ids": [], "exported_ids": [],
        "vocab": [], "all_pieces": [],
        "details": [], "reported_metrics": {
            "total": 0, "kept": 0, "dropped": 0, "keep_rate": 0.0,
            "ratio_stats": {"min": 0.0, "max": 0.0, "mean": 0.0}},
        "batches": [["p0", "p1"]],
    }
    with pytest.raises(PipelineError) as exc:
        self_check(ctx)
    assert exc.value.code == Codes.INCONSISTENT
    assert exc.value.position == "stage_pair_ids:subword"


def test_pipeline_budget_rejection_writes_code(tmp_path):
    data = _workspace(tmp_path)
    cfg = _config()
    cfg["budgets"]["compare_budget"] = 5
    with pytest.raises(PipelineError) as exc:
        run_pipeline(cfg, data, tmp_path / "r", tmp_path / "s")
    assert exc.value.code == Codes.COMPARE_BUDGET
