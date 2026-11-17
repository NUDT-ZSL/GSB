"""Deterministic text detection and line aggregation.

The pipeline is fully deterministic (no RNG, no thread-dependent ordering):
  1. threshold the grayscale image into a stroke mask;
  2. slide windows over the mask to measure stroke density and edge strength,
     marking windows whose features pass the configured thresholds;
  3. label connected components (4- or 8-connectivity) on both the raw stroke
     mask (atomic glyph blocks) and the window coverage (line seeds);
  4. aggregate blocks into text lines using vertical projection and a
     configurable merge gap, splitting horizontally only where coverage seeds
     are genuinely separate;
  5. emit integer, border-clipped line boxes with block count and score.
"""

import numpy as np


def stroke_mask(gray, binary_threshold):
    return gray < binary_threshold


def window_features(mask, win_w, win_h, step):
    """Return per-window density and edge maps at window-grid resolution."""
    height, width = mask.shape
    integral = np.zeros((height + 1, width + 1), dtype=np.int64)
    integral[1:, 1:] = np.cumsum(np.cumsum(mask.astype(np.int64), axis=0), axis=1)
    cols = list(range(0, max(width - win_w, 0) + 1, step))
    rows = list(range(0, max(height - win_h, 0) + 1, step))
    density = np.zeros((len(rows), len(cols)), dtype=np.float64)
    edge = np.zeros((len(rows), len(cols)), dtype=np.float64)
    area = float(win_w * win_h)
    for ri, y in enumerate(rows):
        for ci, x in enumerate(cols):
            y2, x2 = min(y + win_h, height), min(x + win_w, width)
            strokes = (integral[y2, x2] - integral[y, x2]
                       - integral[y2, x] + integral[y, x])
            density[ri, ci] = strokes / area
            window = mask[y:y2, x:x2]
            horiz = np.count_nonzero(window[:, 1:] != window[:, :-1])
            vert = np.count_nonzero(window[1:, :] != window[:-1, :])
            edge[ri, ci] = (horiz + vert) / area
    return rows, cols, density, edge


def label_components(mask, connectivity):
    """Vectorized connected-component labeling (Rosenfeld-Pfaltz style).

    Forward-neighbor equivalences are resolved with a compact union-find over
    a NumPy label array; no per-pixel Python loop is used.  Supports 4- and
    8-connectivity and returns boxes sorted by (top, left).
    """
    height, width = mask.shape
    if not np.any(mask):
        return []
    parent = np.arange(mask.size + 1, dtype=np.int32)

    def find_all(labels):
        # Path halving applied vectorized until labels reach fixed point.
        while True:
            current = parent[labels]
            if np.array_equal(current, parent[current]):
                return current
            parent[labels] = parent[current]

    labels = np.zeros(mask.shape, dtype=np.int32)
    next_label = 1
    offsets = (
        [(-1, -1), (-1, 0), (-1, 1), (0, -1)]
        if connectivity == 8 else [(-1, 0), (0, -1)]
    )
    ys, xs = np.nonzero(mask)
    for y, x in zip(ys.tolist(), xs.tolist()):
        neighbor = []
        for dy, dx in offsets:
            ny, nx = y + dy, x + dx
            if ny >= 0 and nx >= 0 and labels[ny, nx] > 0:
                neighbor.append(int(labels[ny, nx]))
        if not neighbor:
            labels[y, x] = next_label
            next_label += 1
            continue
        root = min(neighbor)
        labels[y, x] = root
        for lbl in neighbor:
            a, b = int(parent[root]), int(parent[lbl])
            lo, hi = min(a, b), max(a, b)
            parent[hi] = lo

    flat_labels = labels.reshape(-1)
    resolved = find_all(flat_labels)
    distinct = np.unique(resolved[resolved > 0])
    remap = {int(root): i + 1 for i, root in enumerate(distinct)}
    final = np.zeros_like(flat_labels)
    active = resolved > 0
    final[active] = np.array([remap[int(v)] for v in resolved[active]],
                             dtype=np.int32)
    grid = final.reshape(height, width)
    boxes = []
    for component in range(1, len(distinct) + 1):
        cy, cx = np.nonzero(grid == component)
        boxes.append([int(cx.min()), int(cy.min()),
                      int(cx.max()) + 1, int(cy.max()) + 1])
    return sorted(boxes, key=lambda b: (b[1], b[0]))


def _project_box(mask, seed, row_support, col_support=1):
    """Tighten a seed box using row/column stroke projection, trimming noise."""
    x0, y0, x1, y1 = seed
    region = mask[y0:y1, x0:x1]
    row_counts = np.count_nonzero(region, axis=1)
    col_counts = np.count_nonzero(region, axis=0)
    rows = np.flatnonzero(row_counts >= row_support)
    cols = np.flatnonzero(col_counts >= col_support)
    if rows.size == 0 or cols.size == 0:
        return None
    return [x0 + int(cols[0]), y0 + int(rows[0]),
            x0 + int(cols[-1]) + 1, y0 + int(rows[-1]) + 1]


