from __future__ import annotations

import json
from pathlib import Path

from ml_gateway.demo import run_fault


def test_expected_fault_workflows_write_structured_logs(tmp_path, capsys):
    rule = run_fault("corrupt_declaration", tmp_path / "rule")
    vector = run_fault("vector_length", tmp_path / "vector")
    snapshot = run_fault("snapshot_conflict", tmp_path / "snapshot")
    assert rule["error"]["code"] == "E_RULE"
    assert rule["error"]["path"] == "$.backends[0].port"
    assert vector["error"]["code"] == "E_VECTOR"
    assert vector["error"]["path"] == "$.backends[0].parameter_file"
    assert snapshot["error"]["code"] == "E_SNAP"
    assert snapshot["error"]["details"]["fingerprint_diff"]["changed"] == ["be-alpha"]
    for result in (rule, vector, snapshot):
        log_path = Path(result["report_path"]).parent.parent / "logs" / "gateway.jsonl"
        records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        assert records[-1]["code"] == result["error"]["code"]
        report = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
        assert report["expected_code"] == result["error"]["code"]
