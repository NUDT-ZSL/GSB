from __future__ import annotations

import random

from osl_forensics.engine import BudgetConfig, ProcessJob, process_files
from osl_forensics.ledger import Ledger
from osl_forensics.writer import SegmentSpec, build_osl


def _marker_jobs(generated_root) -> list[ProcessJob]:
    jobs = []
    for marker_path in sorted((generated_root / "injections").rglob("sync_marker.json")):
        import json

        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        jobs.append(
            ProcessJob(
                file_id=marker["case_id"],
                data=(marker_path.parent / marker["sample_file"]).read_bytes(),
            )
        )
    return jobs


def _event_tuples(ledger: Ledger):
    rows = ledger.conn.execute(
        """
        SELECT run_id,file_id,segment_key,event_type,status,state_generation,
               processed_watermark,cumulative_digest,content_digest,payload_digest,event_hash
        FROM ledger_event ORDER BY event_id
        """
    ).fetchall()
    return [tuple(row) for row in rows]


def test_concurrency_and_arrival_order_match_serial(generated_root, tmp_path):
    jobs = _marker_jobs(generated_root)
    serial_db = tmp_path / "serial.sqlite"
    concurrent_db = tmp_path / "concurrent.sqlite"

    with Ledger(serial_db) as serial_ledger:
        serial = process_files(
            jobs,
            budget=BudgetConfig(concurrency=1, timeout_seconds=10.0, max_queue=100),
            ledger=serial_ledger,
            run_id_prefix="eq",
        )
        serial_states = serial_ledger.comparable_states()
        serial_events = _event_tuples(serial_ledger)

    shuffled = jobs[:]
    random.Random(7719).shuffle(shuffled)
    with Ledger(concurrent_db) as concurrent_ledger:
        concurrent = process_files(
            shuffled,
            budget=BudgetConfig(concurrency=8, timeout_seconds=10.0, max_queue=100),
            ledger=concurrent_ledger,
            run_id_prefix="eq",
        )
        concurrent_states = concurrent_ledger.comparable_states()
        concurrent_events = _event_tuples(concurrent_ledger)

    assert [result.file_id for result in serial.results] == sorted(job.file_id for job in jobs)
    assert [result.file_id for result in concurrent.results] == sorted(job.file_id for job in jobs)
    assert concurrent_states == serial_states
    assert concurrent_events == serial_events
    assert concurrent.rejections == serial.rejections


def test_deterministic_queue_rejections_under_shuffled_arrival(tmp_path):
    jobs = [ProcessJob(file_id=f"f{i:02d}", data=b"OSL1") for i in range(8)]
    db1 = tmp_path / "q1.sqlite"
    db2 = tmp_path / "q2.sqlite"
    with Ledger(db1) as ledger1:
        serial = process_files(
            jobs,
            budget=BudgetConfig(concurrency=1, timeout_seconds=1, max_queue=3),
            ledger=ledger1,
        )
    shuffled = list(reversed(jobs))
    with Ledger(db2) as ledger2:
        concurrent = process_files(
            shuffled,
            budget=BudgetConfig(concurrency=4, timeout_seconds=1, max_queue=3),
            ledger=ledger2,
        )

    expected_rejected = [f"f{i:02d}" for i in range(3, 8)]
    assert [item["file_id"] for item in serial.rejections] == expected_rejected
    assert [item["file_id"] for item in concurrent.rejections] == expected_rejected
    assert serial.report["counts"]["E_QUEUE_FULL"] == 5
    assert concurrent.report["counts"]["E_QUEUE_FULL"] == 5


def test_zero_wall_clock_budget_times_out_and_writes_nothing(tmp_path):
    payload = bytes(range(256)) * 4096
    sample = build_osl([SegmentSpec("big", payload, compression=1, algo=1)]).data
    db = tmp_path / "timeout.sqlite"
    with Ledger(db) as ledger:
        outcome = process_files(
            [ProcessJob(file_id="timeout-file", data=sample)],
            budget=BudgetConfig(concurrency=1, timeout_seconds=0.0, max_queue=4),
            ledger=ledger,
        )
        assert outcome.results[0].primary_code == "E_TIMEOUT"
        assert ledger.list_states() == []
        assert ledger.verify_replay()["events"] == 0


def test_engine_can_override_recursive_expansion_quota(tmp_path):
    child = build_osl([SegmentSpec("child", b"q" * 64)]).data
    sample = build_osl([
        SegmentSpec("outer", child, compression=0x10, segment_type=4)
    ]).data
    with Ledger(tmp_path / "quota.sqlite") as ledger:
        outcome = process_files(
            [ProcessJob(file_id="quota", data=sample)],
            budget=BudgetConfig(concurrency=1, max_expanded_bytes=len(sample)),
            ledger=ledger,
        )
        assert outcome.results[0].primary_code == "E_QUOTA"
        assert ledger.list_states() == []