def detect(gray, cfg):
    """Run detection; return ordered text-line candidates."""
    height, width = gray.shape
    mask = stroke_mask(gray, cfg["binary_threshold"])
    coverage = np.zeros_like(mask, dtype=bool)

    for (win_w, win_h) in cfg["window_sizes"]:
        rows, cols, density, edge = window_features(
            mask, win_w, win_h, cfg["window_step"])
        hit = (density >= cfg["density_threshold"]) & \
              (edge >= cfg["edge_threshold"])
        for ri, y in enumerate(rows):
            for ci, x in enumerate(cols):
                if hit[ri, ci]:
                    coverage[y:min(y + win_h, height),
                             x:min(x + win_w, width)] = True

    min_h = cfg["min_char_height"]
    min_support = max(3, cfg["min_char_height"] // 3)
    raw_boxes = [b for b in label_components(mask, cfg["connectivity"])
                 if (b[3] - b[1]) >= min_h]
    coverage_seeds = [b for b in label_components(coverage, cfg["connectivity"])
                      if (b[3] - b[1]) >= min_h]

    line_seeds = _aggregate_seeds(
        raw_boxes, coverage_seeds, mask, cfg["line_gap"],
        cfg["min_char_height"], width, height)

    results = []
    for seed in line_seeds:
        tight = _project_box(mask, seed, min_support, col_support=1)
        if tight is None or tight[2] - tight[0] < min_h:
            continue
        x0, y0, x1, y1 = tight
        block_count = sum(
            1 for b in raw_boxes
            if not (b[2] <= x0 or b[0] >= x1 or b[3] <= y0 or b[1] >= y1)
        )
        region_mask = mask[y0:y1, x0:x1]
        region_cov = coverage[y0:y1, x0:x1]
        fill = float(np.count_nonzero(region_mask)) / region_mask.size
        coverage_ratio = float(np.count_nonzero(region_cov)) / region_cov.size
        score = round(0.6 * fill + 0.4 * coverage_ratio, 6)
        if score < cfg["candidate_score_threshold"]:
            continue
        results.append({
            "bbox": [int(max(0, x0)), int(max(0, y0)),
                     int(min(width, x1)), int(min(height, y1))],
            "block_count": int(block_count),
            "score": score,
        })
    results.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
    return results


def _vertical_bands(boxes, line_gap):
    """Group boxes into vertical bands by overlapping/near row spans."""
    bands = []
    for box in sorted(boxes, key=lambda b: (b[1], b[0])):
        x0, y0, x1, y1 = box
        for band in bands:
            by0, by1 = band[0], band[1]
            if y0 - by1 <= line_gap and by0 - y1 <= line_gap:
                band[0] = min(by0, y0)
                band[1] = max(by1, y1)
                band[2].append(box)
                break
        else:
            bands.append([y0, y1, [box]])
    return bands


def _aggregate_seeds(raw_boxes, coverage_seeds, mask, line_gap,
                     min_char_height, width, height):
    """Aggregate atomic blocks into line boxes.

    Vertical bands derive from raw glyph blocks so distinct side-by-side
    blocks never merge.  Within one band a horizontal split is emitted only
    where two *coverage* seeds are separated by a gap wider than the block
    separation distance; ordinary intra-line spaces stay inside one line.
    """
    bands = _vertical_bands(raw_boxes, line_gap)
    block_separation = max(line_gap, int(round(min_char_height * 1.5)))
    lines = []
    for by0, by1, members in bands:
        bx0, bx1 = min(m[0] for m in members), max(m[2] for m in members)
        projection = np.count_nonzero(mask[by0:by1, bx0:bx1], axis=0)
        gaps = []
        run = None
        for idx, value in enumerate(projection):
            if value == 0:
                if run is None:
                    run = idx
            elif run is not None:
                gaps.append((bx0 + run, bx0 + idx, idx - run))
                run = None
        if run is not None:
            gaps.append((bx0 + run, bx1, bx1 - (bx0 + run)))
        # A cut requires a wide stroke-free gap whose left and right segments
        # are both short (isolated glyph blocks).  A long text line has at
        # least one wide segment, so intra-line spaces never trigger a cut.
        block_width_limit = min_char_height * 4
        cuts = [(gx0, gx1) for gx0, gx1, gap_width in gaps
                if gap_width >= block_separation
                and (gx0 - bx0) <= block_width_limit
                and (bx1 - gx1) <= block_width_limit
                and any(m[2] <= gx0 for m in members)
                and any(m[0] >= gx1 for m in members)]
        if not cuts:
            lines.append([max(0, bx0), max(0, by0),
                          min(width, bx1), min(height, by1)])
            continue
        cursor = bx0
        for gx0, gx1 in cuts:
            lines.append([max(0, cursor), max(0, by0),
                          min(width, min(bx1, gx0)), min(height, by1)])
            cursor = max(cursor, gx1)
        lines.append([max(0, cursor), max(0, by0),
                      min(width, bx1), min(height, by1)])
    return [ln for ln in lines if ln[2] > ln[0] and ln[3] > ln[1]]
