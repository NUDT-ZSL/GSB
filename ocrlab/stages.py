"""The four deterministic OCR stages and their degradation policies."""

from __future__ import annotations

import base64

import numpy as np

from .config import PipelineConfig
from .errors import (
    E_CHAR_SEGMENTATION_FAILED,
    E_INVALID_LINE_BOX,
    E_INVALID_PIXEL,
    E_INVALID_THRESHOLD,
    E_NO_LINES_DETECTED,
    E_PIXEL_OUT_OF_RANGE,
    E_RESOURCE_LIMIT,
    E_TEMPLATE_SHAPE_MISMATCH,
    E_TOLERANCE_EXCEEDED,
    E_UNATTRIBUTED,
    E_UNKNOWN_CHARSET_VERSION,
)
from .injection import Degradation, InjectionSpec, InjectionTracker, Issue
from .recognition import (
    LineContext,
    build_line_context,
    compare_recognition,
    recognize_lines,
)
from .samples import render_page
from .serialization import checksum, sha256_bytes
from .templates import TemplateLibrary


STAGE_NAMES = {1: "preprocess", 2: "detect", 3: "recognize", 4: "aggregate"}


def _b64(array: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(array).tobytes()).decode("ascii")


def _unpack_b64(payload: str, shape: list[int], dtype: str) -> np.ndarray:
    raw = base64.b64decode(payload.encode("ascii"))
    return np.frombuffer(raw, dtype=np.dtype(dtype)).reshape(tuple(shape)).copy()


def _status(issues: list[Issue], degradations: list[Degradation]) -> str:
    if issues or degradations:
        return "degraded"
    return "ok"


def _injection_message(code: str) -> str:
    return {
        E_PIXEL_OUT_OF_RANGE: "pixel value is outside the valid uint8 range",
        E_INVALID_LINE_BOX: "line box is outside the canvas",
        E_TEMPLATE_SHAPE_MISMATCH: "character template shape does not match the configured feature shape",
        E_UNKNOWN_CHARSET_VERSION: "unknown charset version triggered by injection",
        E_INVALID_THRESHOLD: "recognition or aggregation threshold is non-positive",
        E_RESOURCE_LIMIT: "configured recognition resource limit was exceeded",
        E_NO_LINES_DETECTED: "line detection produced no boxes",
    }.get(code, f"injected business failure: {code}")


def _make_issue(
    stage: str,
    code: str,
    page_id: str,
    *,
    line_no: int | None = None,
    field: str | None = None,
    details: dict | None = None,
    injected: bool = False,
    attributed: bool = True,
) -> Issue:
    return Issue(
        stage=stage,
        code=code,
        message=_injection_message(code),
        page_id=page_id,
        line_no=line_no,
        field=field,
        details=details or {},
        injected=injected,
        attributed=attributed,
    )


def _unattributed_issue(stage: str, page_id: str, exception: Exception) -> dict:
    return _make_issue(
        stage,
        E_UNATTRIBUTED,
        page_id,
        field="exception",
        details={"exception_type": type(exception).__name__},
        attributed=False,
    ).to_dict()


def _unattributed_degradation(stage: str, page_id: str) -> dict:
    return Degradation(
        stage=stage,
        category="unattributed_failure",
        trigger_code=E_UNATTRIBUTED,
        page_id=page_id,
    ).to_dict()


def unattributed_preprocess_page(
    page: dict,
    config: PipelineConfig,
    exception: Exception,
) -> dict:
    page_id = page["page_id"]
    shape = [int(value) for value in page["image_shape"]]
    gray = np.full(tuple(shape), 255, dtype=np.uint8)
    binary = np.zeros(tuple(shape), dtype=bool)
    return {
        "page_id": page_id,
        "shape": shape,
        "gray_b64": _b64(gray),
        "binary_b64": _b64(np.packbits(binary, axis=None)),
        "binary_shape": shape,
        "binary_sha256": sha256_bytes(np.packbits(binary, axis=None).tobytes()),
        "ink_pixels": 0,
        "threshold": int(config.binarize_threshold),
        "status": "error",
        "issues": [_unattributed_issue("preprocess", page_id, exception)],
        "degradations": [_unattributed_degradation("preprocess", page_id)],
        "injection_records": [],
    }


