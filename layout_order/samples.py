"""Deterministic local datasets and anomaly fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .annotations import (
    AnnotationDocument,
    AnnotationRecord,
    default_role,
    render_annotations,
)
from .canonical import atomic_write_json, atomic_write_text, canonical_json
from .costs import DEFAULT_COST_CONFIG
from .errors import LayoutError
from .geometry import DEFAULT_GEOMETRY_CONFIG, DEFAULT_LIMITS
from .snapshot import SnapshotStore, build_snapshot

DEFAULT_SEED = 20261122
CANVAS_SHAPE = [1600, 1200]


def _block(
    block_id: int,
    block_type: str,
    rect: tuple[int, int, int, int],
    *,
    column_index: int,
    zone_id: str,
    nesting_path: str,
    text_len: int,
    reading_order_true: int,
) -> dict[str, Any]:
    x1, y1, x2, y2 = rect
    return {
        "block_id": block_id,
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
        "block_type": block_type,
        "text_len": text_len,
        "reading_order_true": reading_order_true,
        "column_index": column_index,
        "zone_id": zone_id,
        "nesting_path": nesting_path,
    }


def _template_specs() -> dict[str, list[dict[str, Any]]]:
    single = [
        ("header", (80, 45, 1120, 85), 0, "0:header", "root.header"),
        ("title", (80, 115, 1120, 185), 0, "0:top", "0.title"),
        ("section_title", (80, 220, 1120, 258), 0, "0:top", "0.section"),
        ("body", (80, 280, 1120, 348), 0, "0:top", "0.body"),
        ("body", (80, 366, 1120, 434), 0, "0:top", "0.body"),
        ("body", (80, 452, 1120, 520), 0, "0:middle", "0.body"),
        ("figure_caption", (80, 548, 1120, 604), 0, "0:middle", "0.caption"),
        ("section_title", (80, 642, 1120, 680), 0, "0:middle", "0.section"),
        ("body", (80, 700, 1120, 768), 0, "0:middle", "0.body"),
        ("body", (80, 786, 1120, 854), 0, "0:bottom", "0.body"),
        ("footer", (80, 1508, 1120, 1552), 0, "0:footer", "root.footer"),
    ]
    two_column = [
        ("header", (80, 45, 1120, 85), 0, "0:header", "root.header"),
        ("title", (80, 115, 1120, 185), 0, "0:top", "0.title"),
        ("section_title", (80, 220, 560, 258), 0, "0:top", "0.section"),
        ("body", (80, 278, 560, 344), 0, "0:top", "0.body"),
        ("body", (80, 362, 560, 428), 0, "0:top", "0.body"),
        ("body", (80, 446, 560, 512), 0, "0:middle", "0.body"),
        ("section_title", (640, 220, 1120, 258), 1, "1:top", "1.section"),
        ("body", (640, 278, 1120, 344), 1, "1:top", "1.body"),
        ("body", (640, 362, 1120, 428), 1, "1:top", "1.body"),
        ("body", (640, 446, 1120, 512), 1, "1:middle", "1.body"),
        ("figure_caption", (80, 1030, 1120, 1090), 0, "span:bottom", "span.caption"),
        ("footer", (80, 1508, 1120, 1552), 0, "0:footer", "root.footer"),
    ]
    mixed = [
        ("header", (80, 45, 1120, 85), 0, "0:header", "root.header"),
        ("title", (80, 115, 1120, 185), 0, "0:top", "0.title"),
        ("section_title", (80, 220, 560, 258), 0, "0:top", "0.upper.section"),
        ("body", (80, 278, 560, 344), 0, "0:top", "0.upper.body"),
        ("body", (80, 362, 560, 428), 0, "0:top", "0.upper.body"),
        ("section_title", (640, 220, 1120, 258), 1, "1:top", "1.upper.section"),
        ("body", (640, 278, 1120, 344), 1, "1:top", "1.upper.body"),
        ("body", (640, 362, 1120, 428), 1, "1:top", "1.upper.body"),
        ("figure_caption", (80, 690, 1120, 754), 0, "span:middle", "span.caption"),
        ("section_title", (80, 800, 560, 838), 0, "0:bottom", "0.lower.section"),
        ("body", (80, 858, 560, 924), 0, "0:bottom", "0.lower.body"),
        ("section_title", (640, 800, 1120, 838), 1, "1:bottom", "1.lower.section"),
        ("body", (640, 858, 1120, 924), 1, "1:bottom", "1.lower.body"),
        ("footer", (80, 1508, 1120, 1552), 0, "0:footer", "root.footer"),
    ]
    return {"single_column": single, "two_column": two_column, "mixed_columns": mixed}


def _jitter_rect(
    rect: tuple[int, int, int, int], rng: np.random.Generator
) -> tuple[int, int, int, int]:
    deltas = [int(value) for value in rng.integers(-2, 3, size=4)]
    x1 = rect[0] + deltas[0]
    y1 = rect[1] + deltas[1]
    x2 = rect[2] + deltas[2]
    y2 = rect[3] + deltas[3]
    return x1, y1, x2, y2


def generate_page(seed: int, page_index: int, template_name: str) -> dict[str, Any]:
    templates = _template_specs()
    if template_name not in templates:
        raise LayoutError(
            "E_CONFIG_INVALID",
            "unknown layout template",
            field="template",
            details={"template": template_name},
        )
    rng = np.random.default_rng(np.random.SeedSequence([seed, page_index, 7919]))
    blocks: list[dict[str, Any]] = []
    for reading_order, (
        block_type,
        rect,
        column_index,
        zone_id,
        nesting_path,
    ) in enumerate(templates[template_name]):
        jittered = _jitter_rect(rect, rng)
        if block_type in {"header", "footer", "title"}:
            text_len = int(rng.integers(40, 120))
        elif block_type in {"section_title", "figure_caption"}:
            text_len = int(rng.integers(25, 90))
        else:
            text_len = int(rng.integers(60, 260))
        blocks.append(
            _block(
                reading_order + 1,
                block_type,
                jittered,
                column_index=column_index,
                zone_id=zone_id,
                nesting_path=nesting_path,
                text_len=text_len,
                reading_order_true=reading_order,
            )
        )
    return {
        "page_id": f"page-{page_index + 1:04d}",
        "canvas_shape": list(CANVAS_SHAPE),
        "template": template_name,
        "blocks": blocks,
    }


def generate_pages(seed: int = DEFAULT_SEED, count: int = 5) -> list[dict[str, Any]]:
    template_cycle = ("single_column", "two_column", "mixed_columns")
    return [
        generate_page(seed, index, template_cycle[index % len(template_cycle)])
        for index in range(count)
    ]


def dataset_config(seed: int = DEFAULT_SEED) -> dict[str, Any]:
    edge_weights = {
        "weights": dict(DEFAULT_COST_CONFIG["weights"]),
        "type_violation_penalty": DEFAULT_COST_CONFIG["type_violation_penalty"],
        "precision": DEFAULT_COST_CONFIG["precision"],
    }
    return {
        "seed": seed,
        "rng": "numpy.random.Generator(PCG64) via SeedSequence([seed, page_index, 7919])",
        "canvas_shape": list(CANVAS_SHAPE),
        "templates": {
            name: {
                "block_count": len(specs),
                "reading_order": list(range(len(specs))),
            }
            for name, specs in _template_specs().items()
        },
        "annotation_formats": {
            "v1": {
                "header": "#LAYOUT_RO_V1",
                "fields": [
                    "block_id",
                    "reading_order",
                    "block_type",
                    "text_len",
                ],
            },
            "v2": {
                "header": "#LAYOUT_RO_V2",
                "fields": [
                    "block_id",
                    "reading_order",
                    "block_type",
                    "text_len",
                    "confidence_low",
                    "confidence_high",
                    "paragraph_role",
                    "order_weight",
                ],
            },
            "v3": {
                "header": "#LAYOUT_RO_V3",
                "fields": [
                    "block_id",
                    "reading_order",
                    "block_type",
                    "text_len",
                    "confidence_low",
                    "confidence_high",
                    "paragraph_role",
                    "order_weight",
                    "column_index",
                    "zone_id",
                    "nesting_path",
                ],
            },
        },
        "geometry_config": dict(DEFAULT_GEOMETRY_CONFIG),
        "cost_config": DEFAULT_COST_CONFIG,
        "edge_weights": edge_weights,
        "limits": dict(DEFAULT_LIMITS),
        "initial_versions": {
            "block_annotations": 1,
            "type_order": 1,
            "edge_weights": 1,
        },
    }


def pages_to_annotation_document(page: dict[str, Any]) -> AnnotationDocument:
    records: list[AnnotationRecord] = []
    for block in sorted(page["blocks"], key=lambda item: item["reading_order_true"]):
        records.append(
            AnnotationRecord(
                block_id=block["block_id"],
                reading_order=block["reading_order_true"],
                block_type=block["block_type"],
                text_len=block["text_len"],
                confidence_low=0.0,
                confidence_high=1.0,
                paragraph_role=default_role(block["block_type"]),
                order_weight=1.0,
                column_index=block.get("column_index", 0),
                zone_id=block.get("zone_id", "default"),
                nesting_path=block.get("nesting_path", "0"),
            )
        )
    return AnnotationDocument(version="v3", records=records, source=page["page_id"])


def read_samples(path: str | Path) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    seen_pages: set[str] = set()
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                page = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise LayoutError(
                    "E_FORMAT_INVALID",
                    "sample line is not valid JSON",
                    line=line_number,
                    details={"error": str(exc)},
                ) from exc
            if not isinstance(page, dict):
                raise LayoutError(
                    "E_FORMAT_INVALID",
                    "sample line must be an object",
                    line=line_number,
                )
            for field_name in ("page_id", "canvas_shape", "blocks"):
                if field_name not in page:
                    raise LayoutError(
                        "E_FORMAT_INVALID",
                        "required sample field is missing",
                        line=line_number,
                        field=field_name,
                    )
            page_id = str(page["page_id"])
            if page_id in seen_pages:
                raise LayoutError(
                    "E_FORMAT_INVALID",
                    "duplicate page_id in sample manifest",
                    line=line_number,
                    page_id=page_id,
                )
            seen_pages.add(page_id)
            blocks = page["blocks"]
            if not isinstance(blocks, list) or not blocks:
                raise LayoutError(
                    "E_FORMAT_INVALID",
                    "sample blocks must be a non-empty list",
                    line=line_number,
                    field="blocks",
                    page_id=page_id,
                )
            required_block_fields = (
                "block_id",
                "x1",
                "y1",
                "x2",
                "y2",
                "block_type",
                "text_len",
            )
            for block_index, block in enumerate(blocks):
                if not isinstance(block, dict):
                    raise LayoutError(
                        "E_FORMAT_INVALID",
                        "sample block must be an object",
                        line=line_number,
                        field=f"blocks[{block_index}]",
                        page_id=page_id,
                    )
                for field_name in required_block_fields:
                    if field_name not in block:
                        raise LayoutError(
                            "E_FORMAT_INVALID",
                            "required block field is missing",
                            line=line_number,
                            field=f"blocks[{block_index}].{field_name}",
                            page_id=page_id,
                        )
            pages.append(page)
    if not pages:
        raise LayoutError(
            "E_FORMAT_INVALID",
            "sample manifest contains no pages",
            line=1,
        )
    return pages


def write_samples(path: str | Path, pages: list[dict[str, Any]]) -> None:
    lines = [canonical_json(page) for page in pages]
    atomic_write_text(path, "\n".join(lines) + "\n")


def write_anomalies(directory: str | Path, config: dict[str, Any]) -> dict[str, str]:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "bad_field_count_v1": "#LAYOUT_RO_V1\n1|0|body\n",
        "missing_required_v1": "#LAYOUT_RO_V1\n1|0||30\n",
        "bad_block_id_v1": "#LAYOUT_RO_V1\nx|0|body|30\n",
        "unsupported_version_v9": "#LAYOUT_RO_V9\n1|0|body|30\n",
        "bad_order_gap_v1": "#LAYOUT_RO_V1\n1|0|body|30\n2|2|body|30\n",
    }
    for name, text in files.items():
        atomic_write_text(root / f"{name}.tsv", text)
    illegal_weights = {
        "weights": {
            "type_order_violation": -1,
            "column_transition": 4.0,
            "vertical_gap": 6.0,
            "horizontal_misalignment": 1.5,
            "cross_zone": 2.0,
        },
        "type_violation_penalty": 30.0,
        "precision": 8,
    }
    atomic_write_json(root / "illegal_weights.json", illegal_weights)
    atomic_write_json(root / "resource_limit.json", {"max_blocks_per_page": 2})
    out_of_range = {
        "page_id": "page-anomaly",
        "canvas_shape": list(CANVAS_SHAPE),
        "blocks": [
            {
                "block_id": 1,
                "x1": 10,
                "y1": 10,
                "x2": 100,
                "y2": 40,
                "block_type": "body",
                "text_len": 12,
                "reading_order_true": 999,
            },
            {
                "block_id": 2,
                "x1": 10,
                "y1": 50,
                "x2": 100,
                "y2": 80,
                "block_type": "body",
                "text_len": 12,
                "reading_order_true": 0,
            },
        ],
    }
    atomic_write_text(
        root / "out_of_range_blocks.jsonl",
        canonical_json(out_of_range) + "\n",
    )
    missing_block_field = dict(out_of_range)
    missing_block_field["page_id"] = "page-missing-field"
    missing_block_field["blocks"] = [dict(out_of_range["blocks"][0])]
    missing_block_field["blocks"][0].pop("block_type")
    atomic_write_text(
        root / "missing_block_field.jsonl",
        canonical_json(missing_block_field) + "\n",
    )
    atomic_write_json(
        root / "fixture_index.json",
        {
            "field_count_mismatch": "bad_field_count_v1.tsv",
            "required_field_missing": "missing_required_v1.tsv",
            "block_id_unparseable": "bad_block_id_v1.tsv",
            "unknown_version": "unsupported_version_v9.tsv",
            "order_gap": "bad_order_gap_v1.tsv",
            "illegal_weight": "illegal_weights.json",
            "resource_limit": "resource_limit.json",
            "out_of_range": "out_of_range_blocks.jsonl",
            "missing_block_field": "missing_block_field.jsonl",
            "seed": config["seed"],
        },
    )
    return {name: str(root / filename) for name, filename in {
        "field_count_mismatch": "bad_field_count_v1.tsv",
        "required_field_missing": "missing_required_v1.tsv",
        "block_id_unparseable": "bad_block_id_v1.tsv",
        "unknown_version": "unsupported_version_v9.tsv",
        "order_gap": "bad_order_gap_v1.tsv",
        "illegal_weight": "illegal_weights.json",
        "resource_limit": "resource_limit.json",
        "out_of_range": "out_of_range_blocks.jsonl",
        "missing_block_field": "missing_block_field.jsonl",
    }.items()}


def generate_dataset(
    output_dir: str | Path,
    *,
    seed: int = DEFAULT_SEED,
    page_count: int = 5,
    snapshot_dir: str | Path | None = None,
) -> dict[str, Any]:
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise LayoutError(
            "E_CONFIG_INVALID",
            "seed must be an integer",
            field="seed",
        )
    if not isinstance(page_count, int) or isinstance(page_count, bool) or page_count < 1:
        raise LayoutError(
            "E_CONFIG_INVALID",
            "page_count must be a positive integer",
            field="page_count",
        )
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    pages = generate_pages(seed, page_count)
    config = dataset_config(seed)
    write_samples(root / "samples.jsonl", pages)
    atomic_write_json(root / "dataset_config.json", config)
    annotation_dir = root / "annotations"
    annotation_dir.mkdir(parents=True, exist_ok=True)
    for page in pages:
        document = pages_to_annotation_document(page)
        for version in ("v1", "v2", "v3"):
            atomic_write_text(
                annotation_dir / f"{page['page_id']}.{version}.tsv",
                render_annotations(document, version),
            )
    anomaly_files = write_anomalies(root / "anomalies", config)
    block_annotations = {page["page_id"]: page for page in pages}
    snapshot = build_snapshot(
        block_annotations,
        dict(DEFAULT_COST_CONFIG["type_order"]),
        config["edge_weights"],
        config["initial_versions"],
    )
    initial_snapshot_path = root / "initial_snapshot.json"
    atomic_write_json(initial_snapshot_path, snapshot.to_dict())
    snapshot_store = None
    if snapshot_dir is not None:
        snapshot_store = SnapshotStore(snapshot_dir)
        snapshot_store.commit(snapshot)
    return {
        "root": str(root),
        "samples_path": str(root / "samples.jsonl"),
        "config_path": str(root / "dataset_config.json"),
        "annotations_dir": str(annotation_dir),
        "anomalies": anomaly_files,
        "pages": pages,
        "config": config,
        "snapshot": snapshot,
        "snapshot_store": snapshot_store,
    }

