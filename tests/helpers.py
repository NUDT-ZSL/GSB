from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLES_DIR = PROJECT_ROOT / "samples"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def copy_sample_workspace(root: Path) -> dict[str, Path]:
    images = root / "images"
    shutil.copytree(SAMPLES_DIR / "images", images)
    config = root / "config.json"
    metadata = root / "metadata.json"
    truth = root / "layout_truth.json"
    shutil.copy2(SAMPLES_DIR / "config.json", config)
    shutil.copy2(SAMPLES_DIR / "metadata.json", metadata)
    shutil.copy2(SAMPLES_DIR / "layout_truth.json", truth)
    return {
        "root": root,
        "images": images,
        "config": config,
        "metadata": metadata,
        "truth": truth,
    }


def canonical_pages(report: dict[str, Any]) -> dict[str, Any]:
    pages = {}
    for page in report.get("pages", []):
        normalized = copy.deepcopy(page)
        pages[normalized["page"]] = normalized
    return {
        "pages": pages,
        "failures": copy.deepcopy(report.get("failures", [])),
    }


def canonical_page_core(report: dict[str, Any]) -> dict[str, Any]:
    pages = {}
    for page in report.get("pages", []):
        pages[page["page"]] = {
            "image_sha256": page["image_sha256"],
            "pixel_mode": page["pixel_mode"],
            "dimensions": copy.deepcopy(page["dimensions"]),
            "status": page["status"],
            "block_count": page["block_count"],
            "reading_order_count": page["reading_order_count"],
            "role_counts": copy.deepcopy(page["role_counts"]),
            "blocks": copy.deepcopy(page["blocks"]),
            "column_groups": copy.deepcopy(page["column_groups"]),
            "reading_order": copy.deepcopy(page["reading_order"]),
            "diagnostics": copy.deepcopy(page["diagnostics"]),
        }
    return {
        "pages": pages,
        "failures": copy.deepcopy(report.get("failures", [])),
        "business_codes": copy.deepcopy(report.get("business_codes", [])),
    }