def unattributed_detect_page(
    page: dict,
    exception: Exception,
) -> dict:
    page_id = page["page_id"]
    boxes = _truth_boxes(page)
    for index, box in enumerate(boxes):
        box["line_index"] = index
    return {
        "page_id": page_id,
        "boxes": boxes,
        "line_count": len(boxes),
        "status": "error",
        "issues": [_unattributed_issue("detect", page_id, exception)],
        "degradations": [_unattributed_degradation("detect", page_id)],
        "injection_records": [],
    }


def unattributed_recognize_page(
    page: dict,
    detected: dict,
    library: TemplateLibrary,
    exception: Exception,
) -> dict:
    page_id = page["page_id"]
    lines = []
    for index, box in enumerate(detected.get("boxes", [])):
        truth = page["lines"][index] if index < len(page["lines"]) else None
        text = str(truth["text"]) if truth else ""
        chars = [
            {
                "index": char_index,
                "char": character,
                "score": 0.0,
                "candidate_comparisons": 0,
                "candidates": [
                    {
                        "char": character,
                        "score": 0.0,
                        "template_level": "truth",
                        "template_shape": list(library.high_shape),
                    }
                ],
                "crop_box": [box["x0"], box["y0"], box["x1"], box["y1"]],
                "fallback": "truth_text_after_unattributed_error",
            }
            for char_index, character in enumerate(text)
        ]
        lines.append(
            {
                "line_index": index,
                "box": [box["x0"], box["y0"], box["x1"], box["y1"]],
                "text": text,
                "confidence": 0.0,
                "mean": 0.0,
                "std": 1.0,
                "candidate_comparisons": 0,
                "char_count": len(chars),
                "chars": chars,
                "fallback": "truth_text_after_unattributed_error",
                "fallback_trigger_code": E_UNATTRIBUTED,
            }
        )
    comparison = {
        "ok": False,
        "first_difference": None,
        "line_differences": [],
        "character_differences": [],
        "difference_count": 0,
        "max_score_delta": 0.0,
        "max_coordinate_deviation": 0.0,
        "score_tolerance": 0.0,
        "coordinate_tolerance": 0.0,
        "lines_checked": 0,
    }
    return {
        "page_id": page_id,
        "charset": library.charset_versions[sorted(library.charset_versions)[0]],
        "charset_version": page.get("charset_version"),
        "lines": lines,
        "reference_lines": lines,
        "comparison": comparison,
        "candidate_comparisons": 0,
        "resources": {},
        "status": "error",
        "issues": [_unattributed_issue("recognize", page_id, exception)],
        "degradations": [_unattributed_degradation("recognize", page_id)],
        "injection_records": [],
    }


def unattributed_aggregate_page(
    page: dict,
    recognized: dict,
    exception: Exception,
) -> dict:
    page_id = page["page_id"]
    lines = recognized.get("lines", [])
    text = "".join(line.get("text", "") for line in lines)
    return {
        "page_id": page_id,
        "text": text,
        "metrics": {
            "page_id": page_id,
            "line_count": len(lines),
            "expected_line_count": int(page.get("line_count", 0)),
            "line_count_delta": len(lines) - int(page.get("line_count", 0)),
            "char_count": len(text),
            "mean_confidence": 0.0,
            "threshold": 0.0,
            "low_confidence_line_count": len(lines),
            "low_confidence_lines": list(range(len(lines))),
            "candidate_comparisons": 0,
            "max_coordinate_deviation": 0.0,
            "comparison_ok": False,
        },
        "lines": lines,
        "reference_lines": recognized.get("reference_lines", []),
        "comparison": recognized.get("comparison", {}),
        "charset": recognized.get("charset"),
        "charset_version": recognized.get("charset_version"),
        "status": "error",
        "issues": [_unattributed_issue("aggregate", page_id, exception)],
        "degradations": [_unattributed_degradation("aggregate", page_id)],
        "injection_records": [],
    }


