"""测试公共构造。"""

from detwb.vocab import VocabSnapshot


def make_vocab():
    return VocabSnapshot(1, {
        0: {"name": "person", "retired": False},
        1: {"name": "car", "retired": False},
        2: {"name": "dog", "retired": False},
    })


def make_records():
    return [
        {"sample_id": "s0", "cand_idx": 0, "x1": 1.0, "y1": 2.0,
         "x2": 10.0, "y2": 12.0, "score": 0.9, "class_id": 1,
         "class_name": "car", "gt_idx": 0, "iou": 0.8, "decision": "TP",
         "difficulty": "easy", "occluded": False,
         "conf_lo": 0.85, "conf_hi": 0.95, "vocab_version": 1},
        {"sample_id": "s0", "cand_idx": 1, "x1": 20.0, "y1": 20.0,
         "x2": 30.0, "y2": 30.0, "score": 0.4, "class_id": 2,
         "class_name": "dog", "gt_idx": -1, "iou": 0.0, "decision": "FP",
         "difficulty": "none", "occluded": False,
         "conf_lo": 0.35, "conf_hi": 0.45, "vocab_version": 1},
    ]


def make_samples_gt():
    return {"s0": [{"gt_idx": 0, "box": [1.0, 2.0, 10.0, 12.0], "label": 1,
                    "difficulty": "easy", "occluded": False}]}
