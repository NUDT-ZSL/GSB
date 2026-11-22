"""Seeded page generation and JSONL manifest editing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .config import PipelineConfig
from .errors import (
    BusinessError,
    E_DUPLICATE_PAGE_ID,
    E_IMAGE_SHAPE_MISMATCH,
    E_INVALID_LINE_BOX,
    E_INVALID_MANIFEST,
    E_INVALID_POLICY,
    E_INVALID_TEXT,
    E_RESOURCE_LIMIT,
)
from .serialization import (
    atomic_write_bytes,
    atomic_write_json,
    checksum,
    read_json,
    read_jsonl,
    write_jsonl,
)
from .templates import LATIN_CHARSET, TemplateLibrary, get_template_library


GENERATOR_VERSION = "sample-v1"
DEFAULT_IMAGE_SHAPE = (288, 360)
CHARSET_VERSION = "latin-v1"
POLICIES = {"standard", "staggered", "compact"}


class LCG32:
    """Small fixed PRNG so generated samples do not depend on NumPy RNG policy."""

    def __init__(self, seed: int) -> None:
        self.state = (int(seed) ^ 0x9E3779B9) & 0xFFFFFFFF

    def next_u32(self) -> int:
        self.state = (1664525 * self.state + 1013904223) & 0xFFFFFFFF
        return self.state

    def randint(self, low: int, high: int) -> int:
        """Inclusive deterministic integer in [low, high]."""
        if high < low:
            raise ValueError("high must be >= low")
        return low + self.next_u32() % (high - low + 1)

    def choice(self, values: str) -> str:
        return values[self.next_u32() % len(values)]


def _text_profiles(page_index: int) -> str:
    return ("mixed", "digits", "letters", "mixed", "digits", "letters")[page_index % 6]


def _characters_for_profile(profile: str) -> str:
    if profile == "digits":
        return "0123456789"
    if profile == "letters":
        return "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return LATIN_CHARSET


def _page_text(seed: int, profile: str, index: int) -> str:
    rng = LCG32(seed + 17 * index)
    pool = _characters_for_profile(profile)
    length = rng.randint(6, 11)
    return "".join(rng.choice(pool) for _ in range(length))


def _line_layout(
    page_index: int,
    line_index: int,
    image_shape: tuple[int, int],
    text: str,
    policy: str,
) -> list[int]:
    height, width = image_shape
    pitch = 36 if policy != "compact" else 31
    top = 20 + (page_index % 2) * 2
    y0 = top + line_index * pitch
    y0 += 2 if policy == "staggered" and line_index % 2 else 0
    x0 = 24 + (page_index % 3) * 3
    if policy == "staggered":
        x0 += (line_index % 3) * 6
    if policy == "compact":
        x0 += 2
    x1 = x0 + len(text) * 13 - 3
    y1 = y0 + 14
    return [x0, y0, min(x1, width), min(y1, height)]


def make_page(page_index: int, base_seed: int) -> dict[str, Any]:
    seed = int(base_seed) + page_index * 104729
    profile = _text_profiles(page_index)
    line_count = 4 + ((seed >> 4) % 4)
    policy = ("standard", "staggered", "compact")[page_index % 3]
    image_shape = list(DEFAULT_IMAGE_SHAPE)
    lines: list[dict[str, Any]] = []
    for line_index in range(line_count):
        text = _page_text(seed, profile, line_index)
        box = _line_layout(page_index, line_index, DEFAULT_IMAGE_SHAPE, text, policy)
        lines.append(
            {
                "line_no": line_index,
                "text": text,
                "truth_box": box,
            }
        )
    return {
        "page_id": f"page-{page_index + 1:04d}",
        "image_seed": seed,
        "image_shape": image_shape,
        "line_count": line_count,
        "charset_version": CHARSET_VERSION,
        "policy": policy,
        "text_profile": profile,
        "lines": lines,
    }


def render_page(record: dict[str, Any], library: TemplateLibrary | None = None) -> np.ndarray:
    """Render a deterministic grayscale page from its seed and edited truth boxes."""
    library = library or get_template_library("glyph-v1")
    height, width = int(record["image_shape"][0]), int(record["image_shape"][1])
    seed = int(record["image_seed"])
    y_grid = np.arange(height, dtype=np.uint64)[:, None]
    x_grid = np.arange(width, dtype=np.uint64)[None, :]
    background = (
        (x_grid * np.uint64(1103515245) + y_grid * np.uint64(12345) + np.uint64(seed))
        >> np.uint64(16)
    ) % np.uint64(30)
    noise = (background + np.uint64(225)).astype(np.uint8)

    for line in record.get("lines", []):
        text = str(line["text"])
        x0, y0, x1, y1 = [int(value) for value in line["truth_box"]]
        x_cursor = x0 + 1
        y_cursor = y0 + 1
        for char_index, character in enumerate(text):
            pattern = library.pattern(character)
            for row_index, row in enumerate(pattern):
                for column_index, cell in enumerate(row):
                    if cell != "1":
                        continue
                    for dy in range(2):
                        for dx in range(2):
                            py = y_cursor + row_index * 2 + dy
                            px = x_cursor + column_index * 2 + dx
                            if 0 <= py < height and 0 <= px < width:
                                ink = 22 + (
                                    (px * 17 + py * 31 + seed + char_index * 13) % 39
                                )
                                noise[py, px] = ink
            x_cursor += 13
            if x_cursor >= x1 + 2:
                break
    return noise


def page_png_bytes(record: dict[str, Any], library: TemplateLibrary | None = None) -> bytes:
    import io

    image = Image.fromarray(render_page(record, library), mode="L")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False, compress_level=6)
    return buffer.getvalue()


def validate_manifest(
    records: list[dict[str, Any]],
    config: PipelineConfig,
    library: TemplateLibrary,
) -> None:
    seen: set[str] = set()
    expected_shape = list(DEFAULT_IMAGE_SHAPE)
    for index, record in enumerate(records):
        page_id = record.get("page_id")
        if not isinstance(page_id, str) or not page_id:
            raise BusinessError(
                E_INVALID_MANIFEST,
                "page_id must be a non-empty string",
                stage="validate",
                field="page_id",
                details={"record_index": index},
            )
        if page_id in seen:
            raise BusinessError(
                E_DUPLICATE_PAGE_ID,
                f"duplicate page_id: {page_id}",
                stage="validate",
                page_id=page_id,
                field="page_id",
            )
        seen.add(page_id)

        shape = record.get("image_shape")
        if (
            not isinstance(shape, list)
            or len(shape) != 2
            or not all(isinstance(item, int) for item in shape)
            or shape != expected_shape
        ):
            raise BusinessError(
                E_IMAGE_SHAPE_MISMATCH,
                f"image_shape must be {expected_shape}",
                stage="validate",
                page_id=page_id,
                field="image_shape",
                details={"actual": shape, "expected": expected_shape},
            )

        charset_version = record.get("charset_version")
        if charset_version not in library.charset_versions:
            raise BusinessError(
                "E_UNKNOWN_CHARSET_VERSION",
                f"unknown charset_version: {charset_version}",
                stage="validate",
                page_id=page_id,
                field="charset_version",
                details={"charset_version": charset_version},
            )

        policy = record.get("policy")
        if policy not in POLICIES:
            raise BusinessError(
                E_INVALID_POLICY,
                f"unknown policy: {policy}",
                stage="validate",
                page_id=page_id,
                field="policy",
            )

        line_count = record.get("line_count")
        lines = record.get("lines")
        if not isinstance(line_count, int) or not isinstance(lines, list) or line_count != len(lines):
            raise BusinessError(
                E_INVALID_MANIFEST,
                "line_count must equal the number of lines",
                stage="validate",
                page_id=page_id,
                field="line_count",
            )
        if line_count > config.max_lines_per_page:
            raise BusinessError(
                E_RESOURCE_LIMIT,
                "page line count exceeds configured limit",
                stage="validate",
                page_id=page_id,
                field="line_count",
                details={
                    "line_count": line_count,
                    "max_lines_per_page": config.max_lines_per_page,
                },
            )
        if line_count <= 0:
            raise BusinessError(
                E_INVALID_MANIFEST,
                "page must contain at least one line",
                stage="validate",
                page_id=page_id,
                field="line_count",
            )

        charset = library.charset_versions[charset_version]
        for line_no, line in enumerate(lines):
            text = line.get("text")
            if not isinstance(text, str) or not text:
                raise BusinessError(
                    E_INVALID_TEXT,
                    "line text must be a non-empty string",
                    stage="validate",
                    page_id=page_id,
                    line_no=line_no,
                    field=f"lines[{line_no}].text",
                )
            unsupported = sorted(set(text) - set(charset))
            if unsupported:
                raise BusinessError(
                    E_INVALID_TEXT,
                    "line text contains characters outside the charset",
                    stage="validate",
                    page_id=page_id,
                    line_no=line_no,
                    field=f"lines[{line_no}].text",
                    details={"unsupported": unsupported},
                )
            box = line.get("truth_box")
            if not _valid_box(box, height=shape[0], width=shape[1]):
                raise BusinessError(
                    E_INVALID_LINE_BOX,
                    "truth_box is outside the image or is not a valid rectangle",
                    stage="validate",
                    page_id=page_id,
                    line_no=line_no,
                    field=f"lines[{line_no}].truth_box",
                    details={"truth_box": box},
                )


def _valid_box(box: Any, *, height: int, width: int) -> bool:
    if not isinstance(box, list) or len(box) != 4:
        return False
    if not all(isinstance(value, int) for value in box):
        return False
    x0, y0, x1, y1 = box
    return 0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height


def generate_workspace(
    root: str | Path,
    *,
    page_count: int = 6,
    base_seed: int = 20261123,
    library: TemplateLibrary | None = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    library = library or get_template_library("glyph-v1")
    data_dir = Path(root)
    manifest_path = data_dir / "pages.jsonl"
    if manifest_path.exists() and not force:
        return load_manifest(data_dir)
    if page_count <= 0:
        raise ValueError("page_count must be positive")
    records = [make_page(index, base_seed) for index in range(page_count)]
    config = PipelineConfig()
    validate_manifest(records, config, library)
    data_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(manifest_path, records)

    image_dir = data_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    for record in records:
        atomic_write_bytes(image_dir / f"{record['page_id']}.png", page_png_bytes(record, library))

    workspace = {
        "generator_version": GENERATOR_VERSION,
        "base_seed": int(base_seed),
        "page_count": page_count,
        "charset_version": CHARSET_VERSION,
        "image_shape": list(DEFAULT_IMAGE_SHAPE),
        "manifest_checksum": checksum(records),
        "template_version": library.version,
        "template_checksum": library.checksum,
    }
    atomic_write_json(data_dir / "workspace.json", workspace)
    return records


def load_manifest(root: str | Path) -> list[dict[str, Any]]:
    path = Path(root) / "pages.jsonl"
    records = read_jsonl(path)
    return sorted(records, key=lambda item: str(item["page_id"]))


def save_manifest(root: str | Path, records: list[dict[str, Any]]) -> None:
    write_jsonl(Path(root) / "pages.jsonl", records)


def find_page(records: list[dict[str, Any]], page_id: str) -> dict[str, Any]:
    for record in records:
        if record["page_id"] == page_id:
            return record
    raise BusinessError(
        "E_NOT_FOUND",
        f"page not found: {page_id}",
        stage="edit",
        page_id=page_id,
    )


def apply_page_edit(
    root: str | Path,
    page_id: str,
    lines: list[dict[str, Any]],
    *,
    config: PipelineConfig,
    library: TemplateLibrary,
) -> dict[str, Any]:
    records = load_manifest(root)
    page = find_page(records, page_id)
    normalized_lines: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        try:
            text = str(line["text"])
            box = [int(value) for value in line["truth_box"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise BusinessError(
                E_INVALID_MANIFEST,
                "edited line must contain text and a four-integer truth_box",
                stage="edit",
                page_id=page_id,
                line_no=index,
                field=f"lines[{index}]",
            ) from exc
        normalized_lines.append(
            {"line_no": index, "text": text, "truth_box": box}
        )
    page["lines"] = normalized_lines
    page["line_count"] = len(normalized_lines)
    validate_manifest(records, config, library)
    save_manifest(root, records)
    image_dir = Path(root) / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(image_dir / f"{page_id}.png", page_png_bytes(page, library))
    workspace_path = Path(root) / "workspace.json"
    if workspace_path.exists():
        workspace = read_json(workspace_path)
    else:
        workspace = {}
    workspace.update(
        {
            "generator_version": GENERATOR_VERSION,
            "manifest_checksum": checksum(records),
            "template_version": library.version,
            "template_checksum": library.checksum,
        }
    )
    atomic_write_json(workspace_path, workspace)
    return page