def preprocess_page(
    page: dict,
    config: PipelineConfig,
    library: TemplateLibrary,
    tracker: InjectionTracker,
) -> dict:
    page_id = page["page_id"]
    gray = render_page(page, library)
    issues: list[Issue] = []
    degradations: list[Degradation] = []
    injection_records: list[dict] = []

    spec = tracker.find("preprocess", page_id, None)
    if spec is not None:
        if spec.code in {E_PIXEL_OUT_OF_RANGE, E_INVALID_PIXEL}:
            bad_gray = gray.astype(np.int16)
            value = int(spec.params.get("value", 300))
            y = int(spec.params.get("y", 0))
            x = int(spec.params.get("x", 0))
            if 0 <= y < bad_gray.shape[0] and 0 <= x < bad_gray.shape[1]:
                bad_gray[y, x] = value
            injection_records.append(
                tracker.apply(
                    spec,
                    details={"pixel": [x, y], "value": value},
                )
            )
            issues.append(
                _make_issue(
                    "preprocess",
                    E_PIXEL_OUT_OF_RANGE,
                    page_id,
                    field="pixels",
                    details={"pixel": [x, y], "value": value, "allowed": [0, 255]},
                    injected=True,
                )
            )
            gray = np.clip(bad_gray, 0, 255).astype(np.uint8)
        else:
            injection_records.append(tracker.apply(spec))
            issues.append(
                _make_issue(
                    "preprocess",
                    spec.code,
                    page_id,
                    field="params",
                    details=spec.params,
                    injected=True,
                )
            )

    if gray.dtype != np.uint8 or int(gray.min()) < 0 or int(gray.max()) > 255:
        if not issues:
            issues.append(
                _make_issue(
                    "preprocess",
                    E_INVALID_PIXEL,
                    page_id,
                    field="pixels",
                    details={"min": int(gray.min()), "max": int(gray.max())},
                )
            )
        gray = np.clip(gray, 0, 255).astype(np.uint8)

    threshold = int(config.binarize_threshold)
    binary = gray < threshold
    if issues:
        degradations.append(
            Degradation(
                stage="preprocess",
                category="clamped_binarization",
                trigger_code=issues[0].code,
                page_id=page_id,
            )
        )

    return {
        "page_id": page_id,
        "shape": list(gray.shape),
        "gray_b64": _b64(gray),
        "binary_b64": _b64(np.packbits(binary, axis=None)),
        "binary_shape": list(binary.shape),
        "binary_sha256": sha256_bytes(np.packbits(binary, axis=None).tobytes()),
        "ink_pixels": int(binary.sum()),
        "threshold": threshold,
        "status": _status(issues, degradations),
        "issues": [issue.to_dict() for issue in issues],
        "degradations": [item.to_dict() for item in degradations],
        "injection_records": injection_records,
    }


def _binary_from_preprocessed(page: dict) -> np.ndarray:
    packed = np.frombuffer(base64.b64decode(page["binary_b64"]), dtype=np.uint8)
    shape = tuple(int(value) for value in page["binary_shape"])
    return np.unpackbits(packed)[: shape[0] * shape[1]].reshape(shape).astype(bool)


def _detect_boxes(binary: np.ndarray, config: PipelineConfig) -> list[dict]:
    row_ink = binary.sum(axis=1)
    active_rows = row_ink >= int(config.line_min_ink)
    boxes: list[dict] = []
    height, width = binary.shape
    start: int | None = None
    for y, active in enumerate(active_rows):
        if bool(active) and start is None:
            start = y
        elif not bool(active) and start is not None:
            boxes.append(_box_from_band(binary, start, y, width, config))
            start = None
    if start is not None:
        boxes.append(_box_from_band(binary, start, height, width, config))
    boxes = [box for box in boxes if box["x1"] > box["x0"] and box["y1"] > box["y0"]]
    boxes.sort(key=lambda item: (-item["score"], item["y0"], item["x0"]))
    return boxes


def _box_from_band(
    binary: np.ndarray,
    y0: int,
    y1: int,
    width: int,
    config: PipelineConfig,
) -> dict:
    band = binary[y0:y1, :]
    active_columns = np.flatnonzero(band.any(axis=0))
    if active_columns.size == 0:
        x0, x1 = 0, 0
    else:
        x0 = int(active_columns[0])
        x1 = int(active_columns[-1]) + 1
    padding = int(config.line_padding)
    x0 = max(0, x0 - padding)
    x1 = min(width, x1 + padding)
    return {
        "x0": x0,
        "y0": max(0, y0 - padding),
        "x1": x1,
        "y1": min(binary.shape[0], y1 + padding),
        "score": 1.0,
        "source": "detected",
    }


