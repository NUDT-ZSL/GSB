"""Build the reproducible external-sort accounting report."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import SortConfig
from .manifest import atomic_write_json, temp_file_listing


def _relative_or_absolute(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def build_report(
    manifest: dict[str, Any],
    config: SortConfig,
) -> dict[str, Any]:
    spill_record_counts = list(manifest.get("spill_segment_record_counts", []))
    spill_segment_count = int(manifest.get("spill_segment_count", 0))
    merge_passes = int(manifest.get("merge_passes_completed", 0))
    intermediate_passes = int(
        manifest.get("intermediate_merge_passes_completed", 0)
    )
    peak_spill = int(manifest.get("spill_peak_memory_bytes", 0))
    peak_merge = int(manifest.get("merge_peak_memory_bytes", 0))
    peak_memory = max(peak_spill, peak_merge)
    temp_files_remaining = temp_file_listing(config.work_dir)
    report_relative = None
    try:
        report_relative = str(config.effective_report_path.relative_to(config.work_dir))
    except ValueError:
        report_relative = None

    remaining_work_dir_files = sorted(
        {
            item["name"] for item in temp_files_remaining
        }
        | {
            path.name
            for path in config.work_dir.iterdir()
            if path.is_file()
        }
    )
    if report_relative is not None:
        remaining_work_dir_files = sorted(
            set(remaining_work_dir_files) | {report_relative}
        )

    report = {
        "version": 1,
        "input_record_count": int(manifest.get("input_record_count", 0)),
        "output_record_count": int(manifest.get("result_record_count", 0)),
        "spill_segment_count": spill_segment_count,
        "spill_segment_record_counts": spill_record_counts,
        "merge_passes": merge_passes,
        "intermediate_merge_passes": intermediate_passes,
        "peak_memory_bytes": peak_memory,
        "peak_spill_memory_bytes": peak_spill,
        "peak_merge_memory_bytes": peak_merge,
        "mem_limit_bytes": int(manifest["mem_limit_bytes"]),
        "max_fan_in": int(manifest["max_fan_in"]),
        "temp_files_total_bytes": int(manifest.get("spill_temp_bytes", 0)),
        "temp_bytes_written": int(manifest.get("temp_bytes_written", 0)),
        "temp_peak_disk_bytes": int(manifest.get("temp_peak_disk_bytes", 0)),
        "temp_files_remaining": temp_files_remaining,
        "temp_files_remaining_bytes": sum(
            int(item["size_bytes"]) for item in temp_files_remaining
        ),
        "work_dir": str(config.work_dir),
        "work_dir_remaining_files": remaining_work_dir_files,
        "source_table": manifest["source_table"],
        "result_table": manifest["result_table"],
        "source_db_path": manifest["db_path"],
        "output_db_path": manifest["output_db_path"],
        "source_sha256": manifest.get("source_sha256"),
        "result_sha256": manifest.get("result_sha256"),
        "result_digest_algorithm": "sha256-chain over ordered record payloads",
        "input_order": manifest.get("input_order"),
        "output_order": ["trigger_ts ASC", "input_seq ASC"],
        "memory_accounting_unit": "UTF-8 JSON record payload bytes",
        "temp_file_accounting_unit": "segment file and manifest bytes",
    }
    return report


def write_report(
    manifest: dict[str, Any],
    config: SortConfig,
) -> dict[str, Any]:
    report = build_report(manifest, config)
    atomic_write_json(config.effective_report_path, report)
    return report
