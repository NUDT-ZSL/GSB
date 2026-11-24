"""Synchronization-marker truth comparison.

The detector never reads these expected fields. They are consumed only here,
after detection and ledger commit, to compare independent sample markers with
the produced ledger.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from .codes import (
    E_ACCESS_SEQ_DUPLICATE,
    E_INPUT_JSON,
    E_TRACE_SCHEMA,
    E_TRUTH_MISMATCH,
    OOB_CODES,
)
from .models import Rejection, TraceRecord, Verdict, nullable_interval


def _reject(path: str, line: Optional[int], code: str, message: str, detail: Optional[dict[str, Any]] = None) -> Rejection:
    return Rejection(path, line, code, message, detail or {})


def load_markers(paths: Iterable[Path]) -> tuple[dict[str, dict[str, Any]], list[Rejection]]:
    markers: dict[str, dict[str, Any]] = {}
    rejections: list[Rejection] = []
    for path in sorted((Path(item) for item in paths), key=lambda item: str(item)):
        try:
            document = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError) as exc:
            rejections.append(
                _reject(str(path), None, E_INPUT_JSON, "cannot parse truth marker file", {"error": str(exc)})
            )
            continue
        raw_markers = document.get("markers") if isinstance(document, dict) else document
        if isinstance(raw_markers, dict):
            raw_markers = [
                {"sync_marker": key, **value}
                for key, value in raw_markers.items()
                if isinstance(value, dict)
            ]
        if not isinstance(raw_markers, list):
            rejections.append(
                _reject(str(path), 1, E_TRACE_SCHEMA, "truth marker document must contain markers")
            )
            continue
        for index, value in enumerate(raw_markers, 1):
            if not isinstance(value, dict):
                rejections.append(
                    _reject(str(path), index, E_TRACE_SCHEMA, "truth marker must be an object")
                )
                continue
            marker = value.get("sync_marker")
            trace_id = value.get("trace_id")
            access_seq = value.get("access_seq")
            expected_code = value.get("expected_business_code")
            expected_interval = nullable_interval(value.get("expected_interval"))
            sender_interval = nullable_interval(value.get("sender_interval"))
            if (
                not isinstance(marker, str)
                or not marker
                or not isinstance(trace_id, str)
                or type(access_seq) is not int
                or not isinstance(expected_code, str)
            ):
                rejections.append(
                    _reject(
                        str(path),
                        index,
                        E_TRACE_SCHEMA,
                        "truth marker has invalid identity or expected code",
                    )
                )
                continue
            if marker in markers:
                rejections.append(
                    _reject(
                        str(path),
                        index,
                        E_TRACE_SCHEMA,
                        "duplicate truth sync_marker",
                        {"sync_marker": marker},
                    )
                )
                continue
            markers[marker] = {
                "sync_marker": marker,
                "trace_id": trace_id,
                "access_seq": access_seq,
                "expected_business_code": expected_code,
                "expected_interval": expected_interval,
                "sender_interval": sender_interval,
                "source_file": str(path),
                "line_number": index,
            }
    return markers, rejections


def compare_truth(
    markers: Mapping[str, Mapping[str, Any]],
    records: Sequence[TraceRecord],
    verdicts: Sequence[Verdict],
    rejections: Sequence[Rejection],
    ledger_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    record_by_trace = {record.trace_id: record for record in records}
    verdict_by_trace = {verdict.trace_id: verdict for verdict in verdicts}
    duplicate_rejections = {
        str(item.detail.get("duplicate_trace_id")): item
        for item in rejections
        if item.business_code == E_ACCESS_SEQ_DUPLICATE
    }
    mismatches: list[dict[str, Any]] = []
    missing: list[str] = []
    matched = 0
    expected_ledger: dict[tuple[str, str], dict[str, Any]] = {}

    for marker in sorted(markers):
        truth = markers[marker]
        trace_id = str(truth["trace_id"])
        expected_code = str(truth["expected_business_code"])
        expected_interval = truth["expected_interval"]
        sender_interval = truth["sender_interval"]
        if expected_code == E_ACCESS_SEQ_DUPLICATE:
            if trace_id in duplicate_rejections:
                matched += 1
            else:
                missing.append(marker)
            continue
        record = record_by_trace.get(trace_id)
        if record is None:
            missing.append(marker)
            continue

        reasons: list[str] = []
        if record.access_seq != int(truth["access_seq"]):
            reasons.append("trace access_seq differs from truth marker")
        if record.sync_marker != marker:
            reasons.append("trace sync_marker differs from truth marker")
        if sender_interval is not None and tuple(record.sender_interval or ()) != tuple(sender_interval):
            reasons.append("trace sender_interval differs from truth marker")
        if expected_interval is not None and tuple(record.expected_interval or ()) != tuple(expected_interval):
            reasons.append("trace expected_interval differs from truth marker")
        if record.expected_business_code != expected_code:
            reasons.append("trace expected_business_code differs from truth marker")

        verdict = verdict_by_trace.get(trace_id)
        if verdict is None:
            reasons.append("detector did not emit a verdict for the marked access")
        else:
            if verdict.business_code != expected_code:
                reasons.append("detected business_code differs from expected")
            actual_interval = verdict.evidence.get("allowed_intersection")
            if expected_interval is not None and actual_interval != list(expected_interval):
                reasons.append("normalized allowed_intersection differs from expected interval")

        if reasons:
            mismatches.append(
                {
                    "sync_marker": marker,
                    "trace_id": trace_id,
                    "access_seq": int(truth["access_seq"]),
                    "expected_business_code": expected_code,
                    "detected_business_code": None if verdict is None else verdict.business_code,
                    "reasons": reasons,
                }
            )
        else:
            matched += 1

        if verdict is not None:
            key = (verdict.region_id, verdict.access_kind)
            estimate = expected_ledger.setdefault(
                key,
                {
                    "access_count": 0,
                    "oob_count": 0,
                    "oob_upper_bound": None,
                },
            )
            estimate["access_count"] += 1
            if expected_code in OOB_CODES:
                estimate["oob_count"] += 1
                access_end = (
                    int(sender_interval[1])
                    if sender_interval is not None
                    else verdict.access_end
                )
                upper = estimate["oob_upper_bound"]
                estimate["oob_upper_bound"] = (
                    access_end if upper is None else max(int(upper), access_end)
                )

    ledger_by_key = {
        (str(row["region_id"]), str(row["access_kind"])): row for row in ledger_rows
    }
    ledger_mismatches: list[dict[str, Any]] = []
    for key in sorted(expected_ledger):
        expected = expected_ledger[key]
        actual = ledger_by_key.get(key)
        if actual is None:
            ledger_mismatches.append(
                {"region_id": key[0], "access_kind": key[1], "reason": "ledger row missing"}
            )
            continue
        comparable = {
            "access_count": int(actual["access_count"]),
            "oob_count": int(actual["oob_count"]),
            "oob_upper_bound": (
                None
                if actual.get("oob_upper_bound") is None
                else int(actual["oob_upper_bound"])
            ),
        }
        if comparable != expected:
            ledger_mismatches.append(
                {
                    "region_id": key[0],
                    "access_kind": key[1],
                    "expected": expected,
                    "actual": comparable,
                }
            )

    checked = bool(markers)
    return {
        "checked": checked,
        "matched": matched,
        "mismatched": len(mismatches),
        "missing": missing,
        "mismatches": mismatches,
        "ledger_mismatches": ledger_mismatches,
        "ok": checked and not mismatches and not missing and not ledger_mismatches,
        "business_code": (
            None
            if not checked
            else (
                E_TRUTH_MISMATCH
                if mismatches or missing or ledger_mismatches
                else "OK"
            )
        ),
    }
