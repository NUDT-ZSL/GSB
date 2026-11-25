from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import AuditError, ErrorCode
from .hashing import canonical_checksum, file_checksum
from .ledger import build_unified_ledger
from .parsers import parse_rle, parse_v1, parse_v2
from .parsers.formats import iter_source_checksums
from .pngio import read_rgb_png
from .probes import Probe
from .store import LedgerStore


def _first_probe_at(accepted: list[Probe], x: int, y: int) -> str | None:
    for probe in accepted:
        if probe.x == x and probe.y == y:
            return probe.probe_id
    return None


def cross_module_check(
    *,
    sample_dir: str | Path,
    manifest: dict[str, Any],
    format_priority: list[str],
    accepted: list[Probe],
    store: LedgerStore,
    report: dict[str, Any],
) -> dict[str, Any]:
    sample_dir = Path(sample_dir)
    rgb = read_rgb_png(sample_dir / "rgb.png")
    if rgb.shape[0] != int(manifest["height"]) or rgb.shape[1] != int(manifest["width"]) or rgb.shape[2] != 3:
        raise AuditError(ErrorCode.INCONSISTENT, "RGB image dimensions do not match sample manifest", {"shape": list(rgb.shape)})
    entities = {(int(e["category_id"]), int(e["instance_id"])) for e in manifest["entities"]}
    truths = {
        "v1": parse_v1(sample_dir / "gt_v1.json", entities),
        "v2": parse_v2(sample_dir / "gt_v2"),
        "RLE": parse_rle(sample_dir / "gt_rle.json", entities),
    }
    fresh_ledger = build_unified_ledger(truths, format_priority)
    store.verify()

    for rel_path, declared_checksum in manifest.get("files", {}).items():
        actual_checksum = file_checksum(sample_dir / rel_path)
        if actual_checksum != declared_checksum:
            raise AuditError(
                ErrorCode.INCONSISTENT,
                "manifest file checksum inconsistency",
                {"file": rel_path, "declared": declared_checksum, "actual": actual_checksum},
            )

    fresh_sources = sorted(iter_source_checksums(truths.values()), key=lambda item: (item["format"], item["block_id"]))
    stored_sources = sorted(store.source_checksums(), key=lambda item: (item["format"], item["block_id"]))
    if fresh_sources != stored_sources:
        first = next(
            (item for item in fresh_sources if item not in stored_sources),
            fresh_sources[0] if fresh_sources else None,
        )
        raise AuditError(ErrorCode.INCONSISTENT, "ledger source checksum declarations are inconsistent", {"first_block": first})

    pixel_rows = store.conn.execute(
        "SELECT coordinate, y, x, checksum, vote_json FROM pixel_ledger ORDER BY y, x"
    ).fetchall()
    if len(pixel_rows) != fresh_ledger.height * fresh_ledger.width:
        raise AuditError(ErrorCode.INCONSISTENT, "pixel ledger row count mismatch", {})

    accepted_by_uid = {probe.uid: probe for probe in accepted}
    for row in pixel_rows:
        x, y = int(row["x"]), int(row["y"])
        fresh_pixel = fresh_ledger.pixel_at(x, y)
        if row["checksum"] != fresh_pixel["pixel_checksum"]:
            probe_id = _first_probe_at(accepted, x, y)
            raise AuditError(
                ErrorCode.INCONSISTENT,
                "adjudicated pixel cannot be traced to source formats",
                {
                    "first_deviation_coordinate": {"x": x, "y": y},
                    "first_deviation_probe_id": probe_id,
                    "coordinate": row["coordinate"],
                },
            )

        stored_pixel = json.loads(row["vote_json"])
        for fmt, truth in truths.items():
            raw = truth.raw_at(x, y)
            if raw["x"] != x or raw["y"] != y:
                raise AuditError(ErrorCode.INCONSISTENT, "raw source trace coordinate mismatch", {"format": fmt, "x": x, "y": y})
            if int(truth.category[y, x]) != stored_pixel["votes"][[v["format"] for v in stored_pixel["votes"]].index(fmt)]["semantic_key"][1]:
                probe_id = _first_probe_at(accepted, x, y)
                raise AuditError(
                    ErrorCode.INCONSISTENT,
                    "source field does not match traced vote",
                    {"first_deviation_probe_id": probe_id, "coordinate": {"x": x, "y": y}, "format": fmt},
                )

    stored_records = store.load_probe_records()
    report_probes = report.get("probes", [])
    if len(report_probes) != len(stored_records):
        raise AuditError(
            ErrorCode.INCONSISTENT,
            "audit report probe count does not match committed probe ledger",
            {"report_count": len(report_probes), "ledger_count": len(stored_records), "first_deviation_probe_id": stored_records[0]["probe_id"] if stored_records else None},
        )

    progress = store.load_progress()
    if progress["confirmed_uids"] != [record["uid"] for record in stored_records]:
        raise AuditError(
            ErrorCode.INCONSISTENT,
            "progress ledger confirmed probes do not match probe ledger order",
            {"first_deviation_probe_id": stored_records[0]["probe_id"] if stored_records else None},
        )

    for index, (report_entry, stored_record) in enumerate(zip(report_probes, stored_records, strict=True)):
        probe = accepted_by_uid.get(stored_record["uid"])
        if report_entry.get("uid") != stored_record["uid"]:
            raise AuditError(
                ErrorCode.INCONSISTENT,
                "audit report probe order deviates from ledger",
                {"index": index, "first_deviation_probe_id": stored_record.get("probe_id")},
            )
        if report_entry.get("record_checksum") != stored_record.get("record_checksum"):
            raise AuditError(
                ErrorCode.INCONSISTENT,
                "audit report checksum does not correspond to committed probe",
                {"first_deviation_probe_id": stored_record.get("probe_id")},
            )
        if report_entry.get("business_code") != stored_record.get("business_code"):
            raise AuditError(
                ErrorCode.INCONSISTENT,
                "audit report business code deviates from ledger",
                {"first_deviation_probe_id": stored_record.get("probe_id")},
            )
        if probe is not None:
            raw_truth = report_entry.get("raw_truth", {})
            if set(raw_truth) != set(format_priority):
                raise AuditError(
                    ErrorCode.INCONSISTENT,
                    "audit report is missing one or more tri-state raw fields",
                    {"first_deviation_probe_id": probe.probe_id, "formats": sorted(raw_truth)},
                )

    report_checksum = report.get("report_checksum")
    report_body = {key: value for key, value in report.items() if key != "report_checksum"}
    if report_checksum != canonical_checksum(report_body):
        raise AuditError(ErrorCode.INCONSISTENT, "audit report checksum verification failed", {})

    return {
        "status": "PASS",
        "checked_pixels": len(pixel_rows),
        "checked_probes": len(stored_records),
        "checked_source_blocks": len(stored_sources),
        "first_deviation": None,
    }
