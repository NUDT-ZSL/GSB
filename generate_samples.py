#!/usr/bin/env python3
"""Fixed-seed deterministic local sample generator (numpy + stdlib only).

Emits into samples/:
  candidates.jsonl        candidate boxes: x1/y1/x2/y2, score, class_id
  ground_truth.jsonl      GT boxes: label, difficulty, occluded, revision=1
  ground_truth_v2.jsonl   same boxes, revision bumped to 2 on some images
  ledger_v1.template.json / ledger_v2.template.json
  clock.json              injectable logical clock parameters
"""
import argparse
import json
import os

import numpy as np

CLASSES = [0, 1, 2]


def _box(rng, x1=None, y1=None):
    if x1 is None:
        x1 = float(rng.uniform(0, 400))
    if y1 is None:
        y1 = float(rng.uniform(0, 300))
    w = float(rng.uniform(20, 120))
    h = float(rng.uniform(20, 120))
    return (round(x1, 3), round(y1, 3), round(x1 + w, 3), round(y1 + h, 3))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="samples")
    parser.add_argument("--seed", type=int, default=20261126)
    parser.add_argument("--images", type=int, default=12)
    parser.add_argument("--clock-base-ms", type=int, default=1700000000000)
    parser.add_argument("--clock-step-ms", type=int, default=250)
    args = parser.parse_args()

    rng = np.random.RandomState(args.seed)
    os.makedirs(args.out, exist_ok=True)

    cand_path = os.path.join(args.out, "candidates.jsonl")
    gt_path = os.path.join(args.out, "ground_truth.jsonl")
    gt2_path = os.path.join(args.out, "ground_truth_v2.jsonl")
    # images whose GT gets a revision bump in ground_truth_v2.jsonl
    bump_images = {"img_0002", "img_0007"}

    gt_v2_records = []
    with open(cand_path, "w", encoding="utf-8") as fc, open(
        gt_path, "w", encoding="utf-8"
    ) as fg:
        for i in range(args.images):
            image_id = "img_%04d" % i
            n_gt = int(rng.randint(1, 5))
            gts = []
            for j in range(n_gt):
                x1, y1, x2, y2 = _box(rng)
                gt = {
                    "image_id": image_id,
                    "box_idx": j,
                    "x1": x1,
                    "y1": y1,
                    "x2": x2,
                    "y2": y2,
                    "label": int(rng.choice(CLASSES)),
                    "difficulty": int(rng.randint(0, 3)),
                    "occluded": bool(rng.rand() < 0.2),
                    "revision": 1,
                }
                gts.append(gt)
                fg.write(json.dumps(gt, sort_keys=True) + "\n")
                gt2 = dict(gt)
                if image_id in bump_images:
                    gt2["revision"] = 2
                    # tiny deterministic shift so content actually changes
                    gt2["x1"] = round(gt2["x1"] + 1.5, 3)
                    gt2["y1"] = round(gt2["y1"] + 1.5, 3)
                    gt2["x2"] = round(gt2["x2"] + 1.5, 3)
                    gt2["y2"] = round(gt2["y2"] + 1.5, 3)
                gt_v2_records.append(gt2)
            k = 0
            for gt in gts:
                # candidates clustered near each GT (matching class mostly)
                for _ in range(int(rng.randint(1, 3))):
                    jx = float(rng.normal(0, 6))
                    jy = float(rng.normal(0, 6))
                    jw = float(rng.normal(0, 4))
                    jh = float(rng.normal(0, 4))
                    cand = {
                        "image_id": image_id,
                        "box_idx": k,
                        "x1": round(max(0.0, gt["x1"] + jx), 3),
                        "y1": round(max(0.0, gt["y1"] + jy), 3),
                        "x2": round(gt["x2"] + jx + jw, 3),
                        "y2": round(gt["y2"] + jy + jh, 3),
                        "score": round(float(rng.uniform(0.3, 0.99)), 3),
                        "class_id": gt["label"]
                        if rng.rand() < 0.85
                        else int(rng.choice(CLASSES)),
                    }
                    if cand["x2"] <= cand["x1"]:
                        cand["x2"] = round(cand["x1"] + 5.0, 3)
                    if cand["y2"] <= cand["y1"]:
                        cand["y2"] = round(cand["y1"] + 5.0, 3)
                    fc.write(json.dumps(cand, sort_keys=True) + "\n")
                    k += 1
            # a few random background candidates (likely FP)
            for _ in range(int(rng.randint(0, 3))):
                x1, y1, x2, y2 = _box(rng)
                cand = {
                    "image_id": image_id,
                    "box_idx": k,
                    "x1": x1,
                    "y1": y1,
                    "x2": x2,
                    "y2": y2,
                    "score": round(float(rng.uniform(0.2, 0.95)), 3),
                    "class_id": int(rng.choice(CLASSES)),
                }
                fc.write(json.dumps(cand, sort_keys=True) + "\n")
                k += 1

    with open(gt2_path, "w", encoding="utf-8") as fh:
        for rec in gt_v2_records:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")

    v1_template = {
        "format": "match-ledger",
        "version": "v1",
        "ledger_version": 0,
        "config": {"iou_threshold": 0.5},
        "clock": {"base_ms": args.clock_base_ms, "step_ms": args.clock_step_ms},
        "rows": [],
        "aggregate": {},
        "checksum": None,
    }
    v2_template = {
        "format": "match-ledger",
        "version": "v2",
        "ledger_version": 0,
        "layout": "shards/shard_XX.json + meta.sqlite3",
        "row_fields": [
            "image_id", "t_abs_ms", "generation", "cand_hash", "gt_revision",
            "status", "iou", "inter", "union", "class_id", "score", "row_hash",
        ],
    }
    with open(os.path.join(args.out, "ledger_v1.template.json"), "w") as fh:
        json.dump(v1_template, fh, indent=2, sort_keys=True)
        fh.write("\n")
    with open(os.path.join(args.out, "ledger_v2.template.json"), "w") as fh:
        json.dump(v2_template, fh, indent=2, sort_keys=True)
        fh.write("\n")
    with open(os.path.join(args.out, "clock.json"), "w") as fh:
        json.dump(
            {"base_ms": args.clock_base_ms, "step_ms": args.clock_step_ms},
            fh, indent=2, sort_keys=True,
        )
        fh.write("\n")

    n_cand = sum(1 for _ in open(cand_path))
    n_gt = sum(1 for _ in open(gt_path))
    print("seed=%d images=%d candidates=%d ground_truth=%d -> %s"
          % (args.seed, args.images, n_cand, n_gt, args.out))


if __name__ == "__main__":
    main()
