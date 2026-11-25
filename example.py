#!/usr/bin/env python3
"""End-to-end demo of the match-verification component on deterministic
local samples: dual-ledger write, migration, directed invalidation, replay,
crash recovery, concurrency control, and structured error codes."""
import json
import os
import shutil
import subprocess
import sys

from matcher import (
    BusinessError,
    Config,
    Pipeline,
    SimulatedCrash,
)
from matcher.ledger import read_ledger, read_v2
from matcher.replay import replay_row
from matcher.ingest import load_candidates, load_ground_truth
from matcher.snapshot import StageLock

ROOT = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(ROOT, "samples")


def banner(title):
    print("\n=== %s ===" % title)


def expect_error(code, fn, label):
    try:
        fn()
    except BusinessError as exc:
        assert exc.code == code, "expected %s got %s" % (code, exc.code)
        print("%-28s -> %s (field=%s)" % (label, exc.code, exc.field))
        return
    raise AssertionError("expected %s from %s" % (code, label))


def fresh_pipeline(**cfg_kwargs):
    for d in ("ledgers", "checkpoints"):
        path = os.path.join(ROOT, d)
        if os.path.exists(path):
            shutil.rmtree(path)
    return Pipeline(ROOT, Config(**cfg_kwargs))


def main():
    banner("0. generate deterministic samples")
    subprocess.run(
        [sys.executable, os.path.join(ROOT, "generate_samples.py"),
         "--out", SAMPLES],
        check=True,
    )
    cand = os.path.join(SAMPLES, "candidates.jsonl")
    gt = os.path.join(SAMPLES, "ground_truth.jsonl")
    gt2 = os.path.join(SAMPLES, "ground_truth_v2.jsonl")

    banner("1. full pipeline run (ingest -> match -> dual ledger -> replay)")
    p = fresh_pipeline()
    report = p.run(cand, gt)
    print("images=%d rows_v1=%d rows_v2=%d aggregate=%s"
          % (report["images"], report["rows_v1"], report["rows_v2"],
             json.dumps(report["aggregate"]["per_class"], sort_keys=True)))
    print("replay=%s selfcheck=%s"
          % (report["verification"]["replay"],
             report["verification"]["selfcheck"]["status"]))

    banner("2. determinism: identical rerun produces identical v1 bytes")
    v1_bytes_1 = open(p.v1_path, "rb").read()
    p2 = fresh_pipeline()
    p2.run(cand, gt)
    v1_bytes_2 = open(p2.v1_path, "rb").read()
    assert v1_bytes_1 == v1_bytes_2, "v1 ledger bytes diverged"
    print("v1 ledger byte-identical across runs (%d bytes)" % len(v1_bytes_1))

    banner("3. crash recovery: committed batches are never recomputed")
    p3 = fresh_pipeline(batch_size=4)
    try:
        p3.run(cand, gt, crash_after_batch=1)
        raise AssertionError("expected SimulatedCrash")
    except SimulatedCrash as exc:
        print("interrupted:", exc)
    p4 = Pipeline(ROOT, Config(batch_size=4))
    rec = p4.run(cand, gt)
    print("resumed_from_batch=%d images_recomputed=%d (of %d)"
          % (rec["resumed_from_batch"], rec["images_recomputed"], rec["images"]))
    assert rec["resumed_from_batch"] == 1 and rec["images_recomputed"] == 4

    banner("4. concurrency: single committer, late comer gets E_STAGE_BUSY")
    lock = StageLock(os.path.join(ROOT, "ledgers", "commit.lock"))
    lock.acquire()
    expect_error("E_STAGE_BUSY", lambda: p4.run(cand, gt), "concurrent commit")
    lock.release()

    banner("5. directed invalidation: GT revision bump + threshold change")
    r = p4.apply_update(gt_path=gt2)
    print("GT revision bump -> affected=%s reused=%d"
          % (r["affected_images"], r["images_reused"]))
    assert r["affected_images"] == ["img_0002", "img_0007"]
    r = p4.apply_update(iou_threshold=0.6)
    print("threshold 0.5->0.6 -> affected=%d images (all), rows_v1=%d"
          % (len(r["affected_images"]), r["rows_v1"]))

    banner("6. bidirectional ledger migration")
    r = p4.apply_update(migrate_to="v1")
    rec = r["migration"]["record"]
    print("v2->v1 downgrade: rule=%s discarded=%s rows=%d"
          % (rec["rule"], rec["discarded_fields"], rec["rows_affected"]))
    r = p4.apply_update(migrate_to="v2")
    prov = r["migration"]["provenance"]
    sources = sorted({s for _, _, s in prov})
    print("v1->v2 upgrade: provenance=%d entries sources=%s"
          % (len(prov), sources))

    banner("7. row-level replay from v2 ledger")
    data_v2 = read_v2(p4.v2_dir)
    cands = load_candidates(cand, p4.cfg.classes)
    gts = load_ground_truth(gt2, p4.cfg.classes)
    cand_map = {(c.image_id, c.box_idx): c for c in cands}
    gt_map = {(g.image_id, g.box_idx): g for g in gts}
    tp_row = next(r for r in data_v2["rows"] if r["status"] == "TP")
    out = replay_row(
        tp_row,
        cand_map[(tp_row["image_id"], tp_row["cand_idx"])],
        gt_map[(tp_row["image_id"], tp_row["gt_idx"])],
        p4.cfg.iou_threshold,
    )
    print("replayed TP row %s#%s<->%s iou=%s status=%s"
          % (tp_row["image_id"], tp_row["cand_idx"], tp_row["gt_idx"],
             out["iou"], out["status"]))

    banner("8. structured error codes")
    bad_dir = os.path.join(ROOT, "checkpoints", "bad")
    os.makedirs(bad_dir, exist_ok=True)
    dup = os.path.join(bad_dir, "dup.jsonl")
    with open(cand) as fh:
        lines = fh.readlines()
    with open(dup, "w") as fh:
        fh.writelines([lines[0], lines[0]])
    expect_error("E_DUP_IMAGE",
                 lambda: load_candidates(dup, p4.cfg.classes), "duplicate image/box")
    badbox = os.path.join(bad_dir, "badbox.jsonl")
    with open(badbox, "w") as fh:
        fh.write(json.dumps({"image_id": "x", "box_idx": 0, "x1": 10, "y1": 10,
                             "x2": 5, "y2": 20, "score": 0.5, "class_id": 0}) + "\n")
    expect_error("E_BAD_BOX",
                 lambda: load_candidates(badbox, p4.cfg.classes), "degenerate box")
    fake = os.path.join(bad_dir, "ledger_v9.json")
    with open(fake, "w") as fh:
        json.dump({"format": "match-ledger", "version": "v9", "rows": []}, fh)
    expect_error("E_VERSION_UNSUPPORTED", lambda: read_ledger(fake), "unknown version")
    corrupt = os.path.join(bad_dir, "corrupt.json")
    with open(corrupt, "w") as fh:
        json.dump({"format": "match-ledger", "version": "v1",
                   "rows": [{"image_id": "a", "status": "TP"}]}, fh)
    expect_error("E_FORMAT_CORRUPT", lambda: read_ledger(corrupt), "missing fields")
    expect_error("E_THRESHOLD_RANGE",
                 lambda: Config(iou_threshold=1.5).validate(), "threshold range")
    expect_error("E_SNAPSHOT_ROLLBACK",
                 lambda: p4.snapshots.update("ledger_v1", 0, p4.v1_path),
                 "snapshot rollback")
    with open(p4.v1_path, "a") as fh:
        fh.write(" ")  # tamper after snapshot
    expect_error("E_SNAPSHOT_CORRUPT",
                 lambda: p4.verify_snapshots(), "tampered ledger")
    p4.run(cand, gt2)  # restore clean state (rewrites ledgers)
    import tempfile

    tmp_root = tempfile.mkdtemp(prefix="matcher_limit_")
    p_tmp = Pipeline(tmp_root, Config(max_resident_candidates=2,
                                      allow_degrade=False))
    expect_error("E_RESOURCE_LIMIT", lambda: p_tmp.run(cand, gt2),
                 "resident candidate limit")
    p5 = Pipeline(ROOT, Config(max_recompute_images=1))
    expect_error("E_RESOURCE_LIMIT",
                 lambda: p5.apply_update(iou_threshold=0.7),
                 "recompute image limit")
    regress = os.path.join(bad_dir, "gt_regress.jsonl")
    with open(gt2) as fh:
        recs = [json.loads(l) for l in fh if l.strip()]
    recs[0]["revision"] = 0
    with open(regress, "w") as fh:
        for rec in recs:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    expect_error("E_REVISION_REGRESS",
                 lambda: p5.apply_update(gt_path=regress), "revision regress")

    banner("9. final state")
    p6 = Pipeline(ROOT, Config())
    print("read_back:", p6.read_back())
    print("snapshots verified:", p6.verify_snapshots())
    print("\nALL DEMO CHECKS PASSED")


if __name__ == "__main__":
    main()
