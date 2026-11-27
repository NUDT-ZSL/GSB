"""Input loading, business validation, and print-grid derivation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .errors import BusinessError, require
from .io import read_json, sha256_file


@dataclass(slots=True)
class InputBundle:
    sample_dir: Path
    gray: np.ndarray
    mask: np.ndarray
    sample: dict[str, Any]
    details: dict[str, Any]
    config: dict[str, Any]
    print_grid: dict[str, Any]
    part_specs: dict[int, dict[str, Any]]
    detail_regions: list[dict[str, Any]]

    @property
    def height(self) -> int:
        return int(self.gray.shape[0])

    @property
    def width(self) -> int:
        return int(self.gray.shape[1])

    @property
    def background_label(self) -> int:
        return int(self.details.get("mask", {}).get("background_label", 0))

    def bbox_for_label(self, label: int) -> list[int] | None:
        ys, xs = np.nonzero(self.mask == int(label))
        if xs.size == 0:
            return None
        x0, x1 = int(xs.min()), int(xs.max())
        y0, y1 = int(ys.min()), int(ys.max())
        return [x0, y0, x1 - x0 + 1, y1 - y0 + 1]


def resolve_sample_path(sample: str | Path) -> Path:
    path = Path(sample)
    if path.is_file() and path.name.endswith(".json"):
        return path
    candidate = path / "sample.json"
    if candidate.exists():
        return candidate
    if path.exists() and path.is_dir():
        raise BusinessError(
            "E_SAMPLE_NOT_FOUND",
            f"样本目录缺少 sample.json: {path}",
            details={"sample_dir": str(path)},
        )
    raise BusinessError("E_SAMPLE_NOT_FOUND", f"找不到样本: {path}", details={"sample": str(path)})


def _load_npy(path: Path, code: str) -> np.ndarray:
    try:
        return np.load(path, allow_pickle=False)
    except FileNotFoundError as exc:
        raise BusinessError(code, f"文件不存在: {path}", details={"path": str(path)}) from exc
    except (ValueError, OSError) as exc:
        raise BusinessError("E_METADATA_INVALID", f"无法读取矩阵: {path}: {exc}") from exc


def _as_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise BusinessError("E_METADATA_INVALID", f"{field} 必须是整数")
    return int(value)


def _as_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise BusinessError("E_METADATA_INVALID", f"{field} 必须是数值")
    return float(value)


def _validate_bbox(bbox: Any, shape: tuple[int, int], field: str) -> list[int]:
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise BusinessError("E_DETAIL_REGION", f"{field} 必须是 [x, y, width, height]")
    values = [_as_int(item, field) for item in bbox]
    x, y, width, height = values
    if width <= 0 or height <= 0 or x < 0 or y < 0 or x + width > shape[1] or y + height > shape[0]:
        raise BusinessError(
            "E_DETAIL_REGION",
            f"{field} 越界: {values}",
            details={"bbox": values, "image_shape": list(shape)},
        )
    return values


def _derive_print_grid(width: int, height: int, config: dict[str, Any]) -> dict[str, Any]:
    print_cfg = config["print"]
    board_w = _as_float(print_cfg["board_width_mm"], "print.board_width_mm")
    board_h = _as_float(print_cfg["board_height_mm"], "print.board_height_mm")
    lpi = _as_float(print_cfg["lines_per_inch"], "print.lines_per_inch")
    dot_mm = _as_float(print_cfg["ink_dot_diameter_mm"], "print.ink_dot_diameter_mm")
    require(lpi > 0.0, "E_PRINT_LPI", "lines_per_inch 必须大于 0", lines_per_inch=lpi)
    require(
        board_w > 0.0 and board_h > 0.0,
        "E_PRINT_GEOMETRY",
        "展板物理宽高必须大于 0",
        board_width_mm=board_w,
        board_height_mm=board_h,
    )
    require(
        dot_mm > 0.0,
        "E_PRINT_DOT_SIZE",
        "墨点直径必须大于 0",
        ink_dot_diameter_mm=dot_mm,
    )

    dpi_x = width / (board_w / 25.4)
    dpi_y = height / (board_h / 25.4)
    aspect_tolerance = float(print_cfg.get("aspect_tolerance", 0.03))
    aspect_error = abs(dpi_x - dpi_y) / max(dpi_x, dpi_y)
    require(
        aspect_error <= aspect_tolerance,
        "E_PRINT_GEOMETRY",
        "图像像素纵横比与展板物理纵横比不一致，网点单元会变成矩形",
        dpi_x=dpi_x,
        dpi_y=dpi_y,
        aspect_error=aspect_error,
        tolerance=aspect_tolerance,
    )

    cell_x = dpi_x / lpi
    cell_y = dpi_y / lpi
    require(
        cell_x >= 1.0 and cell_y >= 1.0,
        "E_PRINT_LPI",
        "打印线数过高，网点栅格间距不足 1 像素",
        lines_per_inch=lpi,
        cell_pitch_px=[cell_x, cell_y],
    )
    dot_px = dot_mm / 25.4 * 0.5 * (dpi_x + dpi_y)
    dot_to_cell_tolerance = float(print_cfg.get("dot_to_cell_tolerance", 0.03))
    require(
        dot_px <= min(cell_x, cell_y) * (1.0 + dot_to_cell_tolerance),
        "E_PRINT_DOT_SIZE",
        "墨点直径大于网点单元节距，打印时会发生非受控重叠",
        dot_px=dot_px,
        cell_pitch_px=[cell_x, cell_y],
        tolerance=dot_to_cell_tolerance,
    )
    cell_area = cell_x * cell_y
    dot_area = math.pi * (dot_px / 2.0) ** 2
    return {
        "image_width_px": width,
        "image_height_px": height,
        "board_width_mm": board_w,
        "board_height_mm": board_h,
        "lines_per_inch": lpi,
        "ink_dot_diameter_mm": dot_mm,
        "dpi_x": dpi_x,
        "dpi_y": dpi_y,
        "dpi": 0.5 * (dpi_x + dpi_y),
        "screen_pitch_px": [float(cell_x), float(cell_y)],
        "screen_cell_area_px2": float(cell_area),
        "ink_dot_diameter_px": float(dot_px),
        "ink_dot_area_px2": float(dot_area),
        "dot_area_fraction_of_cell": float(min(1.0, dot_area / cell_area)),
        "aspect_error": float(aspect_error),
        "aspect_tolerance": float(aspect_tolerance),
    }


def load_and_validate(
    sample: str | Path,
    config: dict[str, Any],
    *,
    verify_hashes: bool = False,
) -> InputBundle:
    sample_json = resolve_sample_path(sample)
    sample_doc = read_json(sample_json)
    require(
        sample_doc.get("schema") == "clock-halftone-sample/v1",
        "E_METADATA_INVALID",
        "sample.json schema 不受支持",
        schema=sample_doc.get("schema"),
    )
    files = sample_doc.get("files")
    if not isinstance(files, dict):
        raise BusinessError("E_METADATA_INVALID", "sample.json 缺少 files 对象")
    base = sample_json.parent
    gray_path = base / str(files.get("gray", "gray.npy"))
    mask_path = base / str(files.get("mask", "mask.npy"))
    details_path = base / str(files.get("details", "details.json"))
    require(gray_path.exists(), "E_GRAY_NOT_FOUND", f"缺少灰阶矩阵: {gray_path}")
    require(mask_path.exists(), "E_MASK_NOT_FOUND", f"缺少零件掩码: {mask_path}")
    require(details_path.exists(), "E_DETAILS_NOT_FOUND", f"缺少细节标注: {details_path}")
    if verify_hashes:
        expected_hashes = sample_doc.get("hashes")
        if isinstance(expected_hashes, dict):
            hash_failures: list[dict[str, str]] = []
            for filename, path in (
                ("gray.npy", gray_path),
                ("mask.npy", mask_path),
                ("details.json", details_path),
            ):
                expected = expected_hashes.get(filename)
                if isinstance(expected, str) and sha256_file(path) != expected:
                    hash_failures.append(
                        {
                            "file": filename,
                            "expected": expected,
                            "actual": sha256_file(path),
                        }
                    )
            require(
                not hash_failures,
                "E_INPUT_HASH",
                "输入文件哈希与 sample.json 不一致",
                hash_failures=hash_failures,
            )

    details = read_json(details_path)
    require(
        details.get("schema") == "clock-halftone-details/v1",
        "E_METADATA_INVALID",
        "details.json schema 不受支持",
        schema=details.get("schema"),
    )
    canvas = details.get("canvas")
    if not isinstance(canvas, dict):
        raise BusinessError("E_METADATA_INVALID", "details.json 缺少 canvas")
    meta_w = _as_int(canvas.get("width"), "canvas.width")
    meta_h = _as_int(canvas.get("height"), "canvas.height")
    bit_depth = _as_int(canvas.get("bit_depth"), "canvas.bit_depth")
    expected_depth = _as_int(config["validation"].get("expected_bit_depth", 8), "expected_bit_depth")
    require(
        bit_depth == expected_depth,
        "E_GRAY_DEPTH",
        f"元数据位深 {bit_depth} 与期望 {expected_depth} 不一致",
        bit_depth=bit_depth,
        expected_bit_depth=expected_depth,
    )

    gray = _load_npy(gray_path, "E_GRAY_NOT_FOUND")
    mask_raw = _load_npy(mask_path, "E_MASK_NOT_FOUND")
    require(gray.ndim == 2, "E_IMAGE_SIZE", "灰阶矩阵必须是二维")
    require(mask_raw.ndim == 2, "E_MASK_SIZE", "掩码必须是二维矩阵")
    if gray.dtype != np.uint8:
        raise BusinessError(
            "E_GRAY_DEPTH",
            f"灰阶矩阵 dtype={gray.dtype}，仅支持 uint8",
            details={"dtype": str(gray.dtype)},
        )
    if not np.issubdtype(mask_raw.dtype, np.integer):
        raise BusinessError(
            "E_MASK_UNKNOWN_LABEL",
            f"掩码 dtype={mask_raw.dtype}，必须是整数标签",
            details={"dtype": str(mask_raw.dtype)},
        )
    if gray.shape != mask_raw.shape:
        raise BusinessError(
            "E_MASK_SIZE",
            "灰阶矩阵与掩码尺寸不一致",
            details={"gray_shape": list(gray.shape), "mask_shape": list(mask_raw.shape)},
        )
    height, width = gray.shape
    if (width, height) != (meta_w, meta_h):
        raise BusinessError(
            "E_IMAGE_SIZE",
            "灰阶矩阵尺寸与元数据不一致",
            details={
                "gray_shape": [height, width],
                "metadata_shape": [meta_h, meta_w],
            },
        )
    sample_canvas = sample_doc.get("canvas", {})
    if isinstance(sample_canvas, dict) and (
        _as_int(sample_canvas.get("width"), "sample.canvas.width") != width
        or _as_int(sample_canvas.get("height"), "sample.canvas.height") != height
    ):
        raise BusinessError("E_IMAGE_SIZE", "灰阶矩阵尺寸与 sample.json 不一致")

    mask = mask_raw.astype(np.int64, copy=False)
    mask_meta = details.get("mask")
    if not isinstance(mask_meta, dict):
        raise BusinessError("E_METADATA_INVALID", "details.json 缺少 mask")
    declared_labels_raw = mask_meta.get("labels")
    if not isinstance(declared_labels_raw, list) or not declared_labels_raw:
        raise BusinessError("E_METADATA_INVALID", "mask.labels 必须是非空数组")
    declared_labels = {_as_int(item, "mask.labels[]") for item in declared_labels_raw}
    background_label = _as_int(mask_meta.get("background_label", 0), "mask.background_label")
    missing_label = _as_int(mask_meta.get("missing_label", -1), "mask.missing_label")
    require(background_label in declared_labels, "E_METADATA_INVALID", "背景标签未登记")

    if missing_label not in declared_labels and np.any(mask == missing_label):
        locations = np.argwhere(mask == missing_label)
        raise BusinessError(
            "E_MASK_MISSING_PIXELS",
            "掩码包含标记为缺失的像素",
            details={
                "missing_label": missing_label,
                "count": int(locations.shape[0]),
                "first_pixels": locations[:8].tolist(),
            },
        )
    unknown = sorted(int(x) for x in np.unique(mask) if int(x) not in declared_labels)
    if unknown:
        raise BusinessError(
            "E_MASK_UNKNOWN_LABEL",
            "掩码中出现未登记标签",
            details={"unknown_labels": unknown, "declared_labels": sorted(declared_labels)},
        )
    actual_labels = {int(x) for x in np.unique(mask)}
    absent = sorted(label for label in declared_labels if label not in actual_labels)
    if absent:
        raise BusinessError(
            "E_MASK_LABEL_MISSING",
            "掩码缺少已声明零件的像素",
            details={"absent_labels": absent, "actual_labels": sorted(actual_labels)},
        )

    parts_raw = details.get("parts")
    if not isinstance(parts_raw, list) or not parts_raw:
        raise BusinessError("E_METADATA_INVALID", "details.json 缺少 parts")
    allowed_textures = set(config["validation"].get("allowed_texture_classes", []))
    part_specs: dict[int, dict[str, Any]] = {}
    for item in parts_raw:
        if not isinstance(item, dict):
            raise BusinessError("E_METADATA_INVALID", "parts[] 必须是对象")
        label = _as_int(item.get("label"), "parts.label")
        require(label != background_label, "E_MASK_UNKNOWN_LABEL", "零件不能使用背景标签", label=label)
        require(label in declared_labels, "E_MASK_UNKNOWN_LABEL", f"零件标签 {label} 未在 mask.labels 登记")
        require(label not in part_specs, "E_METADATA_INVALID", f"零件标签重复: {label}", label=label)
        texture = item.get("texture_class")
        require(texture in allowed_textures, "E_METADATA_INVALID", f"零件 {label} 纹理类别无效: {texture}")
        target = _as_float(item.get("target_ink_density"), f"parts[{label}].target_ink_density")
        require(
            config["density"]["min_target"] <= target <= config["density"]["max_target"],
            "E_DENSITY_BOUNDS",
            f"零件 {label} 目标密度越界: {target}",
            label=label,
            target_ink_density=target,
        )
        if int(np.count_nonzero(mask == label)) == 0:
            raise BusinessError(
                "E_MASK_LABEL_MISSING",
                f"零件 {label} 在掩码中没有像素",
                details={"label": label},
            )
        spec = dict(item)
        spec["label"] = label
        spec["target_ink_density"] = target
        part_specs[label] = spec
    declared_part_labels = {int(x) for x in mask_meta.get("part_labels", [])}
    if declared_part_labels and declared_part_labels != set(part_specs):
        raise BusinessError(
            "E_MASK_UNKNOWN_LABEL",
            "mask.part_labels 与 parts 定义不一致",
            details={
                "mask_part_labels": sorted(declared_part_labels),
                "parts_labels": sorted(part_specs),
            },
        )

    regions_raw = details.get("detail_regions")
    if not isinstance(regions_raw, list) or not regions_raw:
        raise BusinessError("E_DETAIL_REGION", "details.json 缺少 detail_regions")
    detail_regions: list[dict[str, Any]] = []
    region_ids: set[str] = set()
    for region_raw in regions_raw:
        if not isinstance(region_raw, dict):
            raise BusinessError("E_DETAIL_REGION", "detail_regions[] 必须是对象")
        region = dict(region_raw)
        region_id = str(region.get("id", ""))
        if not region_id or region_id in region_ids:
            raise BusinessError("E_DETAIL_REGION", f"细节区域 id 缺失或重复: {region_id}")
        region_ids.add(region_id)
        label = _as_int(region.get("part_label"), f"detail_regions[{region_id}].part_label")
        require(label in part_specs, "E_DETAIL_REGION", f"细节区域 {region_id} 的零件标签未定义")
        kind = region.get("kind")
        require(kind in {"tooth", "spiral", "bore"}, "E_DETAIL_REGION", f"细节区域 {region_id} kind 无效")
        bbox = _validate_bbox(region.get("bbox"), gray.shape, f"detail_regions[{region_id}].bbox")
        region["bbox"] = bbox
        x, y, w, h = bbox
        region_mask = mask[y : y + h, x : x + w]
        require(
            np.any(region_mask == label),
            "E_MASK_MISSING_PIXELS",
            f"细节区域 {region_id} 未覆盖所属零件像素",
            details={"region_id": region_id, "part_label": label, "bbox": bbox},
        )
        if kind == "bore":
            center = region.get("center")
            if not isinstance(center, list) or len(center) != 2:
                raise BusinessError("E_DETAIL_REGION", f"轴心区域 {region_id} 缺少 center")
            cx, cy = _as_int(center[0], "center.x"), _as_int(center[1], "center.y")
            require(
                x <= cx < x + w and y <= cy < y + h,
                "E_DETAIL_REGION",
                f"轴心区域 {region_id} 的圆心不在 bbox 内",
            )
        detail_regions.append(region)

    print_grid = _derive_print_grid(width, height, config)
    return InputBundle(
        sample_dir=base,
        gray=gray,
        mask=mask,
        sample=sample_doc,
        details=details,
        config=config,
        print_grid=print_grid,
        part_specs=part_specs,
        detail_regions=detail_regions,
    )


def build_input_analysis(bundle: InputBundle) -> dict[str, Any]:
    mask = bundle.mask
    gray = bundle.gray
    total = int(mask.size)
    parts: list[dict[str, Any]] = []
    for label, spec in sorted(bundle.part_specs.items()):
        part_mask = mask == label
        values = gray[part_mask]
        bbox = bundle.bbox_for_label(label)
        if bbox is None:
            raise BusinessError("E_MASK_LABEL_MISSING", f"零件 {label} 没有像素", details={"label": label})
        bins = np.bincount(values, minlength=256).astype(np.int64)
        source_ink = 1.0 - values.astype(np.float64) / 255.0
        cell_area = bundle.print_grid["screen_cell_area_px2"]
        bbox_area = int(bbox[2] * bbox[3])
        parts.append(
            {
                "label": label,
                "name": spec.get("name"),
                "display_name": spec.get("display_name"),
                "texture_class": spec.get("texture_class"),
                "target_ink_density": float(spec["target_ink_density"]),
                "source_ink_mean": float(source_ink.mean()),
                "bbox_xywh": bbox,
                "pixel_count": int(part_mask.sum()),
                "area_ratio_of_canvas": float(part_mask.sum() / total),
                "area_ratio_of_part_mask": float(part_mask.sum() / max(1, total - int(np.count_nonzero(mask == bundle.background_label))),
                ),
                "gray_histogram_256": bins.tolist(),
                "gray_range": [int(values.min()), int(values.max())],
                "print_cells_floor_by_area": int(math.floor(part_mask.sum() / cell_area)),
                "print_cells_by_bbox": [
                    int(math.floor(bbox[2] / bundle.print_grid["screen_pitch_px"][0])),
                    int(math.floor(bbox[3] / bundle.print_grid["screen_pitch_px"][1])),
                ],
                "ink_dots_capacity_estimate": int(
                    math.floor(part_mask.sum() * bundle.print_grid["dot_area_fraction_of_cell"] / cell_area)
                ),
            }
        )

    actual_labels = sorted(int(x) for x in np.unique(mask))
    background_pixels = int(np.count_nonzero(mask == bundle.background_label))
    checks = {
        "image_shape": [bundle.height, bundle.width],
        "gray_dtype": str(gray.dtype),
        "bit_depth": int(bundle.details["canvas"]["bit_depth"]),
        "mask_dtype": str(mask.dtype),
        "mask_label_set": actual_labels,
        "declared_label_set": sorted(int(x) for x in bundle.details["mask"]["labels"]),
        "background_pixels": background_pixels,
        "background_ratio": float(background_pixels / total),
        "missing_pixels": 0,
        "out_of_range_labels": [],
        "detail_regions_checked": len(bundle.detail_regions),
        "hash_verification": "not_requested",
    }
    sample_hashes = bundle.sample.get("hashes", {})
    if isinstance(sample_hashes, dict):
        checks["input_sha256"] = {
            name: sha256_file(bundle.sample_dir / name)
            for name in ("gray.npy", "mask.npy", "details.json")
            if (bundle.sample_dir / name).exists() and name in sample_hashes
        }
    return {
        "schema": "clock-halftone-input-analysis/v1",
        "ok": True,
        "sample_id": bundle.sample.get("sample_id"),
        "sample_dir": str(bundle.sample_dir.resolve()),
        "seed": bundle.sample.get("seed", bundle.details.get("seed")),
        "print_grid": bundle.print_grid,
        "canvas": {
            "width": bundle.width,
            "height": bundle.height,
            "total_pixels": total,
            "bit_depth": int(bundle.details["canvas"]["bit_depth"]),
        },
        "parts": parts,
        "detail_regions": bundle.detail_regions,
        "checks": checks,
    }
