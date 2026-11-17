from __future__ import annotations

from pathlib import Path

from config_forensics.codes import Code
from config_forensics.engine import EngineConfig, ForensicEngine
from tests.helpers import make_record, write_baseline, write_evidence_file


def _run(tmp_path: Path, workers: int = 3):
    return ForensicEngine(
        EngineConfig(workers=workers, queue_limit=32, max_file_seconds=5.0)
    ).run(
        tmp_path / "evidence",
        tmp_path / "baseline.json",
        tmp_path / "report.json",
        tmp_path / "graph.json",
    )


def _baseline(tmp_path: Path, path: str = "service.port") -> None:
    write_baseline(
        tmp_path / "baseline.json",
        [
            {
                "host_id": "host-a",
                "path": path,
                "source": "test-policy",
                "effective_from_seq": 1,
                "effective_to_seq": 100,
                "ranges": [{"interval": "[0,10]"}],
            }
        ],
    )


def test_same_sequence_same_digest_is_deduplicated(tmp_path: Path) -> None:
    _baseline(tmp_path)
    evidence = tmp_path / "evidence"
    first = make_record(
        evidence_id="first-id",
        host_id="host-a",
        sequence=1,
        path="service.port",
        value=1,
    )
    duplicate = {**first, "evidence_id": "duplicate-id", "sync_marker": "sync:dup"}
    second = make_record(
        evidence_id="second-id",
        host_id="host-a",
        sequence=2,
        path="service.port",
        value=20,
        previous="duplicate-id",
    )
    write_evidence_file(evidence, "a.json", [first], file_id="file-a")
    write_evidence_file(evidence, "b.json", [duplicate], file_id="file-b")
    write_evidence_file(evidence, "c.json", [second], file_id="file-c")

    report = _run(tmp_path).report
    assert report["summary"]["unique_evidence_count"] == 2
    chain = report["chains"][0]
    assert chain["status"] == "ok"
    assert chain["sequences"] == [1, 2]
    assert chain["drift"]["sequence"] == 2
    assert "duplicate-id" in report["evidence"][0]["aliases"]


def test_same_sequence_different_digest_is_conflict(tmp_path: Path) -> None:
    _baseline(tmp_path)
    evidence = tmp_path / "evidence"
    write_evidence_file(
        evidence,
        "a.json",
        [
            make_record(
                evidence_id="a",
                host_id="host-a",
                sequence=1,
                path="service.port",
                value=1,
            )
        ],
    )
    write_evidence_file(
        evidence,
        "b.json",
        [
            make_record(
                evidence_id="b",
                host_id="host-a",
                sequence=1,
                path="service.port",
                value=2,
            )
        ],
    )
    report = _run(tmp_path).report
    assert report["conflicts"][0]["host_id"] == "host-a"
    assert report["conflicts"][0]["sequence"] == 1
    assert any(
        issue["code"] == Code.EVIDENCE_CONFLICT and issue["sequence"] == 1
        for issue in report["errors"]
    )
    assert report["chains"] == []


def test_self_link_reports_sequence_and_code(tmp_path: Path) -> None:
    _baseline(tmp_path)
    evidence = tmp_path / "evidence"
    record = make_record(
        evidence_id="self",
        host_id="host-a",
        sequence=1,
        path="service.port",
        value=1,
        previous="self",
    )
    write_evidence_file(evidence, "a.json", [record])
    report = _run(tmp_path).report
    chain = report["chains"][0]
    assert chain["status"] == "self_link"
    self_error = next(
        error for error in chain["errors"] if error["code"] == Code.SELF_LINK
    )
    assert self_error["details"]["sequences"] == [1]