def _truth_boxes(page: dict) -> list[dict]:
    boxes = []
    for line in page["lines"]:
        x0, y0, x1, y1 = [int(value) for value in line["truth_box"]]
        boxes.append(
            {
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
                "score": 1.0,
                "source": "truth_fallback",
            }
        )
    boxes.sort(key=lambda item: (-item["score"], item["y0"], item["x0"]))
    return boxes


def _box_valid(box: dict, height: int, width: int) -> bool:
    return (
        0 <= int(box["x0"]) < int(box["x1"]) <= width
        and 0 <= int(box["y0"]) < int(box["y1"]) <= height
    )


def detect_page(
    page: dict,
    preprocessed: dict,
    config: PipelineConfig,
    tracker: InjectionTracker,
) -> dict:
    page_id = page["page_id"]
    height, width = [int(value) for value in preprocessed["shape"]]
    binary = _binary_from_preprocessed(preprocessed)
    issues: list[Issue] = []
    degradations: list[Degradation] = []
    injection_records: list[dict] = []
    upstream_code = None
    for issue in preprocessed.get("issues", []):
        upstream_code = issue["code"]
        break

    if upstream_code is not None:
        boxes = _truth_boxes(page)
        degradations.append(
            Degradation(
                stage="detect",
                category="truth_box_layout",
                trigger_code=upstream_code,
                page_id=page_id,
                details={"reason": "upstream_preprocess_failure"},
            )
        )
    else:
        boxes = _detect_boxes(binary, config)
        if not boxes:
            issues.append(
                _make_issue(
                    "detect",
                    E_NO_LINES_DETECTED,
                    page_id,
                    field="boxes",
                )
            )
            boxes = _truth_boxes(page)
            degradations.append(
                Degradation(
                    stage="detect",
                    category="truth_box_layout",
                    trigger_code=E_NO_LINES_DETECTED,
                    page_id=page_id,
                )
            )

    spec = tracker.find("detect", page_id, None)
    if spec is None:
        for line_index in range(len(boxes)):
            spec = tracker.find("detect", page_id, line_index)
            if spec is not None:
                break
    if spec is not None:
        injection_records.append(
            tracker.apply(spec, details={"requested_box": spec.params.get("box")})
        )
        requested = spec.params.get("box")
        target_line = spec.line_no
        if (
            spec.code == E_INVALID_LINE_BOX
            and isinstance(requested, list)
            and len(requested) == 4
            and target_line is not None
            and 0 <= target_line < len(boxes)
        ):
            boxes[target_line] = {
                "x0": int(requested[0]),
                "y0": int(requested[1]),
                "x1": int(requested[2]),
                "y1": int(requested[3]),
                "score": boxes[target_line]["score"],
                "source": boxes[target_line]["source"],
            }
        issues.append(
            _make_issue(
                "detect",
                spec.code,
                page_id,
                line_no=spec.line_no,
                field="boxes",
                details={"box": requested},
                injected=True,
            )
        )
        invalid = [index for index, box in enumerate(boxes) if not _box_valid(box, height, width)]
        if invalid or spec.code != E_TEMPLATE_SHAPE_MISMATCH:
            boxes = _truth_boxes(page)
            degradations.append(
                Degradation(
                    stage="detect",
                    category="truth_box_layout",
                    trigger_code=spec.code,
                    page_id=page_id,
                    line_no=spec.line_no,
                    details={"invalid_lines": invalid},
                )
            )

    for line_index, box in enumerate(boxes):
        if not _box_valid(box, height, width):
            issues.append(
                _make_issue(
                    "detect",
                    E_INVALID_LINE_BOX,
                    page_id,
                    line_no=line_index,
                    field="boxes",
                    details={"box": box},
                )
            )
            boxes = _truth_boxes(page)
            degradations.append(
                Degradation(
                    stage="detect",
                    category="truth_box_layout",
                    trigger_code=E_INVALID_LINE_BOX,
                    page_id=page_id,
                    line_no=line_index,
                )
            )
            break

    for index, box in enumerate(boxes):
        box["line_index"] = index
    boxes.sort(key=lambda item: (-item["score"], item["y0"], item["x0"]))
    for index, box in enumerate(boxes):
        box["line_index"] = index

    return {
        "page_id": page_id,
        "boxes": boxes,
        "line_count": len(boxes),
        "status": _status(issues, degradations),
        "issues": [issue.to_dict() for issue in issues],
        "degradations": [item.to_dict() for item in degradations],
        "injection_records": injection_records,
    }


