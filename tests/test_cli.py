from __future__ import annotations

import json

from tlsdiag.cli import main


def _argv(sample_dir, db_path, *extra):
    return ["--sample", str(sample_dir), "--db", str(db_path), *extra]


def test_full_cli_workflow(sample_dir, db_path, capsys):
    assert main(_argv(sample_dir, db_path, "load")) == 0
    assert main(_argv(sample_dir, db_path, "chain")) == 0
    assert main(_argv(sample_dir, db_path, "apply", "--version", "1")) == 0
    assert main(_argv(sample_dir, db_path, "verify")) == 0
    assert main(_argv(sample_dir, db_path, "resume")) == 0
    assert main(_argv(sample_dir, db_path, "status")) == 0
    assert main(_argv(sample_dir, db_path, "verify-audit")) == 0

    capsys.readouterr()
    main(_argv(sample_dir, db_path, "status"))
    status = json.loads(capsys.readouterr().out)
    assert status["current_version"] == 1
    assert status["clock_high_water_ms"] is not None
    assert len(status["chain_conclusion_summary"]) == 6


def test_cli_reports_business_error_code(sample_dir, db_path):
    path = sample_dir / "truststore_v1" / "truststore.json"
    data = json.loads(path.read_text())
    data["entries"][0]["relative_trust_seconds"] = -1
    path.write_text(json.dumps(data))
    code = main(_argv(sample_dir, db_path, "load"))
    assert code == 2
