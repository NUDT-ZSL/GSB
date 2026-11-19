from __future__ import annotations

import json
from pathlib import Path

from multilabel_lab.cli import main


def test_cli_classify_inject_and_metrics(tmp_path: Path):
    state_dir = tmp_path / "snapshots"
    normal_path = tmp_path / "normal.json"
    injection_path = tmp_path / "injection.json"
    injection_file = tmp_path / "injections.json"
    metrics_path = tmp_path / "metrics.json"

    assert (
        main(
            [
                "classify",
                "--state-dir",
                str(state_dir),
                "--samples",
                "6",
                "--output",
                str(normal_path),
            ]
        )
        == 0
    )
    normal = json.loads(normal_path.read_text(encoding="utf-8"))
    assert normal["report"]["status"] == "ok"
    assert normal["verification"]["matched"] is True
    assert normal["report"]["injection_records"] == []

    injection_file.write_text(
        json.dumps(
            {
                "injections": [
                    {
                        "stage": "decision",
                        "type": "nonpositive_threshold",
                        "sample_id": "sample-0001",
                        "label": "cat",
                        "params": {"label": "cat", "threshold": 0.0},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "inject",
                "--state-dir",
                str(state_dir),
                "--samples",
                "6",
                "--injections",
                str(injection_file),
                "--output",
                str(injection_path),
            ]
        )
        == 0
    )
    injection = json.loads(injection_path.read_text(encoding="utf-8"))
    assert injection["report"]["status"] == "degraded"
    assert injection["report"]["injection_records"][0]["stage"] == "decision"
    assert injection["report"]["metrics_path"]["mode"] == "degraded"

    assert (
        main(
            [
                "metrics",
                "--report",
                str(normal_path),
                "--output",
                str(metrics_path),
            ]
        )
        == 0
    )
    recomputed = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert recomputed["matches_stored"] is True
    assert recomputed["status"] == "ok"
