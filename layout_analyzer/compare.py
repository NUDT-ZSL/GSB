from __future__ import annotations

from typing import Any

from .model import Block, VersionAnalysis


BOUNDARY_TOLERANCE_REF_PX = 8.0
MIN_MATCH_IOU = 0.20


def compare_versions(
    page_id: str,
    analyses: dict[str, VersionAnalysis],
    reference_format: str = "lossless_png",
) -> dict[str, Any]:
    base = analyses[reference_format]
    base_by_uid = {block.uid: block for block in base.blocks}
    base_conflicts = _conflict_signatures(base)
    version_entries: list[dict[str, Any]] = []
    all_differences: list[dict[str, Any]] = []
    all_conflict_differences: list[dict[str, Any]] = []
    all_consistent = True

    for format_name, analysis in analyses.items():
        if format_name == reference_format:
            continue
        matches = _InverseLookup(_greedy_matches(base.blocks, analysis.blocks))
        reverse_matches = {variant_uid: base_uid for base_uid, variant_uid in matches.items()}
        variant_by_uid = {block.uid: block for block in analysis.blocks}
        differences: list[dict[str, Any]] = []
        block_checks: list[dict[str, Any]] = []

        for base_block in base.blocks:
            variant_uid = matches.get(base_block.uid)
            if variant_uid is None:
                detail = {
                    "kind": "missing_block",
                    "flags": ["missing_block"],
                    "block_uid": base_block.uid,
                    "reference_format": reference_format,
                    "other_format": format_name,
                    "reference_value": _block_snapshot(base_block),
                    "other_value": None,
                }
                block_checks.append(detail)
                differences.append(detail)
                continue
            other = variant_by_uid[variant_uid]
            deltas = [abs(a - b) for a, b in zip(base_block.bbox, other.bbox)]
            iou = bbox_iou(base_block.bbox, other.bbox)
            boundary_exceeded = max(deltas) > BOUNDARY_TOLERANCE_REF_PX
            type_changed = base_block.block_type != other.block_type
            order_changed = base_block.order_index != other.order_index
            flags = []
            if boundary_exceeded:
                flags.append("boundary_deviation")
            if type_changed:
                flags.append("type_flip")
            if order_changed:
                flags.append("order_position_change")
            detail = {
                "kind": "block_check",
                "flags": flags,
                "block_uid": base_block.uid,
                "matched_uid_other": variant_uid,
                "reference_format": reference_format,
                "other_format": format_name,
                "bbox_max_delta_ref_px": round(max(deltas), 4),
                "bbox_deltas_ref_px": [round(value, 4) for value in deltas],
                "bbox_iou": round(iou, 5),
                "boundary_tolerance_ref_px": BOUNDARY_TOLERANCE_REF_PX,
                "reference_value": _block_snapshot(base_block),
                "other_value": _block_snapshot(other),
            }
            block_checks.append(detail)
            if boundary_exceeded or type_changed or order_changed:
                differences.append({**detail, "kind": "block_difference"})

        for variant_uid, block in variant_by_uid.items():
            if variant_uid not in reverse_matches:
                detail = {
                    "kind": "unexpected_block",
                    "flags": ["unexpected_block"],
                    "block_uid": variant_uid,
                    "reference_format": reference_format,
                    "other_format": format_name,
                    "reference_value": None,
                    "other_value": _block_snapshot(block),
                }
                block_checks.append(detail)
                differences.append(detail)

        variant_conflicts = []
        for signature in _conflict_signatures(analysis):
            canonical = {
                "kind": signature["kind"],
                "involved": sorted(
                    matches.get_inverse(uid, uid)
                    for uid in signature["involved"]
                ),
            }
            variant_conflicts.append(canonical)
        variant_conflict_set = {_signature_key(item) for item in variant_conflicts}
        base_conflict_set = {_signature_key(item) for item in base_conflicts}
        conflict_equal = variant_conflict_set == base_conflict_set
        if not conflict_equal:
            difference = {
                "kind": "conflict_signature_difference",
                "reference_format": reference_format,
                "other_format": format_name,
                "reference_value": sorted(base_conflict_set),
                "other_value": sorted(variant_conflict_set),
            }
            all_conflict_differences.append(difference)

        base_order_canonical = list(base.order.order)
        other_order_canonical = [
            matches.get_inverse(uid, uid)
            for uid in analysis.order.order
        ]
        order_sequence_equal = base_order_canonical == other_order_canonical
        if not order_sequence_equal:
            all_conflict_differences.append(
                {
                    "kind": "reading_order_sequence_difference",
                    "reference_format": reference_format,
                    "other_format": format_name,
                    "reference_value": base_order_canonical,
                    "other_value": other_order_canonical,
                }
            )
        consistent = (
            not differences
            and conflict_equal
            and order_sequence_equal
            and len(base.blocks) == len(analysis.blocks)
        )
        all_consistent = all_consistent and consistent
        all_differences.extend(differences)
        version_entries.append(
            {
                "format": format_name,
                "image_size": list(analysis.image_size),
                "block_count": len(analysis.blocks),
                "matched_block_count": len(matches),
                "canonical_order": other_order_canonical,
                "canonical_unresolved_blocks": sorted(
                    matches.get_inverse(uid, uid) for uid in analysis.order.unresolved
                ),
                "block_checks": block_checks,
                "boundary_tolerance_ref_px": BOUNDARY_TOLERANCE_REF_PX,
                "difference_count": len(differences),
                "conflict_signature_equal": conflict_equal,
                "reading_order_sequence_equal": order_sequence_equal,
                "consistent": consistent,
            }
        )

    return {
        "schema": "gsb-layout-cross-version/v1",
        "page_id": page_id,
        "reference_format": reference_format,
        "formats": [name for name in analyses],
        "consensus": {
            "all_versions_consistent": all_consistent,
            "block_count_equal": all(
                entry["block_count"] == len(base.blocks) for entry in version_entries
            ),
        },
        "versions": version_entries,
        "differences": all_differences,
        "structural_differences": all_conflict_differences,
        "reference_reading_order": list(base.order.order),
        "reference_unresolved_blocks": list(base.order.unresolved),
        "reference_conflicts": base_conflicts,
    }


