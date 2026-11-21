import dataclasses

import numpy as np
import pytest

from pano_workbench.config import WorkbenchConfig
from pano_workbench.merge import PanopticMask
from pano_workbench.metrics import evaluate, recompute_from_details


def make_mask(cls, inst, ignore=None):
    cls = np.asarray(cls, dtype=np.uint8)
    inst = np.asarray(inst, dtype=np.uint16)
    h, w = cls.shape
    if ignore is None:
        ignore = np.zeros((h, w), dtype=bool)
    conf = np.where(inst > 0, 0.9, 0.0).astype(np.float32)
    return PanopticMask(
        width=w, height=h, mapping_version=1,
        class_map=cls, instance_map=inst, conf_map=conf,
        ignore_mask=ignore, sources=(2,),
    )


def cfg1(**kw):
    base = dict(num_categories=1, snapshot_dir="s", cache_dir="c")
    base.update(kw)
    return WorkbenchConfig(**base)


def test_miou_and_pq_known_values():
    cfg = cfg1()
    gt_cls = np.zeros((4, 4), dtype=np.uint8)
    gt_cls[0:2, 0:2] = 1
    gt_inst = (gt_cls == 1).astype(np.uint16)
    pr_cls = np.zeros((4, 4), dtype=np.uint8)
    pr_cls[1:3, 1:3] = 1
    pr_inst = (pr_cls == 1).astype(np.uint16)
    rep = evaluate(make_mask(pr_cls, pr_inst), make_mask(gt_cls, gt_inst), cfg)
    assert rep.per_category[1].iou == pytest.approx(1 / 7)
    assert rep.per_category[0].iou == pytest.approx(9 / 15)
    assert rep.miou == pytest.approx((1 / 7 + 9 / 15) / 2)
    # instance IoU 1/7 < 0.5 -> no match
    assert (rep.per_category[1].tp, rep.per_category[1].fp,
            rep.per_category[1].fn) == (0, 1, 1)
    assert rep.per_category[1].pq == 0.0


def test_identical_masks_score_perfect():
    cfg = cfg1()
    cls = np.zeros((4, 4), dtype=np.uint8)
    cls[0:2, 0:2] = 1
    inst = (cls == 1).astype(np.uint16)
    rep = evaluate(make_mask(cls, inst), make_mask(cls, inst), cfg)
    assert rep.miou == pytest.approx(1.0)
    assert rep.per_category[1].pq == pytest.approx(1.0)
    assert rep.per_category[1].tp == 1


def test_tie_break_by_ascending_instance_id():
    cfg = cfg1(iou_threshold=0.3)
    gt_cls = np.zeros((5, 6), dtype=np.uint8)
    gt_inst = np.zeros((5, 6), dtype=np.uint16)
    gt_cls[0:2, 0:5] = 1
    gt_inst[0:2, 0:5] = 2          # gt instance 2, 10 px
    gt_cls[2:4, 0:5] = 1
    gt_inst[2:4, 0:5] = 3          # gt instance 3, 10 px
    pr_cls = np.zeros((5, 6), dtype=np.uint8)
    pr_inst = np.zeros((5, 6), dtype=np.uint16)
    pr_cls[0:2, 0:3] = 1
    pr_inst[0:2, 0:3] = 1          # 6 px inside gt2
    pr_cls[2:4, 0:3] = 1
    pr_inst[2:4, 0:3] = 1          # 6 px inside gt3 -> equal IoU for both
    rep = evaluate(make_mask(pr_cls, pr_inst), make_mask(gt_cls, gt_inst), cfg)
    matches = rep.details["categories"][1]["matches"]
    assert len(matches) == 1
    assert matches[0]["gt_instance"] == 2      # tie broken by ascending id
    assert matches[0]["iou"] == pytest.approx(6 / 16)


def test_ignore_pixels_excluded_from_scoring():
    cfg = cfg1()
    gt_cls = np.zeros((4, 4), dtype=np.uint8)
    gt_cls[0, 0] = 1
    gt_inst = (gt_cls == 1).astype(np.uint16)
    pr_cls = np.zeros((4, 4), dtype=np.uint8)   # prediction misses the pixel
    pr_inst = np.zeros((4, 4), dtype=np.uint16)
    ignore = np.zeros((4, 4), dtype=bool)
    ignore[0, 0] = True                          # ...but it is ignored
    rep = evaluate(make_mask(pr_cls, pr_inst), make_mask(gt_cls, gt_inst, ignore),
                   cfg)
    assert rep.details["scored_pixels"] == 15
    assert rep.miou == pytest.approx(1.0)        # miss not counted


def test_recompute_from_details_matches(cfg):
    from pano_workbench.session import Session
    s = Session(cfg)
    s.run_pipeline()
    recomputed = recompute_from_details(s.report.details, cfg)
    assert recomputed["miou"] == pytest.approx(s.report.miou, abs=cfg.precision)
    assert recomputed["pq_mean"] == pytest.approx(s.report.pq_mean,
                                                  abs=cfg.precision)
