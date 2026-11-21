"""Deterministic local sample generator.

Given a fixed seed and fixed size, produces:
* an RGB pixel array (uint8, HxWx3)
* ground truth as a v2-style triple (class / instance / confidence)
* a prediction derived from the ground truth by deterministic perturbation
* the prediction serialized in all three formats (v1 bytes, v2 bytes, RLE JSON)

No network, no external datasets: everything is a pure function of the config.
"""
from __future__ import annotations

import dataclasses

import numpy as np

from .config import IGNORE_CLASS, WorkbenchConfig
from .formats import encode_rle_json, encode_v1, encode_v2, rle_encode


@dataclasses.dataclass
class Sample:
    rgb: np.ndarray                # (H, W, 3) uint8
    gt_class: np.ndarray           # (H, W) uint8
    gt_instance: np.ndarray        # (H, W) uint16
    gt_conf: np.ndarray            # (H, W) float32
    gt_ignore: np.ndarray          # (H, W) bool
    pred_class: np.ndarray
    pred_instance: np.ndarray
    pred_conf: np.ndarray
    pred_ignore: np.ndarray
    pred_v1: bytes                 # semantic-only serialization
    pred_v2: bytes                 # class+instance+confidence serialization
    pred_rle: str                  # COCO-style exchange document
    categories: list[dict]


def _paint_instances(rng, cfg: WorkbenchConfig):
    """Paint deterministic elliptical instances; returns (cls, inst)."""
    h, w = cfg.height, cfg.width
    cls = np.zeros((h, w), dtype=np.uint8)
    inst = np.zeros((h, w), dtype=np.uint16)
    yy, xx = np.mgrid[0:h, 0:w]
    for iid in range(1, cfg.num_instances + 1):
        cid = int(rng.integers(1, cfg.num_categories + 1))
        cy = rng.uniform(0.2 * h, 0.8 * h)
        cx = rng.uniform(0.2 * w, 0.8 * w)
        ry = rng.uniform(0.08 * h, 0.22 * h)
        rx = rng.uniform(0.08 * w, 0.22 * w)
        blob = ((yy - cy) / ry) ** 2 + ((xx - cx) / rx) ** 2 <= 1.0
        cls[blob] = cid
        inst[blob] = iid
    return cls, inst


def generate_sample(cfg: WorkbenchConfig) -> Sample:
    rng = np.random.default_rng(cfg.seed)
    h, w = cfg.height, cfg.width

    rgb = rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)

    gt_cls, gt_inst = _paint_instances(rng, cfg)
    gt_conf = np.where(
        gt_inst > 0,
        np.round(rng.uniform(0.5, 1.0, size=(h, w)), 4),
        0.0,
    ).astype(np.float32)

    # deterministic ignore region: a fixed rectangle derived from the seed
    gt_ignore = np.zeros((h, w), dtype=bool)
    iy = int(rng.integers(0, h // 2))
    ix = int(rng.integers(0, w // 2))
    gt_ignore[iy:iy + max(2, h // 8), ix:ix + max(2, w // 8)] = True
    gt_cls[gt_ignore] = IGNORE_CLASS
    gt_inst[gt_ignore] = 0
    gt_conf[gt_ignore] = 0.0

    # --- prediction: deterministic perturbation of the ground truth ---------
    pred_cls = gt_cls.copy()
    pred_inst = gt_inst.copy()
    pred_conf = np.round(
        np.clip(gt_conf + rng.uniform(-0.05, 0.05, size=(h, w)), 0.0, 1.0), 4
    ).astype(np.float32)
    pred_conf[gt_inst == 0] = 0.0
    pred_ignore = gt_ignore.copy()

    # 1) shrink instance 1: drop pixels farthest from its centroid
    if (pred_inst == 1).any():
        ys, xs = np.where(pred_inst == 1)
        cy, cx = ys.mean(), xs.mean()
        dist2 = (ys - cy) ** 2 + (xs - cx) ** 2
        ring = np.zeros((h, w), dtype=bool)
        ring[ys[dist2 > 0.82 * dist2.max()], xs[dist2 > 0.82 * dist2.max()]] = True
        pred_cls[ring] = 0
        pred_inst[ring] = 0
        pred_conf[ring] = 0.0

    # 2) spurious instance (pure FP) with its own unique id; the center walks
    #    a deterministic offset sequence until free background is found
    spurious_id = int(pred_inst.max()) + 1
    yy, xx = np.mgrid[0:h, 0:w]
    spurious = np.zeros((h, w), dtype=bool)
    for step in range(8):
        sy = int((h * (3 + step)) % max(1, h - 4)) + 2
        sx = int((w * (5 + 2 * step)) % max(1, w - 4)) + 2
        cand = ((yy - sy) / 2.0) ** 2 + ((xx - sx) / 2.0) ** 2 <= 1.0
        cand &= (pred_inst == 0) & (~pred_ignore)
        if cand.sum() >= 4:
            spurious = cand
            break
    if spurious.any():
        spurious_cid = int(rng.integers(1, cfg.num_categories + 1))
        pred_cls[spurious] = spurious_cid
        pred_inst[spurious] = spurious_id
        pred_conf[spurious] = np.round(
            rng.uniform(0.4, 0.7, size=int(spurious.sum())), 4
        ).astype(np.float32)

    # --- serialize the prediction in all three formats ----------------------
    pred_v1 = encode_v1(pred_cls, cfg.mapping_version)
    pred_v2 = encode_v2(pred_cls, pred_inst, pred_conf, cfg.mapping_version)

    segments = []
    for iid in sorted(np.unique(pred_inst[pred_inst > 0])):
        m = pred_inst == iid
        conf_in_decode_order = pred_conf.flatten(order="F")[m.flatten(order="F")]
        segments.append(
            {
                "instance_id": int(iid),
                "category_id": int(pred_cls[m][0]),
                "rle": {"counts": rle_encode(m)},
                "confidence": [float(v) for v in conf_in_decode_order],
            }
        )
    categories = [
        {"id": c, "name": "background" if c == 0 else f"cat{c}"}
        for c in range(0, cfg.num_categories + 1)
    ]
    pred_rle = encode_rle_json(
        segments, pred_ignore, categories, w, h, cfg.mapping_version
    )

    return Sample(
        rgb=rgb,
        gt_class=gt_cls, gt_instance=gt_inst, gt_conf=gt_conf, gt_ignore=gt_ignore,
        pred_class=pred_cls, pred_instance=pred_inst, pred_conf=pred_conf,
        pred_ignore=pred_ignore,
        pred_v1=pred_v1, pred_v2=pred_v2, pred_rle=pred_rle,
        categories=categories,
    )
