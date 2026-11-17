import tempfile

from ocr_workbench.config import DETECTION_DEFAULTS
from ocr_workbench.detector import detect, label_components, stroke_mask
from ocr_workbench.loader import load_directory
from ocr_workbench.samplegen import generate_dataset


def _records(count=4):
    tmp = tempfile.mkdtemp()
    generate_dataset(tmp, image_count=count)
    records, _ = load_directory(tmp, DETECTION_DEFAULTS["image_size"])
    return records


def _iou(a, b):
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    union = ((ax1 - ax0) * (ay1 - ay0)
             + (bx1 - bx0) * (by1 - by0) - inter)
    return inter / union


def test_detects_expected_line_count():
    records = _records()
    for record in records:
        truth = record["truth"]["lines"] + record["truth"]["blocks"]
        got = detect(record["gray"], DETECTION_DEFAULTS)
        assert len(got) == len(truth)


def test_boxes_are_integer_clipped_and_sorted():
    records = _records(1)
    got = detect(records[0]["gray"], DETECTION_DEFAULTS)
    h, w = records[0]["gray"].shape
    for line in got:
        x0, y0, x1, y1 = line["bbox"]
        assert all(isinstance(v, int) for v in line["bbox"])
        assert 0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h
        assert line["block_count"] >= 1
        assert line["score"] >= DETECTION_DEFAULTS["candidate_score_threshold"]
    tops = [line["bbox"][1] for line in got]
    assert tops == sorted(tops)


def test_detection_is_deterministic_across_runs():
    records = _records(1)
    first = detect(records[0]["gray"], DETECTION_DEFAULTS)
    second = detect(records[0]["gray"], dict(DETECTION_DEFAULTS))
    assert first == second


def test_detection_matches_truth_with_high_iou():
    records = _records(3)
    ious = []
    for record in records:
        truth = record["truth"]["lines"] + record["truth"]["blocks"]
        got = detect(record["gray"], DETECTION_DEFAULTS)
        for line, tline in zip(got, truth):
            ious.append(_iou(line["bbox"], tline["bbox"]))
    assert sum(ious) / len(ious) > 0.9


def test_label_components_orders_boxes():
    mask = np_like_mask()
    boxes = label_components(mask, 8)
    assert boxes == sorted(boxes, key=lambda b: (b[1], b[0]))
    assert len(boxes) == 2


def np_like_mask():
    import numpy as np
    mask = np.zeros((20, 20), dtype=bool)
    mask[2:5, 2:5] = True
    mask[12:16, 12:17] = True
    return mask


def test_stroke_threshold():
    import numpy as np
    gray = np.full((5, 5), 200, dtype=np.uint8)
    gray[0, 0] = 10
    mask = stroke_mask(gray, 160)
    assert mask[0, 0] and not mask[1, 1]
