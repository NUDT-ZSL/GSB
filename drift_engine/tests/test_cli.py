import json
import os
import sys

from drift_engine import cli, codes
from drift_engine.samplegen import generate


def _run(main_args):
    return cli.main(main_args)


def test_cli_end_to_end_exit_codes(tmp_path):
    workspace = tmp_path / "samples"
    generate(str(workspace))
    db = tmp_path / "ledger.db"
    cp = tmp_path / "checkpoints"
    report = tmp_path / "import.json"
    common = ["--spec", str(workspace / "spec.json"), "--db", str(db),
              "--checkpoint-dir", str(cp)]

    code = _run(common + [
        "import", "--manifest", str(workspace / "manifest.json"),
        "--batch-id", "batch-1", "--window-seconds", "60",
        "--decision-clock-ms", "1700000120000", "--workers", "4",
        "--report", str(report),
    ])
    assert code == codes.EXIT_PARTIAL  # generated samples intentionally contain bad records
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["accepted_records"] > 0
    assert payload["rejected_records"]

    decide_report = tmp_path / "decide.json"
    code = _run(common + [
        "decide", "--window-seconds", "60",
        "--decision-clock-ms", "1700000120000", "--report", str(decide_report),
    ])
    assert code == codes.EXIT_OK
    assert json.loads(decide_report.read_text(encoding="utf-8"))["decisions"]

    attr_report = tmp_path / "attr.json"
    code = _run(common + [
        "attribute", "--window-seconds", "60",
        "--decision-clock-ms", "1700000120000", "--report", str(attr_report),
    ])
    assert code == codes.EXIT_PARTIAL  # dependency cycle present in generated data
    attr = json.loads(attr_report.read_text(encoding="utf-8"))
    assert any(row["code"] == codes.E_DEP_CYCLE for row in attr["attribution"])

    verify_report = tmp_path / "verify.json"
    code = _run(common + ["verify", "--report", str(verify_report)])
    assert code == codes.EXIT_OK
    assert json.loads(verify_report.read_text(encoding="utf-8"))["code"] is None


def test_cli_usage_error(tmp_path):
    code = _run(["--spec", str(tmp_path / "missing.json"),
                 "--db", str(tmp_path / "x.db"),
                 "--checkpoint-dir", str(tmp_path / "cp"),
                 "verify"])
    assert code == codes.EXIT_USAGE
