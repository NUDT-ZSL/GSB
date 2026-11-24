from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import Issue, SchemaError


KNOWN_FAULT_TYPES = {
    "corrupt_pixels",
    "grid_not_divisible",
    "ledger_digest_mismatch",
    "vocabulary_missing",
}


@dataclass(frozen=True)
class Grid:
    rows: int
    cols: int


@dataclass(frozen=True)
class ImageSpec:
    image_id: str
    path: Path
    truth_path: Path
    sha256: str | None = None
    width: int | None = None
    height: int | None = None
    vocabulary: tuple[str, ...] | None = None


@dataclass(frozen=True)
class TruthBlock:
    block_id: str
    bbox: tuple[int, int, int, int]
    text: str
    confidence_lower_bound: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "bbox": list(self.bbox),
            "text": self.text,
            "confidence_lower_bound": self.confidence_lower_bound,
        }


@dataclass
class TaskSpec:
    path: Path
    raw: dict[str, Any]
    task_id: str
    seed: int
    image_width: int
    image_height: int
    detection_threshold: float
    confidence_floor: float
    grid: Grid
    concurrency: int
    memory_budget_bytes: int
    round_logic_budget: float
    state_dir: Path
    output_dir: Path
    vocabulary: list[str]
    images: list[ImageSpec]
    injections: list[dict[str, Any]] = field(default_factory=list)
    normalization: dict[str, Any] = field(default_factory=dict)

    def injections_for(self, image_id: str, fault_type: str) -> list[dict[str, Any]]:
        return [
            item
            for item in self.injections
            if item.get("image_id") == image_id and item.get("type") == fault_type
        ]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_key(raw: dict[str, Any], key: str, issues: list[Issue]) -> Any:
    if key not in raw:
        issues.append(Issue(f"/{key}", "missing required field"))
        return None
    return raw[key]


def _validate_unit_interval(value: Any, field_path: str, issues: list[Issue]) -> None:
    if not _is_number(value) or not (0.0 <= float(value) <= 1.0):
        issues.append(Issue(field_path, "must be a number in [0, 1]"))