def _safe_gray(preprocessed: dict) -> np.ndarray:
    shape = [int(value) for value in preprocessed["shape"]]
    return _unpack_b64(preprocessed["gray_b64"], shape, "uint8")


def _truth_for_box(page: dict, box_index: int) -> dict | None:
    lines = page.get("lines", [])
    if box_index < len(lines):
        return lines[box_index]
    if not lines:
        return None
    center = (box_index, 0)
    del center
    return lines[min(box_index, len(lines) - 1)]


def _collect_recognition_injections(
    page_id: str,
    line_count: int,
    tracker: InjectionTracker,
) -> tuple[dict[int, list[InjectionSpec]], list[InjectionSpec]]:
    line_specs: dict[int, list[InjectionSpec]] = {}
    page_specs: list[InjectionSpec] = []
    page_wide = tracker.find("recognize", page_id, None)
    if page_wide is not None:
        page_specs.append(page_wide)
    for line_index in range(line_count):
        spec = tracker.find("recognize", page_id, line_index)
        if spec is not None:
            line_specs.setdefault(line_index, []).append(spec)
    return line_specs, page_specs


def _estimate_resources(
    contexts: list[LineContext],
    charset: str,
    library: TemplateLibrary,
    config: PipelineConfig,
) -> dict:
    char_count = sum(len(context.char_boxes) for context in contexts)
    comparisons = char_count * len(charset)
    high_bytes = char_count * library.high_shape[0] * library.high_shape[1] * 8
    low_bytes = char_count * library.low_shape[0] * library.low_shape[1] * 8
    return {
        "estimated_chars": char_count,
        "estimated_comparisons": comparisons,
        "estimated_feature_bytes": max(high_bytes, low_bytes),
        "max_candidate_comparisons": int(config.max_candidate_comparisons),
        "max_feature_memory_bytes": int(config.max_feature_memory_bytes),
    }


def _shift_lines(lines: list[dict], offset: int) -> list[dict]:
    if offset == 0:
        return lines
    shifted = []
    for line in lines:
        copy = dict(line)
        copy["line_index"] = int(copy["line_index"]) + offset
        shifted.append(copy)
    return shifted


