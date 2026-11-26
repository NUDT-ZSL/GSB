from __future__ import annotations

from pathlib import Path
from typing import Any

from .formats_dispatch import read_ledger, write_ledger
from .format_v2 import MANIFEST_NAME
from .hashing import read_json
from .model import CSRMatrix
from .semantic import assert_semantic_equal


def _completion_records(target_version: int, target_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if target_version == 2:
        manifest = read_json(target_path / MANIFEST_NAME)
        records.append(
            {
                "field": "shard_boundaries",
                "value": [[shard["row_start"], shard["row_end"]] for shard in manifest["shards"]],
                "source": "derived:config.shard_rows",
            }
        )
        records.append(
            {
                "field": "bitmap_digest",
                "value": [shard["bitmap_digest"] for shard in manifest["shards"]],
                "source": "computed:sqlite.row_bitmap+sha256",
            }
        )
    if target_version == 3:
        document = read_json(target_path)
        records.append(
            {
                "field": "column_block_mapping",
                "value": [
                    {
                        "block_id": block["block_id"],
                        "col_start": block["col_start"],
                        "col_end": block["col_end"],
                        "mapping": block["column_mapping"]["global_formula"],
                    }
                    for block in document["blocks"]
                ],
                "source": "derived:config.block_cols",
            }
        )
        records.append(
            {
                "field": "structural_algebra_defaults",
                "value": document["structural_algebra"],
                "source": "default:float64_ordered_algebra_v1",
            }
        )
        records.append(
            {
                "field": "block_bitmap_digest",
                "value": document["block_bitmap_digest"],
                "source": "computed:nonempty_column_blocks+sha256",
            }
        )
    if target_version == 1:
        records.append(
            {
                "field": "row_major_flat_table",
                "value": "expanded all CSR entries into four-field records",
                "source": "derived:canonical_csr_row_order",
            }
        )
    return records


def _discard_records(source_version: int, target_version: int) -> list[dict[str, Any]]:
    if target_version != 1:
        return []
    discarded: list[dict[str, Any]] = []
    if source_version == 3:
        discarded.extend(
            [
                {
                    "field": "blocks[*].codec/payload/column_mapping",
                    "rule": "v3_to_v1:expand_column_blocks_to_flat_rows_and_drop_compression",
                    "description": "Expand column-block compressed payloads into v1 flat rows and discard compression descriptors.",
                },
                {
                    "field": "block_bitmap_hex/block_bitmap_digest/bitmap_validation",
                    "rule": "v3_to_v1:drop_bitmap_verification_metadata",
                    "description": "Discard column-block bitmap and validation metadata that v1 cannot represent.",
                },
            ]
        )
    if source_version == 2:
        discarded.extend(
            [
                {
                    "field": "shards[*] and SQLite bitmap_index",
                    "rule": "v2_to_v1:merge_shards_and_drop_bitmap_index",
                }
            ]
        )
    return discarded


def convert_ledger(
    source_version: int,
    source_path: str | Path,
    target_version: int,
    target_path: str | Path,
    *,
    expected: CSRMatrix | None = None,
) -> dict[str, Any]:
    source_matrix, source_info = read_ledger(source_version, source_path)
    target = Path(target_path)
    write_ledger(target_version, target, source_matrix)
    target_matrix, target_info = read_ledger(target_version, target)

    comparison_base = expected or source_matrix
    assert_semantic_equal(
        comparison_base,
        target_matrix,
        code="E_SEMANTIC_DRIFT",
        stage="migrate_semantic",
        context={
            "source_version": source_version,
            "target_version": target_version,
            "source_path": str(source_path),
            "target_path": str(target),
        },
    )
    if expected is not None:
        assert_semantic_equal(
            source_matrix,
            expected,
            code="E_SEMANTIC_DRIFT",
            stage="migrate_semantic",
            context={"source_version": source_version, "check": "source_to_expected"},
        )

    return {
        "ok": True,
        "source_version": source_version,
        "target_version": target_version,
        "source_info": source_info,
        "target_info": target_info,
        "completed": _completion_records(target_version, target),
        "discarded": _discard_records(source_version, target_version),
        "semantic_equal": True,
        "first_diff": None,
        "content_hash": target_matrix.digest,
    }


def migrate_three_views(matrix: CSRMatrix, output_dir: str | Path) -> dict[str, Path]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    paths = {
        1: root / "v1_ledger.json",
        2: root / "v2",
        3: root / "v3_ledger.json",
    }
    for version, path in paths.items():
        write_ledger(version, path, matrix)
    return paths
