from __future__ import annotations

import json
from pathlib import Path

from ner_governance.cli import build_report


def test_cli_report_contains_required_sections(tmp_path):
    data_dir = Path(__file__).resolve().parents[1] / "data"
    report = build_report(
        data_dir=data_dir,
        snapshot_dir=tmp_path / "snapshots",
        migration_dir=tmp_path / "migrations",
    )
    assert report["ok"]
    assert report["code"] == "OK"
    assert "E_TYPE_RETIRED" in report["business_codes"]
    assert len(report["migrations"]) == 6
    assert report["cross_format"]["ok"]
    assert report["self_check"]["ok"]
    assert report["trace"]["verify_all"]["verified_positions"] == 134
    assert report["snapshots"]["rollback_attempt"]["code"] == (
        "E_SNAPSHOT_ROLLBACK"
    )
    assert report["verification"]["merge_byte_identical_across_views"]
    json.dumps(report, ensure_ascii=False)
