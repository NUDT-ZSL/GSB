"""Image loading, validation and color-connected-region feature extraction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from .errors import E_IMAGE_INVALID, E_REGION_INVALID, WorkbenchError

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@dataclass
class RegionFeatures:
    image: str
    region_index: int
    area: int
    mean_color: tuple[float, float, float]
    bbox: tuple[int, int, int, int]  # x0, y0, x1, y1 clipped integer pixels
    compactness: float               # area / (bbox_w * bbox_h), in (0, 1]

    @property
    def sort_key(self) -> tuple:
        return (self.image, self.region_index)

    def feature_value(self, name: str, img_area: int) -> float:
        if name == "compactness":
            return self.compactness
        if name == "area_norm":
            return self.area / float(img_area)
        raise KeyError(name)


def load_png(path, config) -> np.ndarray:
    """Load a PNG sample, validating container header and fixed dimensions."""
    path = Path(path)
    with open(path, "rb") as fh:
        head = fh.read(len(PNG_MAGIC))
    if head != PNG_MAGIC:
        raise WorkbenchError(E_IMAGE_INVALID, f"{path.name}: bad PNG container header")
    img = Image.open(path).convert("RGB")
    want = (config["image"]["width"], config["image"]["height"])
    if img.size != want:
        raise WorkbenchError(
            E_IMAGE_INVALID, f"{path.name}: size {img.size} != expected {want}")
    return np.asarray(img)


def _quantize(arr: np.ndarray, levels: int) -> np.ndarray:
    step = 256 // levels
    q = (arr // step).astype(np.int64)
    return (q[:, :, 0] * levels + q[:, :, 1]) * levels + q[:, :, 2]


def _component_roots(labels: np.ndarray) -> np.ndarray:
    """4-connectivity union-find over quantized labels. Fully deterministic."""
    h, w = labels.shape
    n = h * w
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    flat = labels.ravel()
    for y in range(h):
        base = y * w
        for x in range(w):
            i = base + x
            if x + 1 < w and flat[i] == flat[i + 1]:
                union(i, i + 1)
            if y + 1 < h and flat[i] == flat[i + w]:
                union(i, i + w)
    return np.fromiter((find(i) for i in range(n)), dtype=np.int64, count=n)


def extract_regions(image_name: str, arr: np.ndarray, config) -> tuple[list, list]:
    """Extract candidate regions by color connected components.

    Returns (regions, failures). Regions with zero area or an out-of-bounds
    bbox are recorded as E_REGION_INVALID failures; valid regions carry
    clipped integer bboxes.
    """
    h, w = arr.shape[:2]
    labels = _quantize(arr, config["quantization_levels"])
    roots = _component_roots(labels)
    n = h * w

    areas = np.bincount(roots, minlength=n)
    ys = np.arange(n) // w
    xs = np.arange(n) % w
    min_x = np.full(n, w, dtype=np.int64)
    min_y = np.full(n, h, dtype=np.int64)
    max_x = np.full(n, -1, dtype=np.int64)
    max_y = np.full(n, -1, dtype=np.int64)
    np.minimum.at(min_x, roots, xs)
    np.minimum.at(min_y, roots, ys)
    np.maximum.at(max_x, roots, xs)
    np.maximum.at(max_y, roots, ys)
    sum_r = np.bincount(roots, weights=arr[:, :, 0].ravel().astype(np.float64), minlength=n)
    sum_g = np.bincount(roots, weights=arr[:, :, 1].ravel().astype(np.float64), minlength=n)
    sum_b = np.bincount(roots, weights=arr[:, :, 2].ravel().astype(np.float64), minlength=n)

    present = [r for r in range(n) if areas[r] >= config["min_region_area"]]
    # Deterministic region numbering: scan order by (min_y, min_x, -area).
    present.sort(key=lambda r: (min_y[r], min_x[r], -areas[r]))

    regions, failures = [], []
    for idx, r in enumerate(present):
        area = int(areas[r])
        bbox = (int(min_x[r]), int(min_y[r]), int(max_x[r]) + 1, int(max_y[r]) + 1)
        try:
            if area <= 0:
                raise WorkbenchError(E_REGION_INVALID, "zero-area region")
            x0, y0, x1, y1 = bbox
            if x0 < 0 or y0 < 0 or x1 > w or y1 > h:
                raise WorkbenchError(E_REGION_INVALID, f"bbox {bbox} escapes image {w}x{h}")
            bw, bh = x1 - x0, y1 - y0
            mean = (sum_r[r] / area, sum_g[r] / area, sum_b[r] / area)
            regions.append(RegionFeatures(
                image=image_name, region_index=idx, area=area,
                mean_color=mean, bbox=bbox, compactness=area / float(bw * bh),
            ))
        except WorkbenchError as exc:
            failures.append({
                "image": image_name, "region_index": idx, "stage": "extract",
                "code": exc.code, "detail": exc.detail,
            })
    return regions, failures
