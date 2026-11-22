from __future__ import annotations

from pathlib import Path

from ic_ledger.storage import Store
from ic_ledger.validation import parse_document

from conftest import remove_db


def _document(*, budget: int) -> dict:
    records = [
        {
            "arrival_frames": 1,
            "frame_bytes": 100,
            "iface": "eth0",
            "logical_clock_us": index,
            "queue": 0,
            "record_seq": index + 1,
        }
        for index in range(100)
    ]
    return {
        "config": {
            "avg_frame_interval_us": "1.5",
            "clock_step_us": 1,
            "error_bound_budget_us": "100",
            "fixed_scale": 1,
            "float_tolerance": "1e-12",
            "irq_budget_per_second": {"eth0:0": budget},
        },
        "interfaces": [
            {
                "name": "eth0",
                "queues": [
                    {
                        "coalesce": {
                            "rx_frames": 3,
                            "rx_usecs": 0,
                            "tx_frames": 0,
                            "tx_usecs": 0,
                        },
                        "max_irq_rate": 1000000,
                        "queue": 0,
                        "weight": 1,
                    }
                ],
            }
        ],
        "traffic": records,
    }


def test_residue_only_attribution_and_divergence_detail(tmp_path: Path) -> None:
    db = tmp_path / "attribution.sqlite"
    remove_db(db)
    document = parse_document(_document(budget=305000))
    with Store(db) as store:
        store.import_document(document)
        store.run_queues()
        diagnosis = store.diagnose()
        status = store.status()
    queue = diagnosis["queues"][0]
    assert queue["verdict"] == "exceeds_budget"
    assert queue["attributed_to"] == "conversion_residue"
    assert queue["residue_ratio_of_excess"] != "0"
    assert queue["first_threshold_crossing_record"] is not None
    assert queue["participating_record_interval"]["first"] == 1

    snapshot = status["queues"][0]
    detail = snapshot["first_divergence"]
    assert detail["record_seq"] == queue["first_threshold_crossing_record"]
    assert detail["residue"] != "0"
    assert detail["fixed_intermediate"] != detail["float_intermediate"]
    assert set(detail["metric_deviations"]) == {
        "irq_rate",
        "throughput",
        "window_occupancy",
    }
    assert detail["conversion_error_us"] != "0"
    assert detail["arithmetic_error_us"] != "0"
    assert snapshot["conversion_summary"]["count"] == 34
    assert snapshot["conversion_summary"]["last_trace"]["rounding"] == "floor"
    assert snapshot["conversion_summary"]["last_trace"]["scale"] == 1


def test_previous_diagnosis_snapshot_is_preserved(tmp_path: Path) -> None:
    db = tmp_path / "snapshots.sqlite"
    remove_db(db)
    document = parse_document(_document(budget=305000))
    with Store(db) as store:
        store.import_document(document)
        store.run_queues(max_records=10)
        first = store.diagnose()
        first_watermark = first["queues"][0]["ledger_watermark"]
        store.run_queues(max_records=10)
        store.diagnose()
        rows = store.conn.execute(
            "SELECT ledger_watermark, result_json FROM diagnoses "
            "WHERE iface = 'eth0' AND queue = 0 ORDER BY id"
        ).fetchall()
    assert first_watermark == 10
    assert len(rows) == 2
    assert int(rows[0]["ledger_watermark"]) == 10
    assert int(rows[1]["ledger_watermark"]) == 20

