"""Region masks, deterministic mask edits, and mask validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np

from .errors import BusinessError, ErrorCode
from .hashing import array_content_hash, sha256_json


def _normalize_mask_array(mask: Any) -> np.ndarray:
    array = np.asarray(mask)
    if array.ndim != 2:
        raise BusinessError(
            ErrorCode.REGION_OUT_OF_BOUNDS,
            "region mask must be a two-dimensional array",
            field="mask.shape",
            shape=list(array.shape),
        )
    if array.dtype != np.bool_:
        array = array != 0
    normalized = np.array(array, dtype=np.bool_, order="C", copy=True)
    normalized.setflags(write=False)
    return normalized


@dataclass(frozen=True)
class Region:
    region_id: int
    mask: np.ndarray

    def __post_init__(self) -> None:
        if (
            isinstance(self.region_id, (bool, np.bool_))
            or not isinstance(self.region_id, (int, np.integer))
            or int(self.region_id) < 0
        ):
            raise BusinessError(
                ErrorCode.INVALID_REGION_ID,
                "region id must be a non-negative integer",
                region_id=self.region_id,
                field="region_id",
            )
        object.__setattr__(self, "region_id", int(self.region_id))
        object.__setattr__(self, "mask", _normalize_mask_array(self.mask))

    @property
    def digest(self) -> str:
        return array_content_hash(self.mask)

    @property
    def pixel_count(self) -> int:
        return int(np.count_nonzero(self.mask))

    def bbox(self) -> tuple[int, int, int, int]:
        return mask_bbox(self.mask)

    def to_dict(self) -> dict[str, Any]:
        return {
            "region_id": self.region_id,
            "shape": list(self.mask.shape),
            "pixels": self.pixel_count,
            "digest": self.digest,
            "bbox": list(self.bbox()),
        }


@dataclass(frozen=True)
class MaskState:
    regions: tuple[Region, ...]

    def __post_init__(self) -> None:
        seen: set[int] = set()
        normalized: list[Region] = []
        for region in self.regions:
            if region.region_id in seen:
                raise BusinessError(
                    ErrorCode.DUPLICATE_REGION,
                    "duplicate region id",
                    region_id=region.region_id,
                    field="region_id",
                )
            seen.add(region.region_id)
            normalized.append(region)
        normalized.sort(key=lambda item: item.region_id)
        object.__setattr__(self, "regions", tuple(normalized))

    @classmethod
    def from_mapping(cls, mapping: Mapping[int, Any]) -> "MaskState":
        return cls(tuple(Region(region_id, mask) for region_id, mask in mapping.items()))

    @classmethod
    def from_records(cls, records: Iterable[Mapping[str, Any]]) -> "MaskState":
        seen: set[int] = set()
        regions: list[Region] = []
        for record in records:
            region_id = record.get("region_id")
            if region_id in seen:
                raise BusinessError(
                    ErrorCode.DUPLICATE_REGION,
                    "duplicate region id",
                    region_id=region_id,
                    field="region_id",
                )
            seen.add(region_id)
            regions.append(Region(region_id, record.get("mask")))
        return cls(tuple(regions))

    @classmethod
    def from_label_image(cls, labels: np.ndarray) -> "MaskState":
        array = np.asarray(labels)
        if array.ndim != 2:
            raise BusinessError(
                ErrorCode.REGION_OUT_OF_BOUNDS,
                "label image must be two-dimensional",
                field="labels",
                shape=list(array.shape),
            )
        regions = []
        for value in sorted(int(v) for v in np.unique(array) if int(v) != 0):
            regions.append(Region(value, array == value))
        return cls(tuple(regions))

    def by_id(self) -> dict[int, Region]:
        return {region.region_id: region for region in self.regions}

    @property
    def region_ids(self) -> tuple[int, ...]:
        return tuple(region.region_id for region in self.regions)

    @property
    def checksum(self) -> str:
        payload = [
            {"region_id": region.region_id, "digest": region.digest}
            for region in self.regions
        ]
        return sha256_json({"kind": "mask", "regions": payload})

    def to_dict(self) -> dict[str, Any]:
        return {
            "checksum": self.checksum,
            "regions": [region.to_dict() for region in self.regions],
        }


def mask_bbox(mask: np.ndarray) -> tuple[int, int, int, int]:
    rows = np.flatnonzero(np.any(mask, axis=1))
    cols = np.flatnonzero(np.any(mask, axis=0))
    if not len(rows) or not len(cols):
        return (0, 0, 0, 0)
    return (int(rows[0]), int(cols[0]), int(rows[-1] + 1), int(cols[-1] + 1))


def make_region_mask(
    shape: tuple[int, int],
    center: tuple[int, int],
    radii: tuple[int, int],
) -> np.ndarray:
    height, width = shape
    cy, cx = center
    ry, rx = radii
    yy, xx = np.ogrid[:height, :width]
    return (((yy - cy) / max(1, ry)) ** 2 + ((xx - cx) / max(1, rx)) ** 2) <= 1.0


def coerce_mask_state(value: Any) -> MaskState:
    """Accept a MaskState, id->mask mapping, record list, or label image."""

    if isinstance(value, MaskState):
        return value
    if isinstance(value, Mapping):
        return MaskState.from_mapping(value)
    if isinstance(value, np.ndarray):
        return MaskState.from_label_image(value)
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        items = list(value)
        if all(isinstance(item, Region) for item in items):
            return MaskState(tuple(items))
        if all(isinstance(item, Mapping) for item in items):
            return MaskState.from_records(items)
    raise BusinessError(
        ErrorCode.EMPTY_MASK,
        "unsupported mask payload",
        field="mask",
        actual_type=type(value).__name__,
    )


def validate_mask_state(
    mask_state: MaskState,
    canvas_shape: tuple[int, int],
    max_regions: int | None = None,
) -> None:
    if not isinstance(mask_state, MaskState):
        raise BusinessError(
            ErrorCode.EMPTY_MASK,
            "mask must be a MaskState",
            field="mask",
            actual_type=type(mask_state).__name__,
        )
    if not mask_state.regions:
        raise BusinessError(
            ErrorCode.EMPTY_MASK,
            "mask contains no regions",
            field="mask",
            region_id=None,
        )
    if max_regions is not None and len(mask_state.regions) > max_regions:
        raise BusinessError(
            ErrorCode.RESOURCE_LIMIT,
            "region count exceeds configured maximum",
            limit="limits.max_regions",
            actual=len(mask_state.regions),
            max_regions=max_regions,
        )

    occupied = np.zeros(canvas_shape, dtype=np.int32)
    for region in mask_state.regions:
        if region.mask.shape != canvas_shape:
            raise BusinessError(
                ErrorCode.REGION_OUT_OF_BOUNDS,
                "region mask shape does not match the canvas",
                region_id=region.region_id,
                field="mask.shape",
                expected=list(canvas_shape),
                actual=list(region.mask.shape),
            )
        if region.pixel_count == 0:
            raise BusinessError(
                ErrorCode.EMPTY_MASK,
                "region mask is empty",
                region_id=region.region_id,
                field="mask",
            )
        overlap = np.logical_and(occupied != 0, region.mask)
        if np.any(overlap):
            row, col = (int(v) for v in np.argwhere(overlap)[0])
            other = int(occupied[row, col])
            raise BusinessError(
                ErrorCode.MASK_OVERLAP,
                "region masks overlap; merge them explicitly",
                region_id=region.region_id,
                other_region_id=other,
                field="mask",
                coordinate=[row, col],
            )
        occupied[region.mask] = region.region_id + 1


def context_bbox(
    region_bbox: tuple[int, int, int, int],
    radius: int,
    canvas_shape: tuple[int, int],
) -> tuple[int, int, int, int]:
    y0, x0, y1, x1 = region_bbox
    height, width = canvas_shape
    return (
        max(0, y0 - radius),
        max(0, x0 - radius),
        min(height, y1 + radius),
        min(width, x1 + radius),
    )


def estimate_resident_pixels(
    mask_state: MaskState,
    radius: int,
    canvas_shape: tuple[int, int],
    resident_node_copies: int,
) -> int:
    total = 0
    for region in mask_state.regions:
        y0, x0, y1, x1 = context_bbox(region.bbox(), radius, canvas_shape)
        total += (y1 - y0) * (x1 - x0) * resident_node_copies
    return int(total)


def _dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    if radius <= 0:
        return mask.copy()
    height, width = mask.shape
    result = np.zeros_like(mask, dtype=np.bool_)
    offsets = [
        (dy, dx)
        for dy in range(-radius, radius + 1)
        for dx in range(-radius, radius + 1)
        if dy * dy + dx * dx <= radius * radius
    ]
    for dy, dx in offsets:
        src_y0 = max(0, -dy)
        src_y1 = min(height, height - dy)
        src_x0 = max(0, -dx)
        src_x1 = min(width, width - dx)
        dst_y0 = max(0, dy)
        dst_y1 = min(height, height + dy)
        dst_x0 = max(0, dx)
        dst_x1 = min(width, width + dx)
        if src_y1 > src_y0 and src_x1 > src_x0:
            result[dst_y0:dst_y1, dst_x0:dst_x1] |= mask[src_y0:src_y1, src_x0:src_x1]
    return result


def _erode(mask: np.ndarray, radius: int) -> np.ndarray:
    if radius <= 0:
        return mask.copy()
    return ~_dilate(~mask, radius)


class MaskEditor:
    """Pure mask edits. Validation remains the caller's responsibility."""

    @staticmethod
    def grow(mask_state: MaskState, region_id: int, radius: int) -> MaskState:
        radius = _positive_edit_radius(radius)
        found = False
        regions: list[Region] = []
        for region in mask_state.regions:
            if region.region_id == region_id:
                found = True
                regions.append(Region(region.region_id, _dilate(region.mask, radius)))
            else:
                regions.append(region)
        _require_region(found, region_id)
        return MaskState(tuple(regions))

    @staticmethod
    def shrink(mask_state: MaskState, region_id: int, radius: int) -> MaskState:
        radius = _positive_edit_radius(radius)
        found = False
        regions: list[Region] = []
        for region in mask_state.regions:
            if region.region_id == region_id:
                found = True
                regions.append(Region(region.region_id, _erode(region.mask, radius)))
            else:
                regions.append(region)
        _require_region(found, region_id)
        return MaskState(tuple(regions))

    @staticmethod
    def merge(mask_state: MaskState, region_ids: Iterable[int], target_id: int | None = None) -> MaskState:
        ids = tuple(int(value) for value in region_ids)
        if not ids:
            raise BusinessError(
                ErrorCode.INVALID_CONFIG,
                "merge requires at least one region id",
                field="region_ids",
            )
        existing = mask_state.by_id()
        for region_id in ids:
            if region_id not in existing:
                raise BusinessError(
                    ErrorCode.INVALID_CONFIG,
                    "cannot merge an unknown region",
                    region_id=region_id,
                    field="region_ids",
                )
        target = int(target_id if target_id is not None else ids[0])
        union = np.zeros_like(next(iter(existing.values())).mask, dtype=np.bool_)
        union_ids = set(ids)
        if target in existing:
            union_ids.add(target)
        for region_id in sorted(union_ids):
            union |= existing[region_id].mask
        regions = [
            region
            for region in mask_state.regions
            if region.region_id not in union_ids
        ]
        regions.append(Region(target, union))
        return MaskState(tuple(regions))


def _positive_edit_radius(radius: int) -> int:
    if isinstance(radius, (bool, np.bool_)) or not isinstance(radius, (int, np.integer)):
        raise BusinessError(
            ErrorCode.INVALID_CONFIG,
            "edit radius must be a positive integer",
            field="radius",
            value=radius,
        )
    radius = int(radius)
    if radius <= 0:
        raise BusinessError(
            ErrorCode.INVALID_CONFIG,
            "edit radius must be positive",
            field="radius",
            value=radius,
        )
    return radius


def _require_region(found: bool, region_id: int) -> None:
    if not found:
        raise BusinessError(
            ErrorCode.INVALID_CONFIG,
            "unknown region id",
            region_id=region_id,
            field="region_id",
        )
