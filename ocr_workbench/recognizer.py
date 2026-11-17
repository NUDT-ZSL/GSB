"""Character segmentation and deterministic template-matching recognition."""

import numpy as np

from .font import CHARSET, FONT_VERSION, render_glyph


def build_templates(rec_cfg):
    """Render the configured template set once at the configured scale."""
    scale = rec_cfg["glyph_scale"]
    return {ch: np.asarray(render_glyph(ch, scale=scale), dtype=np.uint8) < 200
            for ch in CHARSET}


def _projection_runs(projection):
    runs = []
    start = None
    for idx, value in enumerate(projection):
        if value > 0 and start is None:
            start = idx
        elif value == 0 and start is not None:
            runs.append((start, idx))
            start = None
    if start is not None:
        runs.append((start, len(projection)))
    return runs


def merged_consecutive_spaces(segments):
    merged = []
    for seg in segments:
        if merged and merged[-1][2] and seg[2]:
            merged[-1][1] = seg[1]
        else:
            merged.append(list(seg))
    return merged


def _grid_segments(projection, pitch, glyph_width, space_gap):
    """Fixed-pitch segmentation anchored to each inked projection run.

    The font is monospaced.  Ink runs are snapped onto a pitch grid starting
    at the first inked column; empty grid slots between ink runs become space
    segments (counted by the number of missing pitches).  A trailing zero run
    at least ``space_gap`` wide emits trailing spaces.  Ink fragments are
    never split or dropped, and punctuation occupies a full cell so it cannot
    shift subsequent cells.
    """
    nonzero = np.flatnonzero(projection > 0)
    if nonzero.size == 0:
        return []
    ink_runs = _projection_runs(projection)
    origin = ink_runs[0][0]
    line_width = len(projection)
    segments = []
    previous_cell = -1
    for run_x0, run_x1 in ink_runs:
        cell_index = int(round((run_x0 - origin) / pitch))
        cell_start = origin + cell_index * pitch
        cell_end = min(cell_start + glyph_width, line_width)
        missing = cell_index - previous_cell - 1
        for gap_index in range(1, missing + 1):
            space_cell = previous_cell + gap_index
            sp0 = origin + space_cell * pitch
            segments.append([sp0, min(sp0 + glyph_width, line_width), True])
        segments.append([cell_start, cell_end, False])
        previous_cell = cell_index

    # Trailing spaces: continue the grid while zero columns remain.
    last_ink_end = ink_runs[-1][1]
    trailing_zero = line_width - last_ink_end
    trailing_cells = trailing_zero // pitch
    for extra in range(1, trailing_cells + 1):
        sp0 = origin + (previous_cell + extra) * pitch
        if sp0 >= line_width:
            break
        segments.append([sp0, min(sp0 + glyph_width, line_width), True])
    return segments


def segment_characters(gray, line_bbox, rec_cfg, det_cfg):
    """Split a detected line into character boxes via horizontal projection.

    Uses fixed-pitch grid segmentation (the font is monospaced) with explicit
    space segments; every segment is at least ``min_char_width`` wide and no
    fragment is ever dropped.
    """
    x0, y0, x1, y1 = line_bbox
    line_mask = gray[y0:y1, x0:x1] < det_cfg["binary_threshold"]
    projection = np.count_nonzero(line_mask, axis=0)
    pitch = rec_cfg["char_pitch"]
    glyph_width = rec_cfg["min_char_width"]
    raw = _grid_segments(projection, pitch, glyph_width,
                         rec_cfg["space_gap"])
    segments = []
    for seg_x0, seg_x1, is_space in raw:
        width = seg_x1 - seg_x0
        if not is_space and width < glyph_width:
            # Widen narrow fragments in place rather than dropping them.
            seg_x1 = min(len(projection), seg_x0 + glyph_width)
        segments.append([x0 + seg_x0, y0, x0 + seg_x1, y1, is_space])
    return segments


def _center_pad(mask, target_h, target_w):
    h, w = mask.shape
    out = np.zeros((target_h, target_w), dtype=bool)
    top = max(0, (target_h - h) // 2)
    left = max(0, (target_w - w) // 2)
    out[top:top + h, left:left + w] = mask
    return out


def _xor_similarity(a, b):
    return 1.0 - np.count_nonzero(a != b) / a.size


def _mse_similarity(a, b):
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return 1.0 / (1.0 + mse)


def _coverage_similarity(a, b):
    union = np.count_nonzero(a | b)
    return np.count_nonzero(a & b) / union if union else 0.0


def score_template(char_mask, template_mask, metric):
    ch, cw = char_mask.shape
    th, tw = template_mask.shape
    target_h, target_w = max(ch, th), max(cw, tw)
    char_padded = _center_pad(char_mask, target_h, target_w)
    if metric == "mse":
        return _mse_similarity(
            char_padded, _center_pad(template_mask, target_h, target_w))
    if metric == "coverage":
        return _coverage_similarity(
            char_padded, _center_pad(template_mask, target_h, target_w))
    best = 0.0
    for shift in (-1, 0, 1):
        shifted = np.zeros((target_h, target_w), dtype=bool)
        left = max(0, (target_w - tw) // 2 + shift)
        top = max(0, (target_h - th) // 2)
        if left + tw > target_w or top + th > target_h:
            continue
        shifted[top:top + th, left:left + tw] = template_mask
        best = max(best, _xor_similarity(char_padded, shifted))
    return best


def _tie_order(rec_cfg):
    if rec_cfg["tie_break"] == "reverse":
        return list(reversed(CHARSET))
    return list(CHARSET)


def recognize_segment(gray, bbox, templates, rec_cfg, det_cfg):
    x0, y0, x1, y1 = bbox
    char_mask = gray[y0:y1, x0:x1] < det_cfg["binary_threshold"]
    order = _tie_order(rec_cfg)
    scored = []
    for ch in order:
        score = score_template(char_mask, templates[ch], rec_cfg["metric"])
        scored.append((ch, round(score, 6)))
    scored.sort(key=lambda item: (-item[1], order.index(item[0])))
    best_char, best_score = scored[0]
    second_score = scored[1][1] if len(scored) > 1 else 0.0
    accepted = best_score >= rec_cfg["confidence_threshold"]
    return {
        "bbox": [int(x0), int(y0), int(x1), int(y1)],
        "char": best_char if accepted else "?",
        "best_char": best_char,
        "score": float(best_score),
        "second_score": float(second_score),
        "accepted": bool(accepted),
    }


def recognize_line(gray, line_bbox, templates, rec_cfg, det_cfg):
    segments = segment_characters(gray, line_bbox, rec_cfg, det_cfg)
    chars = []
    for seg in segments:
        if seg[4]:
            chars.append({
                "bbox": [int(seg[0]), int(seg[1]), int(seg[2]), int(seg[3])],
                "char": " ", "best_char": " ", "score": 1.0,
                "second_score": 0.0, "accepted": True, "is_space": True,
            })
        else:
            result = recognize_segment(gray, seg[:4], templates, rec_cfg,
                                       det_cfg)
            result["is_space"] = False
            chars.append(result)
    return {"bbox": list(line_bbox), "text": "".join(c["char"] for c in chars),
            "chars": chars}


def font_version():
    return FONT_VERSION
