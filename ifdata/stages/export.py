"""Stage 6: declared-order, traceable atomic dataset export."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..utils import (
    atomic_write_json,
    atomic_write_jsonl,
    sha256_bytes,
    sha256_json,
)


def _export_record(entry: dict[str, Any]) -> dict[str, Any]:
    sample = entry["sample"]
    trace = {
        "order": entry["order"],
        "raw_line_no": sample["__line_no"],
        "raw_sample_checksum": sample.get("__raw_checksum")
        or sha256_json(
            {
                key: sample[key]
                for key in (
                    "sample_id",
                    "instruction",
                    "input",
                    "output",
                    "source_tag",
                    "quality_flag",
                    "lang",
                    "annotation_revision_raw",
                )
            }
        ),
        "template_id": sample["template_id"],
        "normalized_text_checksum": sha256_json(sample["canonical_text"]),
        "quality_score": sample["quality_score"],
        "quality_tier": sample["quality_tier"],
        "quality_contributions": sample["quality_contributions"],
        "cluster_id": sample["cluster_id"],
        "dedup_retained": True,
        "sampling_target_id": entry["target_id"],
        "sampling_target_index": entry["target_index"],
        "sampling_seed": entry["sampling_seed"],
        "sampling_rank_position": entry["rank_position"],
        "target_quality_tier": entry["target_quality_tier"],
        "actual_quality_tier": entry["actual_quality_tier"],
        "quality_relaxed": entry["relaxed"],
        "relaxed_from_quality_tier": entry[
            "relaxed_from_quality_tier"
        ],
    }
    row = {
        "sample_id": sample["sample_id"],
        "instruction": sample["normalized_instruction"],
        "input": sample["normalized_input"],
        "output": sample["output"],
        "source_tag": sample["source_tag"],
        "quality_flag": sample["quality_flag"],
        "lang": sample["lang"],
        "annotation_revision": sample["annotation_revision"],
        "trace": trace,
    }
    row["trace"]["export_row_checksum"] = sha256_json(row)
    return row


def run_export(
    sampling_result: dict[str, Any],
    out_dir: str | Path,
    *,
    strategy_checksum: str | None = None,
) -> dict[str, Any]:
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    rows = [
        _export_record(entry)
        for entry in sorted(
            sampling_result["selected"],
            key=lambda entry: entry["order"],
        )
    ]
    dataset_path = root / "dataset.jsonl"
    atomic_write_jsonl(dataset_path, rows)
    payload = dataset_path.read_bytes() if dataset_path.exists() else b""
    manifest = {
        "schema_version": 1,
        "stage": "export",
        "row_count": len(rows),
        "dataset_path": str(dataset_path),
        "dataset_checksum": sha256_bytes(payload),
        "strategy_checksum": strategy_checksum,
        "sampling_seed": sampling_result["summary"]["seed"],
        "ordering": sampling_result["summary"]["ordering"],
        "sample_ids": [row["sample_id"] for row in rows],
        "targets": [
            {
                "target_id": target["target_id"],
                "quota": target["quota"],
                "achieved": target["achieved"],
                "shortfall": target["shortfall"],
                "shortfall_reasons": target["shortfall_reasons"],
                "seed": target["seed"],
            }
            for target in sampling_result["targets"]
        ],
    }
    manifest_path = root / "export_manifest.json"
    atomic_write_json(manifest_path, manifest)
    return {
        "stage": "export",
        "manifest": manifest,
        "dataset_checksum": manifest["dataset_checksum"],
        "completed_sample_ids": [row["sample_id"] for row in rows],
        "valid_sample_ids": [row["sample_id"] for row in rows],
        "expected_sample_ids": [row["sample_id"] for row in rows],
    }

