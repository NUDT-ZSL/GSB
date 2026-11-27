from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .compare import bbox_iou
from .model import Block, VersionAnalysis


def evaluate_page(
    analysis: VersionAnalysis,
    truth: dict[str, Any],
) -> dict[str, Any]:
    truth_blocks = {
        block["uid"]: {
            "uid": block["uid"],
            "type": block["type"],
            "bbox": tuple(float(value) for value in block["bbox"]),
        }
        for block in truth["blocks"]
    }
    matches = _match_blocks(analysis.blocks, truth_blocks)
    matched_truth = set(matches.values())
    correct_types = sum(
        1
        for uid, truth_uid in matches.items()
        if _block_by_uid(analysis.blocks, uid).block_type == truth_blocks[truth_uid]["type"]
    )
    matched_ious = [
        bbox_iou(_block_by_uid(analysis.blocks, uid).bbox, truth_blocks[truth_uid]["bbox"])
        for uid, truth_uid in matches.items()
    ]

    predicted_to_truth = dict(matches)
    predicted_order_truth = []
    for uid in analysis.order.order:
        truth_uid = predicted_to_truth.get(uid)
        if truth_uid is not None:
            predicted_order_truth.append(truth_uid)
    truth_order = [
        uid for uid in truth.get("reading_order", []) if uid in set(predicted_order_truth)
    ]
    kendall = kendall_tau_b(predicted_order_truth, truth_order)

    detected_conflicts = [conflict.to_dict() for conflict in analysis.order.conflicts]
    expected_conflicts = truth.get("expected_conflicts", [])
    matched_conflicts = _match_conflicts(detected_conflicts, expected_conflicts, predicted_to_truth)

    return {
        "page_id": analysis.page_id,
        "format": analysis.format_name,
        "predicted_block_count": len(analysis.blocks),
        "truth_block_count": len(truth_blocks),
        "matched_block_count": len(matches),
        "segmentation": {
            "mean_iou_matched": _mean(matched_ious),
            "mean_iou_all_truth": sum(matched_ious) / max(1, len(truth_blocks)),
            "precision_at_iou_0_5": sum(1 for value in matched_ious if value >= 0.5) / max(1, len(analysis.blocks)),
            "recall_at_iou_0_5": sum(1 for value in matched_ious if value >= 0.5) / max(1, len(truth_blocks)),
            "unmatched_predicted": len(analysis.blocks) - len(matches),
            "unmatched_truth": len(truth_blocks) - len(matches),
        },
        "type_classification": {
            "correct": correct_types,
            "accuracy": correct_types / max(1, len(truth_blocks)),
            "accuracy_matched": correct_types / max(1, len(matches)),
        },
        "reading_order": {
            "evaluated_block_count": len(predicted_order_truth),
            "kendall_tau_b": kendall,
            "predicted_truth_sequence": predicted_order_truth,
            "manual_truth_sequence": truth_order,
            "unresolved_blocks": analysis.order.unresolved,
        },
        "conflicts": {
            "expected_count": len(expected_conflicts),
            "detected_count": len(detected_conflicts),
            "matched_count": matched_conflicts,
        },
    }


def evaluate_analyses(
    analyses: dict[str, dict[str, VersionAnalysis]],
    truth_by_page: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    by_format: dict[str, list[dict[str, Any]]] = {}
    for page_id, page_versions in analyses.items():
        for format_name, analysis in page_versions.items():
            by_format.setdefault(format_name, []).append(
                evaluate_page(analysis, truth_by_page[page_id])
            )
    summaries = {
        format_name: _format_summary(page_metrics)
        for format_name, page_metrics in by_format.items()
    }
    aggregate = _format_summary(
        [metric for values in by_format.values() for metric in values]
    )
    return {
        "schema": "gsb-layout-metrics/v1",
        "primary_format": "lossless_png",
        "aggregate": aggregate,
        "by_format": summaries,
        "pages": {
            format_name: [
                {
                    "page_id": metric["page_id"],
                    "segmentation": metric["segmentation"],
                    "type_classification": metric["type_classification"],
                    "reading_order": metric["reading_order"],
                    "conflicts": metric["conflicts"],
                }
                for metric in values
            ]
            for format_name, values in by_format.items()
        },
        "detailed": by_format,
    }


def _format_summary(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    pages = len(metrics)
    return {
        "page_count": pages,
        "block_segmentation_iou": _mean(
            [metric["segmentation"]["mean_iou_all_truth"] for metric in metrics]
        ),
        "block_segmentation_iou_matched": _mean(
            [metric["segmentation"]["mean_iou_matched"] for metric in metrics]
        ),
        "type_classification_accuracy": _mean(
            [metric["type_classification"]["accuracy"] for metric in metrics]
        ),
        "kendall_rank_correlation": _mean(
            [metric["reading_order"]["kendall_tau_b"] for metric in metrics]
        ),
        "expected_conflicts": sum(metric["conflicts"]["expected_count"] for metric in metrics),
        "detected_conflicts": sum(metric["conflicts"]["detected_count"] for metric in metrics),
        "matched_conflicts": sum(metric["conflicts"]["matched_count"] for metric in metrics),
    }


def kendall_tau_b(predicted: list[str], truth: list[str]) -> float:
    common = [value for value in truth if value in predicted]
    predicted_ranks = {value: index for index, value in enumerate(predicted)}
    truth_ranks = {value: index for index, value in enumerate(truth)}
    if len(common) < 2:
        return 1.0
    concordant = 0
    discordant = 0
    for i in range(len(common)):
        for j in range(i + 1, len(common)):
            a, b = common[i], common[j]
            x = predicted_ranks[a] - predicted_ranks[b]
            y = truth_ranks[a] - truth_ranks[b]
            if x * y > 0:
                concordant += 1
            elif x * y < 0:
                discordant += 1
    denominator = len(common) * (len(common) - 1) / 2
    return (concordant - discordant) / denominator if denominator else 1.0


def _match_blocks(
    predicted: list[Block],
    truth_blocks: dict[str, dict[str, Any]],
) -> dict[str, str]:
    candidates: list[tuple[float, str, str]] = []
    for block in predicted:
        for truth_uid, truth_block in truth_blocks.items():
            candidates.append((bbox_iou(block.bbox, truth_block["bbox"]), block.uid, truth_uid))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    used_predicted: set[str] = set()
    used_truth: set[str] = set()
    matches: dict[str, str] = {}
    for iou, predicted_uid, truth_uid in candidates:
        if iou < 0.35 or predicted_uid in used_predicted or truth_uid in used_truth:
            continue
        matches[predicted_uid] = truth_uid
        used_predicted.add(predicted_uid)
        used_truth.add(truth_uid)
    return matches


def _match_conflicts(
    detected: list[dict[str, Any]],
    expected: list[dict[str, Any]],
    prediction_to_truth: dict[str, str],
) -> int:
    matched = 0
    for exp in expected:
        exp_involved = sorted(exp.get("involved", []))
        for det in detected:
            det_involved = sorted(
                prediction_to_truth.get(uid, uid) for uid in det.get("involved", [])
            )
            if exp.get("kind") == det.get("kind") and exp_involved == det_involved:
                matched += 1
                break
    return matched


def _block_by_uid(blocks: list[Block], uid: str) -> Block:
    for block in blocks:
        if block.uid == uid:
            return block
    raise KeyError(uid)


def _mean(values: list[float]) -> float:
    return sum(values) / max(1, len(values))


def load_truth(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
