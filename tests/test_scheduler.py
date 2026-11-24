from __future__ import annotations

from conftest import run_sample


def test_per_host_alert_budget_rejects_all_oversized_hosts(
    sample_dir, tmp_path
):
    _, report = run_sample(
        sample_dir,
        tmp_path / "budget",
        max_alerts_per_host=1,
    )
    assert report["counts"]["clusters"] == 0
    assert report["business_codes"]["E_BUDGET"] == report["scheduler"]["hosts"]
    assert report["scheduler"]["rejected_hosts"] == report["scheduler"]["hosts"]


def test_wall_clock_timeout_stops_host_processing(sample_dir, tmp_path):
    _, report = run_sample(
        sample_dir,
        tmp_path / "timeout",
        host_timeout_ms=0,
    )
    assert report["counts"]["clusters"] == 0
    assert report["business_codes"]["E_TIMEOUT"] == report["scheduler"]["hosts"]
    assert report["scheduler"]["rejected_hosts"] == report["scheduler"]["hosts"]


def test_queue_admission_limit_is_deterministic(sample_dir, tmp_path):
    _, report = run_sample(
        sample_dir,
        tmp_path / "queue",
        workers=1,
        queue_limit=1,
    )
    capacity = 1 + 1
    expected_rejected = report["scheduler"]["hosts"] - capacity
    assert expected_rejected > 0
    assert report["business_codes"]["E_QUEUE_FULL"] == expected_rejected
    assert report["scheduler"]["rejected_hosts"] == expected_rejected


def test_scheduler_elapsed_is_real_wall_clock(default_report):
    _, report = default_report
    assert report["scheduler"]["elapsed_ms"] >= 0
    assert report["scheduler"]["host_elapsed_ms"]
    assert all(
        value >= 0 for value in report["scheduler"]["host_elapsed_ms"].values()
    )
