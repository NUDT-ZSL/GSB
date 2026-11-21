"""Span-level slot extraction and deterministic overlap resolution."""

from __future__ import annotations

import re
from typing import Any

from .taxonomy import Taxonomy


def _normalize_regex_value(raw: str, mode: str) -> str:
    if mode == "identity":
        return raw
    if mode == "uppercase":
        return raw.upper()
    if mode == "strip_yuan":
        cleaned = raw.replace("元", "").replace(",", "").strip()
        try:
            return str(int(cleaned))
        except ValueError:
            return cleaned
    if mode == "date":
        return raw
    return raw


def _deduplicate(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[str, int, int, str], dict[str, Any]] = {}
    for candidate in candidates:
        key = (
            candidate["slot_name"],
            candidate["start"],
            candidate["end"],
            candidate["normalized_value"],
        )
        previous = best.get(key)
        if previous is None or (
            candidate["confidence"],
            candidate["match_method"],
            -candidate["start"],
        ) > (
            previous["confidence"],
            previous["match_method"],
            -previous["start"],
        ):
            best[key] = candidate
    return list(best.values())


def resolve_non_overlapping(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(
        _deduplicate(candidates),
        key=lambda row: (
            -row["confidence"],
            row["slot_name"],
            -(row["end"] - row["start"]),
            row["start"],
            row["end"],
            row["normalized_value"],
        ),
    )
    selected: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []
    selected_slot_names: set[str] = set()
    for candidate in ranked:
        if candidate["slot_name"] in selected_slot_names:
            continue
        if any(
            candidate["start"] < end and start < candidate["end"]
            for start, end in occupied
        ):
            continue
        selected.append(candidate)
        selected_slot_names.add(candidate["slot_name"])
        occupied.append((candidate["start"], candidate["end"]))
    selected.sort(key=lambda row: (row["start"], row["end"], row["slot_name"]))
    for index, row in enumerate(selected):
        row["slot_id"] = f"s{index:04d}"
    return selected


def extract_slots(
    normalized_text: str,
    evidence: list[dict[str, Any]],
    domain: str,
    intent_class: str,
    taxonomy: Taxonomy,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    intent = taxonomy.require_intent(domain, intent_class)
    schema_by_name = {slot.name: slot for slot in intent.slot_schema}
    candidates: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    directional_pairs = [
        ("city_from", "city_to"),
        ("origin", "destination"),
    ]
    directional_pair = next(
        (
            pair
            for pair in directional_pairs
            if pair[0] in schema_by_name and pair[1] in schema_by_name
        ),
        None,
    )
    directional_rows = []
    if directional_pair is not None:
        directional_rows = sorted(
            [
                row
                for row in evidence
                if row["domain"] == domain
                and row["intent_class"] == intent_class
                and row.get("slot") in directional_pair
            ],
            key=lambda row: (row["start"], row["end"], row["entry_id"]),
        )
    directional_role: dict[tuple[int, int], str] = {}
    if directional_pair is not None and directional_rows:
        marker_start = -1
        marker_end = -1
        for marker in ("到", "至"):
            marker_index = normalized_text.find(marker)
            if marker_index >= 0:
                marker_start = marker_index
                marker_end = marker_index + len(marker)
                break
        distinct_spans = []
        seen_spans: set[tuple[int, int]] = set()
        for row in directional_rows:
            span = (int(row["start"]), int(row["end"]))
            if span not in seen_spans:
                seen_spans.add(span)
                distinct_spans.append(span)
        if marker_start >= 0:
            for span in distinct_spans:
                if span[1] <= marker_start:
                    directional_role[span] = directional_pair[0]
                elif span[0] >= marker_end:
                    directional_role[span] = directional_pair[1]
        else:
            for index, span in enumerate(distinct_spans):
                directional_role[span] = directional_pair[0] if index == 0 else directional_pair[1]

    for row in evidence:
        slot_name = row.get("slot")
        if (
            row["domain"] != domain
            or row["intent_class"] != intent_class
            or slot_name is None
        ):
            continue
        if slot_name not in schema_by_name:
            dropped.append(
                {
                    "slot_name": slot_name,
                    "start": row["start"],
                    "end": row["end"],
                    "reason": "slot_not_in_selected_intent_schema",
                }
            )
            continue
        span = (int(row["start"]), int(row["end"]))
        if directional_pair is not None and span in directional_role:
            slot_name = directional_role[span]
        base = 0.96 if row["method"] == "exact" else 0.80
        confidence = min(0.99, base * float(row["similarity"]) * float(row["weight"]))
        candidates.append(
            {
                "slot_name": slot_name,
                "start": int(row["start"]),
                "end": int(row["end"]),
                "text": normalized_text[int(row["start"]) : int(row["end"])],
                "normalized_value": str(row["canonical_value"]),
                "confidence": round(confidence, 6),
                "match_method": row["method"],
                "source_entry_id": row["entry_id"],
            }
        )

    for slot in intent.slot_schema:
        if not slot.regex:
            continue
        for match in re.finditer(slot.regex, normalized_text):
            value = _normalize_regex_value(match.group(0), slot.normalize)
            candidates.append(
                {
                    "slot_name": slot.name,
                    "start": match.start(),
                    "end": match.end(),
                    "text": match.group(0),
                    "normalized_value": value,
                    "confidence": 0.90,
                    "match_method": "regex",
                    "source_entry_id": None,
                }
            )

    resolved = resolve_non_overlapping(candidates)
    return resolved, dropped

