#!/usr/bin/env python3
"""Run the deterministic end-to-end pipeline and write a verification report."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ic_ledger.storage import Store  # noqa: E402
from ic_ledger.validation import load_document  # noqa: E402


def _remove_database(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", ".lock"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def main() -> int:
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    db_path = artifacts / "demo.sqlite"
    report_path = artifacts / "verification-report.json"
    _remove_database(db_path)

    document = load_document(ROOT / "samples" / "sample.json")
    with Store(db_path) as store:
        imported = store.import_document(document)
        first_run = store.run_queues()
        second_run = store.run_queues()
        diagnosis = store.diagnose()
        status = store.status()
        verification = store.verify()

    assert imported.batch_version == 1
    assert first_run["committed"] == 75
    assert second_run["committed"] == 0
    assert second_run["idempotent"] == 75
    assert status["record_watermark"] == 1740
    assert [item["fixed_interrupts"] for item in status["queues"]] == [9, 6, 8]
    assert status["diverged_queues"] == ["eth0:1"]
    assert status["queues"][1]["first_divergence"]["record_seq"] == 1060
    assert verification["ok"] is True
    q0_diagnosis = next(
        item
        for item in diagnosis["queues"]
        if item["iface"] == "eth0" and item["queue"] == 0
    )
    assert q0_diagnosis["attributed_to"] == "mixed_traffic_and_residue"

    report = {
        "dataset": {
            "flow_records": len(document.flows),
            "input_hash": document.input_hash,
            "queues": len(document.queues),
        },
        "diagnosis": diagnosis,
        "idempotent_rerun": second_run,
        "import": {
            "batch_version": imported.batch_version,
            "idempotent": imported.idempotent,
        },
        "ok": True,
        "run": first_run,
        "status": status,
        "verification": verification,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": True,
                "report": str(report_path),
                "record_watermark": status["record_watermark"],
                "interrupts": {
                    f"{item['iface']}:{item['queue']}": item["fixed_interrupts"]
                    for item in status["queues"]
                },
                "diverged_queues": status["diverged_queues"],
                "verify": verification,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

