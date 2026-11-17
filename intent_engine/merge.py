"""Merge dictionary slots and BIESO fragments with source priority."""

from __future__ import annotations

from typing import Any

from .utils import quantize, stable_hash


def _overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return left["start"] < right["end"] and right["start"] < left["end"]


def merge_results(
    slots_product: dict[str, Any],
    sequence_product: dict[str, Any],
    *,
    source_priority: list[str],
    digits: int,
) -> dict[str, Any]:
    priority = {source: index for index, source in enumerate(source_priority)}
    candidates: list[dict[str, Any]] = []
    for slot in slots_product.get("slots", []):
        candidate = dict(slot)
        candidate["source"] = "dictionary"
        candidates.append(candidate)
    for fragment in sequence_product.get("fragments", []):
        candidate = dict(fragment)
        candidate["source"] = "sequence"
        candidates.append(candidate)
    candidates.sort(
        key=lambda item: (
            priority.get(item.get("source", ""), 99),
            -int(item["end"] - item["start"]),
            int(item["start"]),
            item.get("type", ""),
            item.get("text", ""),
            item.get("source", ""),
        )
    )
    selected: list[dict[str, Any]] = []
    covered: list[dict[str, Any]] = []
    for candidate in candidates:
        overlapping = next((item for item in selected if _overlap(item, candidate)), None)
        if overlapping is not None:
            item = dict(candidate)
            item["covered_by"] = {
                "source": overlapping.get("source"),
                "type": overlapping.get("type"),
                "text": overlapping.get("text"),
                "start": overlapping.get("start"),
                "end": overlapping.get("end"),
            }
            if (
                item["start"] == overlapping["start"]
                and item["end"] == overlapping["end"]
                and item["text"] == overlapping["text"]
            ):
                item["reason"] = "same_span_lower_source_priority"
            else:
                item["reason"] = "overlap_lower_source_priority"
            covered.append(item)
            continue
        selected.append(dict(candidate))
    selected.sort(
        key=lambda item: (
            int(item["start"]),
            int(item["end"]),
            item.get("type", ""),
            item.get("text", ""),
            item.get("source", ""),
        )
    )
    normalized_selected: list[dict[str, Any]] = []
    for item in selected:
        item = dict(item)
        item["weight"] = quantize(float(item.get("weight", 1.0)), digits)
        item.pop("candidate_id", None)
        item.pop("priority", None)
        item.pop("base_weight", None)
        item.pop("length", None)
        normalized_selected.append(item)
    # This assertion is kept as an explicit invariant rather than relying on
    # the selection loop alone.
    for index, item in enumerate(normalized_selected):
        if index and normalized_selected[index - 1]["end"] > item["start"]:
            raise AssertionError("merged entities overlap")
    return {
        "slots": normalized_selected,
        "covered_fragments": sorted(
            covered,
            key=lambda item: (item["start"], item["end"], item.get("source", ""), item.get("type", "")),
        ),
        "source_priority": list(source_priority),
        "fingerprint": stable_hash(
            {"slots": normalized_selected, "covered": covered, "source_priority": source_priority}
        ),
    }

