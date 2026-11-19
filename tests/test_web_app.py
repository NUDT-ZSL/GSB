from __future__ import annotations

import json
from pathlib import Path

from qconv.models import Budget, SchedulerConfig, ToleranceConfig
from qconv.samplegen import default_sample_jobs
from qconv.service import QConvService
from qconv.web import HTML, Application


def test_web_application_edits_runs_and_cleans(tmp_path: Path) -> None:
    jobs = default_sample_jobs()[:1]
    jobs_path = tmp_path / "jobs.jsonl"
    jobs_path.write_text(
        json.dumps(jobs[0].to_dict(), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    service = QConvService(tmp_path / "snapshots", tmp_path / "cache")
    app = Application(
        service,
        jobs_path,
        Budget(5_000_000, 2_000_000, 2),
        SchedulerConfig(max_workers_per_task=1),
        ToleranceConfig(),
    )
    initial = app.state()
    assert initial["status"] == "idle"
    result = app.run(
        {
            "mode": "normal",
            "jobs_text": jobs_path.read_text(encoding="utf-8"),
            "mac_budget": 5_000_000,
            "memory_budget_bytes": 2_000_000,
            "worker_slots": 2,
            "max_workers_per_task": 1,
            "atol": 0.05,
        }
    )
    assert result["ok"]
    assert result["state"]["artifact"]["self_check"]["ok"]
    assert result["state"]["artifact"]["snapshot"]["generation"] == 1
    assert "QConv Budget Lab" in HTML
    cleaned = app.clean()
    assert cleaned["ok"]
    assert cleaned["state"]["snapshot"] is None

