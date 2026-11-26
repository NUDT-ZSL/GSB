from __future__ import annotations

from typing import Any

from .config import AuditConfig, DEFAULT_CONFIG
from .codes import E_QUERY_MISMATCH


class QueryIndex:
    def __init__(self, result: dict[str, Any], config: AuditConfig = DEFAULT_CONFIG):
        self.result = result
        self.config = config
        self.turns = list(result["classified_turns"])
        self.segments = list(result["segments"])
        self.assignments = dict(result.get("assignments") or {})
        self.ledger_rows = list(result.get("ledger_rows") or [])
        self.ledger_by_event = {
            row["common"]["event_id"]: row for row in self.ledger_rows
        }

    def _turn_summary(self, turn: dict[str, Any]) -> dict[str, Any]:
        return {
            "event_id": turn["event_id"],
            "ordinal": turn["ordinal"],
            "kind": turn["kind"],
            "at_ms": turn["at_ms"],
            "frame_index": turn["frame_index"],
            "business_code": turn["business_code"],
            "anomaly_type": turn["anomaly_type"],
        }

    def at_ms(self, at_ms: int | str) -> dict[str, Any]:
        if isinstance(at_ms, str):
            if not at_ms.lstrip("-").isdigit():
                raise ValueError("at_ms must be an integer millisecond value")
            at_ms_int = int(at_ms)
        elif isinstance(at_ms, float):
            if not at_ms.is_integer():
                raise ValueError("at_ms must be an integer millisecond value; no implicit rounding is allowed")
            at_ms_int = int(at_ms)
        else:
            at_ms_int = int(at_ms)
        if at_ms_int < 0:
            raise ValueError("at_ms must be non-negative")

        exact_sample_index = at_ms_int * self.config.samples_per_ms
        latest: dict[str, Any] | None = None
        for turn in self.turns:
            if int(turn["at_ms"]) <= at_ms_int:
                latest = turn
            else:
                break

        nearest: dict[str, Any] | None = None
        nearest_delta: int | None = None
        for turn in self.turns:
            delta = abs(int(turn["at_ms"]) - at_ms_int)
            if nearest_delta is None or delta < nearest_delta:
                nearest_delta = delta
                nearest = turn

        active_segment = None
        for segment in self.segments:
            if int(segment["start_ms"]) <= at_ms_int < int(segment["end_ms"]):
                active_segment = segment
                break

        assignment = (
            self.assignments.get(active_segment["segment_id"]) if active_segment else None
        )
        ledger = None
        if latest is not None:
            row = self.ledger_by_event.get(latest["event_id"])
            ledger = row["common"] if row else None

        return {
            "query_type": "at_ms",
            "query_ms": at_ms_int,
            "exact_sample_index": exact_sample_index,
            "frame_grid_ms": self.config.hop_ms,
            "precision_ms": 0,
            "latest_turn": self._turn_summary(latest) if latest else None,
            "nearest_turn": (
                {
                    **self._turn_summary(nearest),
                    "distance_ms": nearest_delta,
                }
                if nearest
                else None
            ),
            "active_segment_id": active_segment["segment_id"] if active_segment else None,
            "active_segment_no": active_segment["segment_no"] if active_segment else None,
            "speaker_id": assignment["speaker_id"] if assignment else None,
            "identity_status": assignment["status"] if assignment else "silence",
            "business_code": latest["business_code"] if latest else "BIZ_NO_TURN_YET",
            "ledger_ref": ledger,
            "deterministic": True,
        }

    def segment(self, segment_ref: str | int) -> dict[str, Any]:
        segment = None
        if isinstance(segment_ref, int) or str(segment_ref).isdigit():
            number = int(segment_ref)
            for item in self.segments:
                if int(item["segment_no"]) == number:
                    segment = item
                    break
        else:
            for item in self.segments:
                if item["segment_id"] == segment_ref:
                    segment = item
                    break
        if segment is None:
            raise KeyError(f"segment not found: {segment_ref}")

        assignment = self.assignments.get(segment["segment_id"])
        ledger_rows = []
        for turn in self.turns:
            if int(turn["at_ms"]) == int(segment["start_ms"]):
                row = self.ledger_by_event.get(turn["event_id"])
                if row:
                    ledger_rows.append(row["common"])
        return {
            "query_type": "segment",
            "segment_id": segment["segment_id"],
            "segment_no": segment["segment_no"],
            "start_ms": segment["start_ms"],
            "end_ms": segment["end_ms"],
            "speaker_id": assignment["speaker_id"] if assignment else None,
            "identity_status": assignment["status"] if assignment else None,
            "business_code": ledger_rows[0]["business_code"] if ledger_rows else None,
            "ledger_refs": ledger_rows,
            "deterministic": True,
        }


def check_query_batch_consistency(
    result: dict[str, Any], config: AuditConfig = DEFAULT_CONFIG
) -> dict[str, Any]:
    index = QueryIndex(result, config)
    checks: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []

    for turn in result["classified_turns"]:
        at_ms = int(turn["at_ms"])
        response = index.at_ms(at_ms)
        expected_speaker = None
        for segment in result["segments"]:
            if int(segment["start_ms"]) <= at_ms < int(segment["end_ms"]):
                expected_speaker = result["assignments"].get(segment["segment_id"], {}).get(
                    "speaker_id"
                )
                break
        ok = (
            response["latest_turn"]["event_id"] == turn["event_id"]
            and response["business_code"] == turn["business_code"]
            and response["speaker_id"] == expected_speaker
        )
        checks.append({"at_ms": at_ms, "event_id": turn["event_id"], "ok": ok})
        if not ok:
            mismatches.append({"at_ms": at_ms, "response": response})

    for segment in result["segments"]:
        response = index.segment(segment["segment_id"])
        expected = result["assignments"][segment["segment_id"]]["speaker_id"]
        if response["speaker_id"] != expected:
            mismatches.append(
                {"segment_id": segment["segment_id"], "expected_speaker": expected}
            )

    return {
        "ok": not mismatches,
        "code": None if not mismatches else E_QUERY_MISMATCH,
        "checked_turns": len(result["classified_turns"]),
        "checked_segments": len(result["segments"]),
        "mismatches": mismatches,
        "checks": checks,
    }


# Explicit domain-language alias for the real-time query interface.
RealtimeQueryService = QueryIndex