def recognize_page(
    page: dict,
    preprocessed: dict,
    detected: dict,
    config: PipelineConfig,
    library: TemplateLibrary,
    tracker: InjectionTracker,
) -> dict:
    page_id = page["page_id"]
    gray = _safe_gray(preprocessed)
    boxes = [dict(box) for box in detected["boxes"]]
    issues: list[Issue] = []
    degradations: list[Degradation] = []
    injection_records: list[dict] = []

    try:
        charset_version = str(page["charset_version"])
        charset = library.charset_for(charset_version)
    except Exception:
        charset = library.charset_versions[sorted(library.charset_versions)[0]]

    contexts = [
        build_line_context(
            gray,
            [box["x0"], box["y0"], box["x1"], box["y1"]],
            config,
        )
        for box in boxes
    ]
    line_specs, page_specs = _collect_recognition_injections(page_id, len(boxes), tracker)
    low_lines: set[int] = set()
    truth_fallback_lines: dict[int, dict] = {}

    for spec in page_specs:
        injection_records.append(tracker.apply(spec, details=spec.params))
        issues.append(
            _make_issue(
                "recognize",
                spec.code,
                page_id,
                line_no=spec.line_no,
                field="charset_version" if spec.code == E_UNKNOWN_CHARSET_VERSION else "template",
                details=spec.params,
                injected=True,
            )
        )
        if spec.code == E_UNKNOWN_CHARSET_VERSION:
            low_lines.update(range(len(boxes)))
            degradations.append(
                Degradation(
                    stage="recognize",
                    category="low_resolution_templates",
                    trigger_code=spec.code,
                    page_id=page_id,
                )
            )

    for line_index, specs in sorted(line_specs.items()):
        for spec in specs:
            injection_records.append(tracker.apply(spec, details=spec.params))
            issues.append(
                _make_issue(
                    "recognize",
                    spec.code,
                    page_id,
                    line_no=line_index,
                    field="template_shape"
                    if spec.code == E_TEMPLATE_SHAPE_MISMATCH
                    else "charset_version",
                    details=spec.params,
                    injected=True,
                )
            )
            low_lines.add(line_index)
            degradations.append(
                Degradation(
                    stage="recognize",
                    category="low_resolution_templates",
                    trigger_code=spec.code,
                    page_id=page_id,
                    line_no=line_index,
                )
            )

    for line_index, context in enumerate(contexts):
        if not context.char_boxes:
            truth = _truth_for_box(page, line_index)
            truth_text = str(truth["text"]) if truth else ""
            issues.append(
                _make_issue(
                    "recognize",
                    E_CHAR_SEGMENTATION_FAILED,
                    page_id,
                    line_no=line_index,
                    field="chars",
                )
            )
            truth_fallback_lines[line_index] = {
                "text": truth_text,
                "category": "truth_text_after_segmentation",
                "trigger_code": E_CHAR_SEGMENTATION_FAILED,
            }
            degradations.append(
                Degradation(
                    stage="recognize",
                    category="truth_text_after_segmentation",
                    trigger_code=E_CHAR_SEGMENTATION_FAILED,
                    page_id=page_id,
                    line_no=line_index,
                )
            )

    resources = _estimate_resources(contexts, charset, library, config)
    over_limit = (
        resources["estimated_comparisons"] > int(config.max_candidate_comparisons)
        or resources["estimated_feature_bytes"] > int(config.max_feature_memory_bytes)
    )
    if over_limit:
        issue = _make_issue(
            "recognize",
            E_RESOURCE_LIMIT,
            page_id,
            field="resource_limits",
            details=resources,
        )
        issues.append(issue)
        if config.resource_policy == "error":
            for line_index in range(len(boxes)):
                truth = _truth_for_box(page, line_index)
                if truth is not None:
                    truth_fallback_lines[line_index] = {
                        "text": str(truth["text"]),
                        "category": "truth_text_resource_fallback",
                        "trigger_code": E_RESOURCE_LIMIT,
                    }
            degradations.append(
                Degradation(
                    stage="recognize",
                    category="truth_text_resource_fallback",
                    trigger_code=E_RESOURCE_LIMIT,
                    page_id=page_id,
                    details=resources,
                )
            )
        else:
            degradations.append(
                Degradation(
                    stage="recognize",
                    category="streaming_line_recognition",
                    trigger_code=E_RESOURCE_LIMIT,
                    page_id=page_id,
                    details=resources,
                )
            )

    chunk_size = int(config.recognition_chunk_lines)
    chunked_lines: list[dict] = []
    if len(boxes) == 0:
        chunked_lines = []
    else:
        for start in range(0, len(boxes), chunk_size):
            end = min(len(boxes), start + chunk_size)
            subset_page = dict(page)
            subset_page["lines"] = page.get("lines", [])[start:end]
            subset_contexts = contexts[start:end]
            subset_low = {
                index - start for index in low_lines if start <= index < end
            }
            subset_fallback = {
                index - start: value
                for index, value in truth_fallback_lines.items()
                if start <= index < end
            }
            lines = recognize_lines(
                subset_page,
                gray,
                subset_contexts,
                charset=charset,
                library=library,
                config=config,
                dtype=np.dtype(np.float32),
                low_lines=subset_low,
                truth_fallback_lines=subset_fallback,
            )
            chunked_lines.extend(_shift_lines(lines, start))

    reference_lines = recognize_lines(
        page,
        gray,
        contexts,
        charset=charset,
        library=library,
        config=config,
        dtype=np.dtype(np.float64),
        low_lines=low_lines,
        truth_fallback_lines=truth_fallback_lines,
    )
    comparison = compare_recognition(
        chunked_lines,
        reference_lines,
        config,
        raise_on_failure=False,
    )
    active_lines = chunked_lines
    if not comparison["ok"]:
        first = comparison["first_difference"] or {}
        issues.append(
            _make_issue(
                "recognize",
                E_TOLERANCE_EXCEEDED,
                page_id,
                line_no=first.get("line_no"),
                field="comparison",
                details={"first_difference": first},
            )
        )
        active_lines = reference_lines
        degradations.append(
            Degradation(
                stage="recognize",
                category="high_precision_reference",
                trigger_code=E_TOLERANCE_EXCEEDED,
                page_id=page_id,
                details={"first_difference": first},
            )
        )

    total_comparisons = sum(int(line.get("candidate_comparisons", 0)) for line in active_lines)
    return {
        "page_id": page_id,
        "charset": charset,
        "charset_version": page.get("charset_version"),
        "lines": active_lines,
        "reference_lines": reference_lines,
        "comparison": comparison,
        "candidate_comparisons": total_comparisons,
        "resources": resources,
        "status": _status(issues, degradations),
        "issues": [issue.to_dict() for issue in issues],
        "degradations": [item.to_dict() for item in degradations],
        "injection_records": injection_records,
    }


