"""Merge parsed masks (v1 / v2 / RLE) into one unified panoptic mask.

Invariants enforced here:
* every pixel carries at most one category and at most one instance
  (violation -> E_PIXEL_CONFLICT)
* an instance id maps to exactly one category inside one image
  (violation -> E_INSTANCE_DUP)
* ignore-region pixels are excluded from scoring
* the category/instance mapping-table version must match the config
  (violation -> E_VERSION_UNSUPPORTED)
"""
from __future__ import annotations

import dataclasses
import hashlib
import json

import numpy as np

from .config import IGNORE_CLASS, WorkbenchConfig
from .errors import (
    E_INSTANCE_DUP,
    E_PIXEL_CONFLICT,
    E_VERSION_UNSUPPORTED,
    PanoError,
)
from .formats import ParsedMask


@dataclasses.dataclass
class PanopticMask:
    width: int
    height: int
    mapping_version: int
    class_map: np.ndarray       # (H, W) uint8, IGNORE_CLASS on ignored pixels
    instance_map: np.ndarray    # (H, W) uint16, 0 = no instance
    conf_map: np.ndarray        # (H, W) float32
    ignore_mask: np.ndarray     # (H, W) bool
    sources: tuple              # versions that contributed, e.g. (1, 2, 3)

    def canonical_bytes(self) -> bytes:
        head = json.dumps(
            {
                "width": self.width,
                "height": self.height,
                "mapping_version": self.mapping_version,
                "sources": list(self.sources),
            },
            sort_keys=True,
        ).encode("utf-8")
        return b"|".join(
            [
                head,
                self.class_map.astype(np.uint8).tobytes(),
                self.instance_map.astype("<u2").tobytes(),
                self.conf_map.astype("<f4").tobytes(),
                self.ignore_mask.astype(np.uint8).tobytes(),
            ]
        )

    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def merge_masks(parsed_list: list[ParsedMask], cfg: WorkbenchConfig) -> PanopticMask:
    if not parsed_list:
        raise PanoError(E_VERSION_UNSUPPORTED, "no parsed masks to merge")

    for p in parsed_list:
        if p.mapping_version != cfg.mapping_version:
            raise PanoError(
                E_VERSION_UNSUPPORTED,
                "mapping-table version mismatch",
                expected=cfg.mapping_version, actual=p.mapping_version,
                source_version=p.version,
            )

    height = parsed_list[0].height
    width = parsed_list[0].width
    for p in parsed_list:
        if p.height != height or p.width != width:
            raise PanoError(
                E_PIXEL_CONFLICT, "source dimensions disagree",
                expected=(height, width), actual=(p.height, p.width),
                source_version=p.version,
            )

    cls_out = np.zeros((height, width), dtype=np.uint8)
    inst_out = np.zeros((height, width), dtype=np.uint16)
    conf_out = np.zeros((height, width), dtype=np.float32)
    ignore = np.zeros((height, width), dtype=bool)
    cls_owner = np.zeros((height, width), dtype=np.int8)   # source version or 0
    inst_owner = np.zeros((height, width), dtype=np.int8)
    instance_category: dict[int, int] = {}

    for p in parsed_list:
        ignore |= p.ignore_mask

    for p in parsed_list:
        scored = ~ignore
        # --- category agreement on scored pixels --------------------------
        provides_cls = scored & (p.class_map != IGNORE_CLASS)
        conflict = provides_cls & (cls_owner != 0) & (cls_out != p.class_map)
        if conflict.any():
            y, x = (int(v) for v in np.argwhere(conflict)[0])
            raise PanoError(
                E_PIXEL_CONFLICT, "pixel assigned two categories",
                x=x, y=y, existing=int(cls_out[y, x]),
                incoming=int(p.class_map[y, x]), source_version=p.version,
            )
        newly = provides_cls & (cls_owner == 0)
        cls_out[newly] = p.class_map[newly]
        cls_owner[provides_cls] = p.version

        if p.instance_map is None:
            continue
        # --- instance agreement on scored pixels --------------------------
        provides_inst = scored & (p.instance_map != 0)
        conflict = provides_inst & (inst_owner != 0) & (inst_out != p.instance_map)
        if conflict.any():
            y, x = (int(v) for v in np.argwhere(conflict)[0])
            raise PanoError(
                E_PIXEL_CONFLICT, "pixel assigned two instances",
                x=x, y=y, existing=int(inst_out[y, x]),
                incoming=int(p.instance_map[y, x]), source_version=p.version,
            )
        newly = provides_inst & (inst_owner == 0)
        inst_out[newly] = p.instance_map[newly]
        if p.conf_map is not None:
            conf_out[newly] = p.conf_map[newly]
        inst_owner[provides_inst] = p.version

        # --- instance id -> category uniqueness ---------------------------
        for iid in np.unique(p.instance_map[provides_inst]):
            iid = int(iid)
            cats = np.unique(p.class_map[p.instance_map == iid])
            cats = [int(c) for c in cats if c != IGNORE_CLASS]
            if len(cats) > 1:
                raise PanoError(
                    E_INSTANCE_DUP, "instance id spans multiple categories",
                    instance_id=iid, categories=cats,
                    source_version=p.version,
                )
            if not cats:
                continue
            cid = cats[0]
            if iid in instance_category and instance_category[iid] != cid:
                raise PanoError(
                    E_INSTANCE_DUP, "instance id reused with another category",
                    instance_id=iid, existing_category=instance_category[iid],
                    incoming_category=cid, source_version=p.version,
                )
            instance_category[iid] = cid

    cls_out[ignore] = IGNORE_CLASS
    inst_out[ignore] = 0
    conf_out[ignore] = 0.0

    return PanopticMask(
        width=width, height=height, mapping_version=cfg.mapping_version,
        class_map=cls_out, instance_map=inst_out, conf_map=conf_out,
        ignore_mask=ignore,
        sources=tuple(p.version for p in parsed_list),
    )
