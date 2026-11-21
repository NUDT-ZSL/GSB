"""确定性样本生成：固定种子派生 RGB 像素数组、候选框与真值框，输出 JSONL 清单。"""

import json

import numpy as np

from .canon import norm, sha256_bytes

IMAGE_W = 64
IMAGE_H = 64
DIFFICULTIES = ("easy", "medium", "hard")


def _rng(seed, index):
    return np.random.default_rng(np.random.SeedSequence([int(seed), int(index)]))


def image_for(seed, index):
    """按 (seed, index) 确定性重建 RGB 像素数组。"""
    rng = _rng(seed, index)
    return rng.integers(0, 256, size=(IMAGE_H, IMAGE_W, 3), dtype=np.uint8)


def image_sha256(seed, index):
    return sha256_bytes(image_for(seed, index).tobytes())


def _rand_box(rng):
    x1 = float(rng.uniform(0.0, IMAGE_W - 20.0))
    y1 = float(rng.uniform(0.0, IMAGE_H - 20.0))
    w = float(rng.uniform(8.0, 20.0))
    h = float(rng.uniform(8.0, 20.0))
    x2 = min(x1 + w, float(IMAGE_W))
    y2 = min(y1 + h, float(IMAGE_H))
    return [round(v, 6) for v in (x1, y1, x2, y2)]


def gen_sample(seed, index, num_classes, retired_label=None):
    """生成单个样本；retired_label 用于注入已停用类别的真值。"""
    rng = _rng(seed, index)
    n_gt = int(rng.integers(1, 6))
    gts = []
    for gi in range(n_gt):
        box = _rand_box(rng)
        label = int(rng.integers(0, num_classes))
        if retired_label is not None and gi == 0:
            label = int(retired_label)
        gts.append({
            "gt_idx": gi,
            "box": box,
            "label": label,
            "difficulty": DIFFICULTIES[int(rng.integers(0, 3))],
            "occluded": bool(rng.random() < 0.25),
        })
    cands = []
    ci = 0
    for gt in gts:
        n_pos = int(rng.integers(0, 3))
        for _ in range(n_pos):
            jit = [round(float(rng.uniform(-2.0, 2.0)), 6) for _ in range(4)]
            box = [round(min(max(gt["box"][k] + jit[k], 0.0),
                             IMAGE_W if k % 2 == 0 else IMAGE_H), 6) for k in range(4)]
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            cands.append({
                "cand_idx": ci,
                "box": box,
                "score": round(float(rng.uniform(0.5, 0.99)), 6),
                "class_id": gt["label"] if rng.random() < 0.9 else int(rng.integers(0, num_classes)),
            })
            ci += 1
    n_neg = int(rng.integers(1, 5))
    for _ in range(n_neg):
        cands.append({
            "cand_idx": ci,
            "box": _rand_box(rng),
            "score": round(float(rng.uniform(0.05, 0.6)), 6),
            "class_id": int(rng.integers(0, num_classes)),
        })
        ci += 1
    return {
        "sample_id": f"s{index:04d}",
        "seed": int(seed),
        "index": int(index),
        "image": {"width": IMAGE_W, "height": IMAGE_H, "channels": 3,
                  "sha256": image_sha256(seed, index)},
        "candidates": cands,
        "gt": gts,
    }


def gen_manifest(seed, num_samples, num_classes, retired_label=None, retired_index=None):
    samples = []
    for i in range(num_samples):
        rl = retired_label if (retired_index is not None and i == retired_index) else None
        samples.append(gen_sample(seed, i, num_classes, retired_label=rl))
    return samples


def write_manifest(samples, path):
    with open(path, "w", encoding="utf-8") as fh:
        for s in samples:
            fh.write(json.dumps(norm(s), sort_keys=True, ensure_ascii=False) + "\n")


def read_manifest(path):
    samples = []
    with open(path, "r", encoding="utf-8") as fh:
        for ln, line in enumerate(fh, start=1):
            line = line.strip()
            if line:
                samples.append((ln, json.loads(line)))
    return samples