def aggregate_page(
    page: dict,
    recognized: dict,
    config: PipelineConfig,
    tracker: InjectionTracker,
) -> dict:
    page_id = page["page_id"]
    issues: list[Issue] = []
    degradations: list[Degradation] = []
    injection_records: list[dict] = []
    threshold = float(config.aggregate_confidence_threshold)

    spec = tracker.find("aggregate", page_id, None)
    if spec is not None:
        injection_records.append(tracker.apply(spec, details=spec.params))
        requested = spec.params.get("threshold", 0.0)
        issues.append(
            _make_issue(
                "aggregate",
                spec.code,
                page_id,
                field="threshold",
                details={"threshold": requested},
                injected=True,
            )
        )
        if spec.code == E_INVALID_THRESHOLD or float(requested) <= 0:
            threshold = float(config.aggregate_confidence_threshold)
            if threshold <= 0:
                threshold = 0.5
            degradations.append(
                Degradation(
                    stage="aggregate",
                    category="default_aggregation_threshold",
                    trigger_code=E_INVALID_THRESHOLD,
                    page_id=page_id,
                    details={"fallback_threshold": threshold},
                )
            )

    lines = recognized.get("lines", [])
    confidences = [float(line.get("confidence", 0.0)) for line in lines]
    mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    low_confidence_lines = [
        index for index, confidence in enumerate(confidences) if confidence < threshold
    ]
    char_count = sum(len(line.get("text", "")) for line in lines)
    text = "".join(line.get("text", "") for line in lines)
    expected_lines = int(page.get("line_count", len(page.get("lines", []))))
    comparison = recognized.get("comparison", {})
    metrics = {
        "page_id": page_id,
        "line_count": len(lines),
        "expected_line_count": expected_lines,
        "line_count_delta": len(lines) - expected_lines,
        "char_count": char_count,
        "mean_confidence": round(mean_confidence, 9),
        "threshold": round(threshold, 9),
        "low_confidence_line_count": len(low_confidence_lines),
        "low_confidence_lines": low_confidence_lines,
        "candidate_comparisons": int(recognized.get("candidate_comparisons", 0)),
        "max_coordinate_deviation": comparison.get("max_coordinate_deviation", 0.0),
        "comparison_ok": bool(comparison.get("ok", False)),
    }
    status = "degraded" if issues or degradations else recognized.get("status", "ok")
    if status == "ok" and low_confidence_lines:
        status = "degraded"
        degradations.append(
            Degradation(
                stage="aggregate",
                category="low_confidence_page",
                trigger_code="E_LOW_CONFIDENCE",
                page_id=page_id,
                details={"low_confidence_lines": low_confidence_lines},
            )
        )
    return {
        "page_id": page_id,
        "text": text,
        "metrics": metrics,
        "lines": lines,
        "reference_lines": recognized.get("reference_lines", []),
        "comparison": comparison,
        "charset": recognized.get("charset"),
        "charset_version": recognized.get("charset_version"),
        "status": status,
        "issues": [issue.to_dict() for issue in issues],
        "degradations": [item.to_dict() for item in degradations],
        "injection_records": injection_records,
    }


def content_checksum(pages: dict[str, dict]) -> str:
    return checksum({page_id: pages[page_id] for page_id in sorted(pages)})

