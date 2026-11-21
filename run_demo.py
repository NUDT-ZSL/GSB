#!/usr/bin/env python3
"""Scripted end-to-end demonstration of the panoptic workbench.

Runs the full pipeline on deterministic local samples, exercises the
interactive query path, the latency budget guard, snapshot replacement,
consistency verification and the cross-module self-check.
"""
from __future__ import annotations

import json

from pano_workbench.config import WorkbenchConfig
from pano_workbench.errors import PanoError
from pano_workbench.query import QueryEngine
from pano_workbench.workbench import Workbench


def main() -> None:
    cfg = WorkbenchConfig(snapshot_dir="snapshots", cache_dir="cache")
    print(f"== config == seed={cfg.seed} size={cfg.width}x{cfg.height} "
          f"categories=0..{cfg.num_categories} budget={cfg.latency_budget_ms}ms "
          f"iou_threshold={cfg.iou_threshold} precision={cfg.precision}")
    wb = Workbench(cfg)
    s = wb.session

    print("\n== mask view (viewport 32x16) ==")
    print(wb.render_view())

    print("\n== metrics ==")
    print(wb.render_metrics())

    print("\n== snapshot ==")
    print(wb.render_snapshot())

    print("\n== interactive queries ==")
    mask = s.mask
    cx, cy = mask.width // 2, mask.height // 2
    r = s.query_pixel(cx, cy)
    print(f"pixel({cx},{cy}) -> cat={r.category_id} inst={r.instance_id} "
          f"conf={r.confidence:.4f} observed={r.latency_observed_us:.1f}us "
          f"estimate={r.latency_estimate_us:.1f}us "
          f"contribution={r.metric_contribution}")
    inst_id = s.index.entries[0].instance_id
    r = s.query_instance(inst_id)
    print(f"instance({inst_id}) -> cat={r.category_id} "
          f"conf_mean={r.confidence:.4f} observed={r.latency_observed_us:.1f}us "
          f"contribution={r.metric_contribution}")

    print("\n== latency budget guard (forced) ==")
    ticks = iter(range(0, 10 ** 9, 1))
    engine = QueryEngine(s.index, cfg, clock=lambda: next(ticks) * 0.010)
    try:
        engine.query_pixel(0, 0)
    except PanoError as err:
        print(json.dumps(err.to_dict(), ensure_ascii=False))

    print("\n== consistency (query path vs batch path) ==")
    print(s.verify_consistency())

    print("\n== snapshot replacement (v1 -> v2, whole-sale) ==")
    before = s.manager.current()
    s._commit_snapshot()
    after = s.manager.current()
    before.verify()
    print(f"v{before.version} checksum={before.checksum[:16]}... -> "
          f"v{after.version} checksum={after.checksum[:16]}... "
          f"(pinned old snapshot still verifies)")

    print("\n== self-check ==")
    print(json.dumps(s.self_check(), indent=2))

    print("\n== recent business errors ==")
    print(wb.render_errors())


if __name__ == "__main__":
    main()
