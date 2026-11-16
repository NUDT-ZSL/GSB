"""Bidirectional v1 <-> v2 conversion with information-preservation policy."""

from __future__ import annotations

import math

from .model import RegionDesc

DEFAULT_COLOR_TABLE = {
    "rectangle": [255, 0, 0],
    "circle": [0, 200, 0],
    "triangle": [0, 0, 255],
}
DEFAULT_COLOR_NAMES = {
    "rectangle": "red",
    "circle": "green",
    "triangle": "blue",
}


def round_half_up(x: float) -> int:
    """Round to nearest integer, halves away from zero (non-negative domain)."""
    return math.floor(x + 0.5)


def clip_permille(v: int) -> int:
    return max(0, min(1000, v))


def ir_from_v1(rec: dict, sort_key=("", 0)) -> RegionDesc:
    bbox = tuple(int(v) for v in rec["bbox"])
    conf = clip_permille(int(rec["conf_permille"]))
    return RegionDesc(
        inst_id=rec["inst_id"], category=rec["category"], bbox=bbox,
        conf_permille=conf, source_version=1,
        orig_bbox=tuple(float(v) for v in bbox), orig_confidence=conf / 1000.0,
        relations=[], color_name=None, sort_key=sort_key,
    )


def ir_from_v2(inst: dict, sort_key=("", 0)) -> RegionDesc:
    orig_bbox = tuple(float(v) for v in inst["bbox"])
    orig_conf = float(inst["confidence"])
    bbox = tuple(round_half_up(v) for v in orig_bbox)
    conf = clip_permille(round_half_up(orig_conf * 1000.0))
    return RegionDesc(
        inst_id=inst["inst_id"], category=inst["category"], bbox=bbox,
        conf_permille=conf, source_version=2,
        orig_bbox=orig_bbox, orig_confidence=orig_conf,
        relations=list(inst.get("relations", [])),
        color_name=inst.get("color_name"), sort_key=sort_key,
    )


def upgrade_v1_to_v2(irs: list[RegionDesc]) -> tuple[list[dict], dict, dict]:
    """v1 -> v2: ints become floats; default named colors are generated;
    integer-origin markers go into sidecar metadata.

    Returns (v2_instances, color_table, sidecar_metadata).
    """
    instances, sidecar = [], {"integer_origin": {}, "source_version": 1}
    color_table = dict(DEFAULT_COLOR_TABLE)
    for ir in irs:
        color_name = DEFAULT_COLOR_NAMES.get(ir.category, "default")
        instances.append({
            "kind": "instance",
            "id": ir.inst_id,
            "category": ir.category,
            "bbox": [float(v) for v in ir.bbox],
            "confidence": ir.conf_permille / 1000.0,
            "relations": [],
            "color_name": color_name,
        })
        sidecar["integer_origin"][ir.inst_id] = {
            "bbox_was_integer": True,
            "confidence_was_permille": True,
        }
    return instances, color_table, sidecar


def downgrade_v2_to_v1(irs: list[RegionDesc]) -> tuple[list[dict], list[dict]]:
    """v2 -> v1: floats round half-up to ints; permille recomputed.

    Returns (v1_records, loss_ledger). The ledger is an auditable per-region
    account of rounding residuals and dropped relation references.
    """
    records, ledger = [], []
    for ir in irs:
        records.append({
            "inst_id": ir.inst_id, "category": ir.category,
            "bbox": list(ir.bbox), "conf_permille": ir.conf_permille,
        })
        orig_bbox = ir.orig_bbox or tuple(float(v) for v in ir.bbox)
        orig_conf = ir.orig_confidence
        coord_residuals = [orig_bbox[i] - ir.bbox[i] for i in range(4)]
        conf_residual = (orig_conf - ir.conf_permille / 1000.0
                         ) if orig_conf is not None else 0.0
        ledger.append({
            "inst_id": ir.inst_id,
            "coord_residuals": coord_residuals,
            "conf_residual": conf_residual,
            "dropped_relations": list(ir.relations),
            "dropped_color_name": ir.color_name,
            "lossless": (all(r == 0 for r in coord_residuals)
                         and conf_residual == 0.0 and not ir.relations),
        })
    return records, ledger


def roundtrip_v1_v2_v1(irs_v1: list[RegionDesc]) -> bool:
    """v1 -> v2 -> v1 must restore the original key fields exactly."""
    insts, _colors, _sidecar = upgrade_v1_to_v2(irs_v1)
    back = [ir_from_v2({
        "inst_id": i["id"], "category": i["category"], "bbox": i["bbox"],
        "confidence": i["confidence"], "relations": i["relations"],
        "color_name": i["color_name"],
    }) for i in insts]
    return [ir.key_fields() for ir in back] == [ir.key_fields() for ir in irs_v1]


def roundtrip_v2_v1_v2(irs_v2: list[RegionDesc]) -> bool:
    """v2 -> v1 -> v2 must restore the quantized key fields exactly."""
    records, _ledger = downgrade_v2_to_v1(irs_v2)
    back = [ir_from_v1(r) for r in records]
    return [ir.key_fields() for ir in back] == [ir.key_fields() for ir in irs_v2]