def _resolve(path_value: str, base_dir: Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else (base_dir / path).resolve()


def load_task(path: str | Path) -> TaskSpec:
    path = Path(path).resolve()
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SchemaError(f"cannot read task JSON: {exc}", field=str(path)) from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SchemaError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}",
            field=f"{path}:{exc.lineno}:{exc.colno}",
        ) from exc
    if not isinstance(raw, dict):
        raise SchemaError("task document must be a JSON object", field="/")

    issues: list[Issue] = []
    task_id = _require_key(raw, "task_id", issues)
    if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
        issues.append(Issue("/task_id", "must be a non-empty string"))

    seed = raw.get("seed", 0)
    if not _is_int(seed):
        issues.append(Issue("/seed", "must be an integer"))

    width = _require_key(raw, "image_width", issues)
    height = _require_key(raw, "image_height", issues)
    if width is not None and (not _is_int(width) or width <= 0):
        issues.append(Issue("/image_width", "must be a positive integer"))
    if height is not None and (not _is_int(height) or height <= 0):
        issues.append(Issue("/image_height", "must be a positive integer"))

    detection_threshold = _require_key(raw, "detection_threshold", issues)
    if detection_threshold is not None:
        _validate_unit_interval(detection_threshold, "/detection_threshold", issues)
    confidence_floor = _require_key(raw, "confidence_floor", issues)
    if confidence_floor is not None:
        _validate_unit_interval(confidence_floor, "/confidence_floor", issues)

    grid_raw = _require_key(raw, "chunk_grid", issues)
    grid: Grid | None = None
    if grid_raw is not None:
        if not isinstance(grid_raw, dict):
            issues.append(Issue("/chunk_grid", "must be an object with rows and cols"))
        else:
            rows = grid_raw.get("rows")
            cols = grid_raw.get("cols")
            if not _is_int(rows) or rows <= 0:
                issues.append(Issue("/chunk_grid/rows", "must be a positive integer"))
            if not _is_int(cols) or cols <= 0:
                issues.append(Issue("/chunk_grid/cols", "must be a positive integer"))
            if _is_int(rows) and _is_int(cols) and rows > 0 and cols > 0:
                if _is_int(height) and height > 0 and height % rows != 0:
                    issues.append(
                        Issue(
                            "/chunk_grid/rows",
                            f"rows={rows} must divide image_height={height}",
                            code="E_GRID_NOT_DIVISIBLE",
                        )
                    )
                if _is_int(width) and width > 0 and width % cols != 0:
                    issues.append(
                        Issue(
                            "/chunk_grid/cols",
                            f"cols={cols} must divide image_width={width}",
                            code="E_GRID_NOT_DIVISIBLE",
                        )
                    )
                grid = Grid(rows=rows, cols=cols)

    concurrency = _require_key(raw, "concurrency", issues)
    if concurrency is not None and (not _is_int(concurrency) or concurrency <= 0):
        issues.append(Issue("/concurrency", "must be a positive integer"))

    memory_budget = _require_key(raw, "memory_budget_bytes", issues)
    if memory_budget is not None and (not _is_int(memory_budget) or memory_budget <= 0):
        issues.append(Issue("/memory_budget_bytes", "must be a positive integer"))

    round_budget = _require_key(raw, "round_logic_budget", issues)
    if round_budget is not None and (not _is_number(round_budget) or round_budget <= 0):
        issues.append(Issue("/round_logic_budget", "must be a positive number"))

    vocabulary = _require_key(raw, "vocabulary", issues)
    if vocabulary is not None:
        if not isinstance(vocabulary, list) or not all(isinstance(x, str) and x.strip() for x in vocabulary):
            issues.append(Issue("/vocabulary", "must be a list of non-empty strings"))
            vocabulary = []

    state_dir = _require_key(raw, "state_dir", issues)
    output_dir = _require_key(raw, "output_dir", issues)
    if state_dir is not None and not isinstance(state_dir, str):
        issues.append(Issue("/state_dir", "must be a string path"))
    if output_dir is not None and not isinstance(output_dir, str):
        issues.append(Issue("/output_dir", "must be a string path"))

    images_raw = _require_key(raw, "images", issues)
    images: list[ImageSpec] = []
    base_dir = path.parent
    if images_raw is not None:
        if not isinstance(images_raw, list) or not images_raw:
            issues.append(Issue("/images", "must be a non-empty list"))
        else:
            seen_ids: set[str] = set()
            for index, item in enumerate(images_raw):
                prefix = f"/images/{index}"
                if not isinstance(item, dict):
                    issues.append(Issue(prefix, "image descriptor must be an object"))
                    continue
                image_id = item.get("image_id")
                if not isinstance(image_id, str) or not image_id.strip():
                    issues.append(Issue(f"{prefix}/image_id", "must be a non-empty string"))
                elif image_id in seen_ids:
                    issues.append(Issue(f"{prefix}/image_id", f"duplicate image_id {image_id!r}"))
                else:
                    seen_ids.add(image_id)
                image_path = item.get("path")
                truth_path = item.get("truth_path")
                if not isinstance(image_path, str) or not image_path.strip():
                    issues.append(Issue(f"{prefix}/path", "must be a relative or absolute file path"))
                if not isinstance(truth_path, str) or not truth_path.strip():
                    issues.append(Issue(f"{prefix}/truth_path", "must be a relative or absolute file path"))
                sha = item.get("sha256")
                if sha is not None and (not isinstance(sha, str) or len(sha) != 64):
                    issues.append(Issue(f"{prefix}/sha256", "must be a 64-character SHA-256 string"))
                width_override = item.get("width")
                height_override = item.get("height")
                if width_override is not None and (not _is_int(width_override) or width_override <= 0):
                    issues.append(Issue(f"{prefix}/width", "must be a positive integer"))
                if height_override is not None and (not _is_int(height_override) or height_override <= 0):
                    issues.append(Issue(f"{prefix}/height", "must be a positive integer"))
                vocab_override = item.get("vocabulary")
                if vocab_override is not None and (
                    not isinstance(vocab_override, list)
                    or not all(isinstance(x, str) and x.strip() for x in vocab_override)
                ):
                    issues.append(Issue(f"{prefix}/vocabulary", "must be a list of strings"))
                    vocab_override = None
                if isinstance(image_id, str) and isinstance(image_path, str) and isinstance(truth_path, str):
                    images.append(
                        ImageSpec(
                            image_id=image_id,
                            path=_resolve(image_path, base_dir),
                            truth_path=_resolve(truth_path, base_dir),
                            sha256=sha if isinstance(sha, str) else None,
                            width=width_override if _is_int(width_override) else None,
                            height=height_override if _is_int(height_override) else None,
                            vocabulary=tuple(vocab_override) if isinstance(vocab_override, list) else None,
                        )
                    )

    injections = raw.get("exception_injections", [])
    if injections is not None and not isinstance(injections, list):
        issues.append(Issue("/exception_injections", "must be a list"))
        injections = []
    elif isinstance(injections, list):
        for index, injection in enumerate(injections):
            prefix = f"/exception_injections/{index}"
            if not isinstance(injection, dict):
                issues.append(Issue(prefix, "must be an object"))
                continue
            if injection.get("type") not in KNOWN_FAULT_TYPES:
                issues.append(
                    Issue(f"{prefix}/type", f"must be one of {sorted(KNOWN_FAULT_TYPES)}")
                )
            if not isinstance(injection.get("image_id"), str):
                issues.append(Issue(f"{prefix}/image_id", "must be a string"))

    normalization = raw.get("normalization", {})
    if normalization is None:
        normalization = {}
    if not isinstance(normalization, dict):
        issues.append(Issue("/normalization", "must be an object"))
        normalization = {}
    else:
        for key in ("target_cap_height", "template_cell", "max_width"):
            if key in normalization and (not _is_int(normalization[key]) or normalization[key] <= 0):
                issues.append(Issue(f"/normalization/{key}", "must be a positive integer"))

    if issues:
        raise SchemaError(
            f"task schema validation failed with {len(issues)} issue(s)",
            field="/",
            issues=issues,
        )

    assert grid is not None
    assert isinstance(vocabulary, list)
    return TaskSpec(
        path=path,
        raw=raw,
        task_id=str(task_id),
        seed=int(seed),
        image_width=int(width),
        image_height=int(height),
        detection_threshold=float(detection_threshold),
        confidence_floor=float(confidence_floor),
        grid=grid,
        concurrency=int(concurrency),
        memory_budget_bytes=int(memory_budget),
        round_logic_budget=float(round_budget),
        state_dir=Path(str(state_dir)),
        output_dir=Path(str(output_dir)),
        vocabulary=[str(x) for x in vocabulary],
        images=images,
        injections=list(injections),
        normalization=dict(normalization),
    )


