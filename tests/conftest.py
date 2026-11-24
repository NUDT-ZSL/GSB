from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from memguard.engine import RunOptions, run
from memguard.generator import generate_fixture


ROOT = Path(__file__).resolve().parents[1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decision_snapshot(report: dict[str, Any]) -> dict[str, Any]:
    regions = []
    for region in report["regions"]:
        regions.append(
            {
                "region_id": region["region_id"],
                "arena_id": region["arena_id"],
                "interval": region["interval"],
                "directions": {
                    kind: {
                        "status": value["status"],
                        "primary_business_code": value["primary_business_code"],
                        "business_codes": value["business_codes"],
                        "counts": value["counts"],
                        "evidence": value["evidence"],
                        "ledger": value["ledger"],
                    }
                    for kind, value in region["directions"].items()
                },
            }
        )
    return {
        "accesses": report["accesses"],
        "regions": regions,
        "rejections": report["rejections"],
        "sequence_diagnostics": report["sequence_diagnostics"],
        "topology_diagnostics": report["topology_diagnostics"],
        "ledger_rows": report["ledger"]["rows"],
        "truth": {
            "checked": report["truth"]["checked"],
            "ok": report["truth"]["ok"],
            "matched": report["truth"]["matched"],
            "mismatches": report["truth"]["mismatches"],
            "ledger_mismatches": report["truth"]["ledger_mismatches"],
        },
    }


@pytest.fixture()
def sample(tmp_path: Path) -> Path:
    out = tmp_path / "sample"
    generate_fixture(out, seed=424242)
    return out


@pytest.fixture()
def run_sample(
    tmp_path: Path, sample: Path
) -> Callable[..., tuple[Any, Path]]:
    counter = 0

    def execute(
        *,
        workers: int = 2,
        max_inflight_bytes: int = 10_000_000,
        arena_timeout_ms: float = 10_000.0,
        max_pending: int = 64,
        batch_size: int = 4,
        traces: list[Path] | None = None,
        markers: bool = True,
        name: str | None = None,
    ) -> tuple[Any, Path]:
        nonlocal counter
        counter += 1
        run_name = name or ("run-%d" % counter)
        report_path = tmp_path / ("%s-report.json" % run_name)
        result = run(
            RunOptions(
                region_paths=[sample / "regions.json"],
                trace_paths=traces or [sample / "traces.jsonl"],
                database_path=tmp_path / ("%s-ledger.sqlite" % run_name),
                snapshot_path=tmp_path / ("%s-snapshot.json" % run_name),
                report_path=report_path,
                truth_marker_paths=[sample / "sync_markers.json"] if markers else [],
                max_workers=workers,
                max_inflight_bytes=max_inflight_bytes,
                arena_timeout_ms=arena_timeout_ms,
                max_pending=max_pending,
                batch_size=batch_size,
            )
        )
        return result, report_path

    return execute


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )
