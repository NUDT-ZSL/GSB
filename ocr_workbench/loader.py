"""Image and ground-truth loading with strict validation."""

import hashlib
import json
import os

import numpy as np
from PIL import Image

from .errors import (
    E_IMAGE_DECODE,
    E_IMAGE_SHAPE,
    E_TRUTH_INVALID,
    E_TRUTH_OUT_OF_RANGE,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def image_fingerprint(gray):
    """Content fingerprint of a decoded grayscale array."""
    return hashlib.sha256(np.ascontiguousarray(gray).tobytes()).hexdigest()[:16]


def _is_int(value):
    if isinstance(value, bool):
        return False
    return isinstance(value, int)


def _valid_bbox(bbox, width, height):
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return False
    if not all(_is_int(v) for v in bbox):
        return False
    x0, y0, x1, y1 = bbox
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        return False
    return True


def _validate_truth(truth, width, height):
    if not isinstance(truth, dict) or "lines" not in truth:
        return E_TRUTH_INVALID, "truth missing 'lines'"
    groups = list(truth.get("lines", [])) + list(truth.get("blocks", []))
    for group in groups:
        chars = group.get("chars", [])
        if not chars:
            return E_TRUTH_INVALID, "line/block without chars"
        for char in chars:
            bbox = char.get("bbox")
            if not _valid_bbox(bbox, width, height):
                return E_TRUTH_OUT_OF_RANGE, f"char bbox invalid or out of range"
        line_bbox = group.get("bbox")
        if line_bbox is not None and not _valid_bbox(line_bbox, width, height):
            return E_TRUTH_OUT_OF_RANGE, "line bbox out of range"
        # Same-line char boxes must vertically overlap one another.
        intervals = [c["bbox"][1:4:2] for c in chars]
        for idx in range(1, len(intervals)):
            prev_y0, prev_y1 = intervals[idx - 1]
            y0, y1 = intervals[idx]
            if min(prev_y1, y1) <= max(prev_y0, y0):
                return E_TRUTH_OUT_OF_RANGE, "same-line chars do not vertically overlap"
    return None, None


def load_sample(png_path, expected_size):
    """Load one PNG + truth JSON.

    Returns ``(record, failure)``.  Exactly one of them is not ``None``.
    """
    name = os.path.splitext(os.path.basename(png_path))[0]
    json_path = os.path.splitext(png_path)[0] + ".json"

    try:
        with open(png_path, "rb") as handle:
            header = handle.read(8)
        if header != PNG_SIGNATURE:
            return None, {"file": name, "code": E_IMAGE_DECODE,
                          "message": "PNG container header mismatch"}
        with Image.open(png_path) as image:
            image.load()
            if image.mode != "L":
                return None, {"file": name, "code": E_IMAGE_SHAPE,
                              "message": f"pixel mode is {image.mode}, expected L"}
            width, height = image.size
            if [width, height] != list(expected_size):
                return None, {"file": name, "code": E_IMAGE_SHAPE,
                              "message": f"size {width}x{height} != {expected_size}"}
            gray = np.asarray(image, dtype=np.uint8).copy()
    except Exception as exc:  # decode failures of any kind
        return None, {"file": name, "code": E_IMAGE_DECODE,
                      "message": str(exc)}

    try:
        with open(json_path, "r", encoding="utf-8") as handle:
            truth = json.load(handle)
    except Exception as exc:
        return None, {"file": name, "code": E_TRUTH_INVALID,
                      "message": f"truth unreadable: {exc}"}

    code, message = _validate_truth(truth, width, height)
    if code is not None:
        return None, {"file": name, "code": code, "message": message}

    record = {
        "name": name,
        "png_path": png_path,
        "gray": gray,
        "fingerprint": image_fingerprint(gray),
        "truth": truth,
        "width": width,
        "height": height,
    }
    return record, None


def load_directory(sample_dir, expected_size):
    """Scan all ``*.png`` files in filename order; failures never stop peers."""
    pngs = sorted(
        os.path.join(sample_dir, f)
        for f in os.listdir(sample_dir)
        if f.lower().endswith(".png")
    )
    records, failures = [], []
    for png_path in pngs:
        record, failure = load_sample(png_path, expected_size)
        if failure is not None:
            failures.append(failure)
        else:
            records.append(record)
    return records, failures