def _greedy_matches(base: list[Block], other: list[Block]) -> dict[str, str]:
    candidates: list[tuple[float, float, str, str]] = []
    for base_block in base:
        for other_block in other:
            iou = bbox_iou(base_block.bbox, other_block.bbox)
            center_distance = abs(base_block.x_center - other_block.x_center) + abs(
                base_block.y_center - other_block.y_center
            )
            if iou >= MIN_MATCH_IOU:
                candidates.append((iou, -center_distance, base_block.uid, other_block.uid))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2], item[3]))
    used_base: set[str] = set()
    used_other: set[str] = set()
    matches: dict[str, str] = {}
    for _, _, base_uid, other_uid in candidates:
        if base_uid in used_base or other_uid in used_other:
            continue
        matches[base_uid] = other_uid
        used_base.add(base_uid)
        used_other.add(other_uid)
    # A type flip can lower IoU slightly; allow a centered one-to-one fallback.
    for base_block in base:
        if base_block.uid in used_base:
            continue
        fallback = sorted(
            (
                other_block
                for other_block in other
                if other_block.uid not in used_other
            ),
            key=lambda other_block: (
                abs(base_block.x_center - other_block.x_center)
                + abs(base_block.y_center - other_block.y_center)
            ),
        )
        if fallback:
            nearest = fallback[0]
            if (
                abs(base_block.x_center - nearest.x_center) <= max(12.0, base_block.width * 0.25)
                and abs(base_block.y_center - nearest.y_center) <= max(18.0, base_block.height * 0.20)
            ):
                matches[base_block.uid] = nearest.uid
                used_base.add(base_block.uid)
                used_other.add(nearest.uid)
    return matches


def _conflict_signatures(analysis: VersionAnalysis) -> list[dict[str, Any]]:
    signatures = []
    for conflict in analysis.order.conflicts:
        signatures.append(
            {
                "kind": conflict.kind,
                "involved": sorted(conflict.involved),
            }
        )
    return signatures


def _signature_key(signature: dict[str, Any]) -> str:
    return f"{signature['kind']}:" + ",".join(signature["involved"])


def _block_snapshot(block: Block) -> dict[str, Any]:
    return {
        "uid": block.uid,
        "bbox": [round(value, 3) for value in block.bbox],
        "type": block.block_type,
        "order_index": block.order_index,
    }


def bbox_iou(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    intersection_width = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    intersection_height = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    intersection = intersection_width * intersection_height
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


class _InverseLookup(dict):
    def get_inverse(self, value: str, default: str) -> str:
        for key, candidate in self.items():
            if candidate == value:
                return key
        return default