def load_truth(path: str | Path) -> list[TruthBlock]:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaError(f"cannot read truth list: {exc}", field=str(path)) from exc
    if isinstance(raw, dict):
        raw_blocks = raw.get("blocks")
    else:
        raw_blocks = raw
    if not isinstance(raw_blocks, list):
        raise SchemaError("truth document must be a list or contain blocks list", field=str(path))

    blocks: list[TruthBlock] = []
    for index, item in enumerate(raw_blocks):
        prefix = f"{path}:/blocks/{index}"
        if not isinstance(item, dict):
            raise SchemaError("truth block must be an object", field=prefix)
        block_id = item.get("block_id", item.get("id"))
        bbox = item.get("bbox")
        text = item.get("text")
        lower = item.get("confidence_lower_bound", item.get("min_confidence"))
        if not isinstance(block_id, str) or not block_id:
            raise SchemaError("block_id must be a non-empty string", field=f"{prefix}/block_id")
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or not all(_is_int(x) for x in bbox)
            or bbox[2] <= 0
            or bbox[3] <= 0
            or bbox[0] < 0
            or bbox[1] < 0
        ):
            raise SchemaError(
                "bbox must be [x, y, positive_width, positive_height]",
                field=f"{prefix}/bbox",
            )
        if not isinstance(text, str):
            raise SchemaError("text must be a string", field=f"{prefix}/text")
        _validate_unit_interval(lower if lower is not None else 0.0, f"{prefix}/confidence_lower_bound", [])
        if not _is_number(lower) or not (0 <= float(lower) <= 1):
            raise SchemaError(
                "confidence_lower_bound must be in [0,1]",
                field=f"{prefix}/confidence_lower_bound",
            )
        blocks.append(
            TruthBlock(
                block_id=block_id,
                bbox=(int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])),
                text=text,
                confidence_lower_bound=float(lower),
            )
        )
    return sorted(blocks, key=lambda b: (b.bbox[0], b.bbox[1], b.bbox[2], b.bbox[3], b.block_id))
