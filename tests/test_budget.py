from __future__ import annotations

import time
from pathlib import Path

from config_forensics.codes import Code
from config_forensics.digests import DEFAULT_ALGORITHMS
from config_forensics.engine import EngineConfig, ForensicEngine
from config_forensics.parser import ParseContext, process_evidence_file
from tests.helpers import make_record, write_baseline, write_evidence_file


def _setup(tmp_path: Path, count: int, hosts: list[str] | None = None) -> list[Path]:
    write_baseline(
        tmp_path / "baseline.json",
        [
            {
                "host_id": host,
                "path": "service.port",
                "source": "budget-policy",
                "effective_from_seq": 1,
                "effective_to_seq": 10,
                "ranges": [{"interval": "[0,10]"}],
            }
            for host in (hosts or [f"host-{index}" for index in range(count)])
        ],
    )
    paths = []
    for index in range(count):
        host = (hosts or [f"host-{index}" for index in range(count)])[index]
        paths.append(
            write_evidence_file(
                tmp_path / "evidence",
                f"file-{index}.json",
                [
                    make_record(
                        evidence_id=f"e-{host}",
                        host_id=host,
                        sequence=1,
                        path="service.port",
                        value=1,
                    )
                ],
                file_id=f"file-{index}",
            )
        )
    return paths


def test_queue_limit_rejects_and_counts_files(tmp_path: Path) -> None:
    paths = _setup(tmp_path, 4)
    context = ParseContext(allowed_algorithms=DEFAULT_ALGORITHMS)

    def slow_processor(task):
        time.sleep(0.1)
        return process_evidence_file(
            task.path,
            source_id=task.source_id,
            source_name=task.source_name,
            file_index=task.file_index,
            context=context,
        )

    result = ForensicEngine(
        EngineConfig(workers=1, queue_limit=0, max_file_seconds=3.0)
    ).run(
        paths,
        tmp_path / "baseline.json",
        tmp_path / "report.json",
        tmp_path / "graph.json",
        processor=slow_processor,
    )
    report = result.report
    rejected = [
        file for file in report["files"] if file["status"] == "queue_rejected"
    ]
    assert rejected
    assert report["budget"]["queue_rejected_files"] == len(rejected)
    assert all(
        any(issue["code"] == Code.QUEUE_FULL for issue in file["issues"])
        for file in rejected
    )


def test_measured_timeout_skips_only_its_evidence(tmp_path: Path) -> None:
    paths = _setup(tmp_path, 2, hosts=["host-slow", "host-fast"])
    context = ParseContext(allowed_algorithms=DEFAULT_ALGORITHMS)

    def processor(task):
        if task.file_index == 1:
            time.sleep(0.3)
        return process_evidence_file(
            task.path,
            source_id=task.source_id,
            source_name=task.source_name,
            file_index=task.file_index,
            context=context,
        )

    result = ForensicEngine(
        EngineConfig(workers=2, queue_limit=8, max_file_seconds=0.03)
    ).run(
        paths,
        tmp_path / "baseline.json",
        tmp_path / "report.json",
        tmp_path / "graph.json",
        processor=processor,
    )
    report = result.report
    by_source = {file["source_id"]: file for file in report["files"]}
    assert by_source["file-0.json"]["status"] == "timed_out"
    assert by_source["file-1.json"]["status"] == "accepted"
    timeout_issue = by_source["file-0.json"]["issues"][0]
    assert timeout_issue["code"] == Code.FILE_TIMEOUT
    assert timeout_issue["details"]["thread_termination_claimed"] is False
    assert {item["host_id"] for item in report["evidence"]} == {"host-fast"}
    assert report["budget"]["timed_out_files"] == 1


def test_workers_do_not_change_verdicts_without_budget_limits(tmp_path: Path) -> None:
    paths = _setup(tmp_path, 6)
    outputs = {}
    for workers in (1, 5):
        outputs[workers] = ForensicEngine(
            EngineConfig(workers=workers, queue_limit=32, max_file_seconds=3.0)
        ).run(
            list(reversed(paths)),
            tmp_path / "baseline.json",
            tmp_path / f"report-{workers}.json",
            tmp_path / f"graph-{workers}.json",
        )
    assert outputs[1].report["drift_points"] == outputs[5].report["drift_points"]
    assert outputs[1].report["chains"] == outputs[5].report["chains"]
    assert outputs[1].report["errors"] == outputs[5].report["errors"]
    assert outputs[1].graph == outputs[5].graph
