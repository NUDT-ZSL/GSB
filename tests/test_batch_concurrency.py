from __future__ import annotations

import json
from pathlib import Path

from slpguard import BatchConfig, normalized_results, run_batch
from slpguard.io_utils import read_json_envelope


def test_serial_and_parallel_results_are_identical(corpus, tmp_path: Path) -> None:
    reports = {}
    for workers in (1, 8):
        workdir = tmp_path / f"workers-{workers}"
        workdir.mkdir()
        reports[workers] = run_batch(
            corpus,
            config=BatchConfig(
                workers=workers,
                checkpoint_enabled=True,
                report_path="reports/concurrency.json",
            ),
            workdir=workdir,
        )
    assert normalized_results(reports[1]) == normalized_results(reports[8])
    assert reports[1]["batch"]["files"] == reports[8]["batch"]["files"]
    assert reports[1]["batch"]["success"] == reports[8]["batch"]["success"]
    assert reports[1]["batch"]["limited"] == reports[8]["batch"]["limited"]
    assert reports[1]["batch"]["failed"] == reports[8]["batch"]["failed"]
    assert reports[1]["batch"]["truth_matched_events"] == reports[8]["batch"][
        "truth_matched_events"
    ]

    report_path = tmp_path / "workers-1" / "reports" / "concurrency.json"
    payload = read_json_envelope(report_path, kind="slp-report")
    assert payload["batch"]["files"] == len(payload["files"])
    assert list(report_path.parent.glob("*.tmp")) == []


def test_resume_uses_checkpoint_without_changing_results(corpus, tmp_path: Path) -> None:
    workdir = tmp_path / "resume"
    workdir.mkdir()
    first = run_batch(
        corpus,
        config=BatchConfig(workers=1),
        workdir=workdir,
    )
    second = run_batch(
        corpus,
        config=BatchConfig(workers=4),
        workdir=workdir,
    )
    assert second["batch"]["checkpoint_reused_files"] == len(second["files"])
    assert normalized_results(first) == normalized_results(second)
    assert second["batch"]["files"] == len(second["files"])


def test_batch_report_is_integrity_checked(corpus, tmp_path: Path) -> None:
    workdir = tmp_path / "integrity"
    workdir.mkdir()
    report = run_batch(
        corpus / "valid_slp.slp",
        config=BatchConfig(workers=2, checkpoint_enabled=False),
        workdir=workdir,
    )
    path = Path(report["report_path"])
    document = json.loads(path.read_text(encoding="utf-8"))
    document["payload"]["batch"]["success"] = 999
    path.write_text(json.dumps(document), encoding="utf-8")
    try:
        read_json_envelope(path, kind="slp-report")
    except Exception as error:
        assert "integrity" in str(error)
    else:
        raise AssertionError("tampered report passed integrity verification")


def test_identical_file_contents_have_distinct_completion_ids(
    corpus,
    tmp_path: Path,
) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    data = (corpus / "valid_slp.slp").read_bytes()
    (inputs / "a.slp").write_bytes(data)
    (inputs / "b.slp").write_bytes(data)
    workdir = tmp_path / "work"
    workdir.mkdir()
    report = run_batch(
        inputs,
        config=BatchConfig(workers=2, report_path="reports/duplicates.json"),
        workdir=workdir,
    )
    assert report["batch"]["files"] == 2
    assert len({item["file_id"] for item in report["files"]}) == 2
    resumed = run_batch(
        inputs,
        config=BatchConfig(workers=2, report_path="reports/duplicates.json"),
        workdir=workdir,
    )
    assert resumed["batch"]["checkpoint_reused_files"] == 2
