"""Independent layout-rule checker.

This module intentionally imports no ordering, segmentation, or classification
code. Its public entry point accepts only a final page report and a plain block
feature table, exactly as required by the independent-check contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


POSITION_TOLERANCE_REF_PX = 3.0


def verify_from_files(
    report_path: str | Path,
    features_path: str | Path,
) -> dict[str, Any]:
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    features = json.loads(Path(features_path).read_text(encoding="utf-8"))
    return verify(report=report, features=features)


def verify(report: dict[str, Any], features: dict[str, Any]) -> dict[str, Any]:
    block_features = {
        block["uid"]: block for block in features.get("blocks", [])
    }
    sequence_uids = [
        item["uid"] if isinstance(item, dict) else item
        for item in report.get("reading_order", [])
    ]
    ranks = {uid: index for index, uid in enumerate(sequence_uids)}
    unresolved = set(report.get("conflict_section", {}).get("unresolved_blocks", []))
    recorded_conflicts = report.get("conflict_section", {}).get("conflicts", [])
    violations: list[dict[str, Any]] = []

    body_uids = [
        uid
        for uid in sequence_uids
        if block_features.get(uid, {}).get("type") == "body_column"
    ]
    for left_index in range(len(body_uids)):
        for right_index in range(left_index + 1, len(body_uids)):
            earlier = body_uids[left_index]
            later = body_uids[right_index]
            x_earlier = float(block_features[earlier]["x_center"])
            x_later = float(block_features[later]["x_center"])
            if x_later > x_earlier - POSITION_TOLERANCE_REF_PX:
                violations.append(
                    {
                        "rule": "vertical_columns_right_to_left",
                        "pair": [earlier, later],
                        "message": "a later body column is not to the left of the earlier one",
                        "values": {
                            "earlier_x_center": x_earlier,
                            "later_x_center": x_later,
                            "required": "later_x_center < earlier_x_center",
                        },
                    }
                )

    all_bodies = [
        block for block in block_features.values() if block.get("type") == "body_column"
    ]
    for uid in sequence_uids:
        block = block_features.get(uid, {})
        if block.get("type") != "inline_annotation":
            continue
        if not all_bodies:
            violations.append(
                {
                    "rule": "annotation_follows_anchor_column",
                    "pair": [uid],
                    "message": "annotation has no body column capable of anchoring it",
                }
            )
            continue
        candidates = _anchor_candidates(block, all_bodies)
        best_score = candidates[0]["score"]
        tied = [
            candidate
            for candidate in candidates
            if abs(candidate["score"] - best_score) <= POSITION_TOLERANCE_REF_PX
        ]
        if len(tied) > 1:
            violations.append(
                {
                    "rule": "annotation_has_unique_anchor",
                    "pair": [uid] + [candidate["uid"] for candidate in tied],
                    "message": "annotation anchor is tied but the block was not recorded as unresolved",
                    "values": tied,
                }
            )
            continue
        anchor = tied[0]["uid"]
        anchor_rank = ranks.get(anchor)
        annotation_rank = ranks.get(uid)
        if anchor_rank is None or annotation_rank is None:
            violations.append(
                {
                    "rule": "annotation_follows_anchor_column",
                    "pair": [anchor, uid],
                    "message": "annotation or its anchor is absent from the reading sequence",
                }
            )
        elif annotation_rank != anchor_rank + 1:
            violations.append(
                {
                    "rule": "annotation_follows_anchor_column",
                    "pair": [anchor, uid],
                    "message": "annotation is not immediately after its anchor body column",
                    "values": {
                        "anchor_order_index": anchor_rank,
                        "annotation_order_index": annotation_rank,
                    },
                }
            )

    for uid in block_features:
        block = block_features[uid]
        if block.get("type") == "inline_annotation" and uid not in ranks and uid not in unresolved:
            violations.append(
                {
                    "rule": "annotation_follows_anchor_column",
                    "pair": [uid],
                    "message": "annotation is missing from the final reading sequence without a recorded conflict",
                }
            )

    if all_bodies:
        body_top = min(float(block["bbox"][1]) for block in all_bodies)
        body_bottom = max(float(block["bbox"][3]) for block in all_bodies)
        body_ranks = [ranks[uid] for uid in body_uids if uid in ranks]
        first_body_rank = min(body_ranks) if body_ranks else None
        last_body_rank = max(body_ranks) if body_ranks else None
        for index, uid in enumerate(sequence_uids):
            block = block_features.get(uid, {})
            if block.get("type") != "header_note":
                continue
            bbox_bottom = float(block["bbox"][3])
            bbox_top = float(block["bbox"][1])
            if bbox_bottom <= body_top + POSITION_TOLERANCE_REF_PX:
                if first_body_rank is not None and index > first_body_rank:
                    violations.append(
                        {
                            "rule": "header_above_body_reads_before",
                            "pair": [uid, body_uids[0]],
                            "message": "header note lies above the body but follows a body column",
                            "values": {
                                "header_order_index": index,
                                "first_body_order_index": first_body_rank,
                            },
                        }
                    )
            elif bbox_top >= body_bottom - POSITION_TOLERANCE_REF_PX:
                if last_body_rank is not None and index < last_body_rank:
                    violations.append(
                        {
                            "rule": "header_below_body_reads_after",
                            "pair": [body_uids[-1], uid],
                            "message": "header note lies below the body but precedes a body column",
                            "values": {
                                "header_order_index": index,
                                "last_body_order_index": last_body_rank,
                            },
                        }
                    )
            else:
                violations.append(
                    {
                        "rule": "header_position_is_separable",
                        "pair": [uid],
                        "message": "header vertically overlaps the body span",
                        "values": {
                            "header_bbox": block.get("bbox"),
                            "body_top": body_top,
                            "body_bottom": body_bottom,
                        },
                    }
                )

    return {
        "schema": "gsb-layout-rule-check/v1",
        "page_id": report.get("page_id"),
        "format": report.get("format"),
        "ok": not violations,
        "checked_sequence_length": len(sequence_uids),
        "violation_count": len(violations),
        "violations": violations,
        "recorded_conflicts": recorded_conflicts,
        "unresolved_blocks": sorted(unresolved),
        "input_contract": {
            "report_schema": report.get("schema"),
            "features_schema": features.get("schema"),
            "ordering_code_reused": False,
        },
    }


def _anchor_candidates(
    annotation: dict[str, Any],
    bodies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    ax0, _, ax1, _ = [float(value) for value in annotation["bbox"]]
    candidates = []
    for body in bodies:
        bx0, _, bx1, _ = [float(value) for value in body["bbox"]]
        overlap = max(0.0, min(ax1, bx1) - max(ax0, bx0))
        gap = max(0.0, bx0 - ax1, ax0 - bx1)
        score = overlap if overlap > 0 else -gap
        candidates.append(
            {
                "uid": body["uid"],
                "score": round(score, 4),
                "x_overlap_px": round(overlap, 4),
                "horizontal_gap_px": round(gap, 4),
            }
        )
    candidates.sort(key=lambda item: (-item["score"], item["uid"]))
    return candidates
