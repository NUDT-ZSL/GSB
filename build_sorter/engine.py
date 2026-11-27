"""High-level orchestration for fresh, split-phase, and resumed runs."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .config import SortConfig
from .errors import ResumeError, SorterError
from .manifest import (
    cleanup_temp_files,
    load_manifest,
    manifest_path,
)
from .merge import merge_all_segments
from .report import write_report
from .source import quote_identifier
from .spill import spill_database


def sort_database(
    *,
    db_path: str | Path,
    work_dir: str | Path,
    mem_limit_bytes: int,
    max_fan_in: int,
    source_table: str = "build_task",
    result_table: str = "build_task_sorted",
    output_db_path: str | Path | None = None,
    report_path: str | Path | None = None,
    checkpoint_records: int = 1000,
    resume: bool = False,
    verify_segments: bool = True,
) -> dict[str, Any]:
    config = SortConfig(
        db_path=Path(db_path),
        work_dir=Path(work_dir),
        mem_limit_bytes=mem_limit_bytes,
        max_fan_in=max_fan_in,
        source_table=source_table,
        result_table=result_table,
        output_db_path=Path(output_db_path) if output_db_path is not None else None,
        report_path=Path(report_path) if report_path is not None else None,
        checkpoint_records=checkpoint_records,
        resume=resume,
        verify_segments=verify_segments,
    )
    return run_all(config)


def _drop_result_table(config: SortConfig) -> None:
    if not config.output_db_path.exists():
        return
    connection = sqlite3.connect(config.output_db_path)
    try:
        quoted = quote_identifier(config.result_table)
        connection.execute(f"DROP TABLE IF EXISTS {quoted}")
        connection.commit()
    except sqlite3.Error as exc:
        raise SorterError(
            "OUTPUT_TABLE_ERROR",
            "unable to reset the output table",
            details={"table": config.result_table, "error": str(exc)},
        ) from exc
    finally:
        connection.close()


def _remove_stale_report(config: SortConfig) -> None:
    try:
        config.effective_report_path.unlink()
    except FileNotFoundError:
        pass


def ensure_manifest_matches_config(
    manifest: dict[str, Any],
    config: SortConfig,
) -> None:
    comparisons = (
        ("db_path", str(config.db_path)),
        ("output_db_path", str(config.output_db_path)),
        ("source_table", config.source_table),
        ("result_table", config.result_table),
        ("mem_limit_bytes", config.mem_limit_bytes),
        ("max_fan_in", config.max_fan_in),
    )
    mismatches = {
        name: {"manifest": manifest.get(name), "requested": requested}
        for name, requested in comparisons
        if manifest.get(name) != requested
    }
    if mismatches:
        raise ResumeError(
            "command-line configuration differs from the sort manifest",
            mismatches=mismatches,
        )


def prepare_fresh_run(config: SortConfig) -> None:
    config.work_dir.mkdir(parents=True, exist_ok=True)
    cleanup_temp_files(config.work_dir)
    _remove_stale_report(config)
    _drop_result_table(config)


def run_spill_phase(config: SortConfig) -> dict[str, Any]:
    try:
        prepare_fresh_run(config)
        return spill_database(config)
    except Exception:
        cleanup_after_failure(config)
        raise


def _finalize_from_manifest(
    manifest: dict[str, Any],
    config: SortConfig,
) -> dict[str, Any]:
    cleanup_temp_files(config.work_dir)
    return write_report(manifest, config)


def run_merge_phase(config: SortConfig) -> dict[str, Any]:
    try:
        config.work_dir.mkdir(parents=True, exist_ok=True)
        manifest = load_manifest(config.work_dir)
        ensure_manifest_matches_config(manifest, config)
        if manifest.get("state") == "spilling":
            raise ResumeError(
                "spill phase was interrupted before a complete manifest was written",
                state=manifest.get("state"),
            )
        manifest = merge_all_segments(manifest, config)
        return _finalize_from_manifest(manifest, config)
    except Exception:
        cleanup_after_failure(config)
        raise


def _run_all_impl(config: SortConfig) -> dict[str, Any]:
    config.work_dir.mkdir(parents=True, exist_ok=True)
    if config.resume and manifest_path(config.work_dir).is_file():
        manifest = load_manifest(config.work_dir)
        ensure_manifest_matches_config(manifest, config)
        if manifest.get("state") == "spilling":
            prepare_fresh_run(config)
            manifest = spill_database(config)
            manifest = merge_all_segments(manifest, config)
        elif manifest.get("state") == "spilled" or manifest.get("state") == "merging":
            manifest = merge_all_segments(manifest, config)
        elif manifest.get("state") == "final":
            manifest = merge_all_segments(manifest, config)
        elif manifest.get("state") == "complete":
            pass
        else:
            raise ResumeError(
                "manifest is in an unknown state",
                state=manifest.get("state"),
            )
        return _finalize_from_manifest(manifest, config)

    prepare_fresh_run(config)
    manifest = spill_database(config)
    manifest = merge_all_segments(manifest, config)
    return _finalize_from_manifest(manifest, config)


def run_all(config: SortConfig) -> dict[str, Any]:
    try:
        return _run_all_impl(config)
    except Exception:
        cleanup_after_failure(config)
        raise


def cleanup_after_failure(config: SortConfig | None) -> None:
    if config is None:
        return
    try:
        cleanup_temp_files(config.work_dir)
    except OSError:
        pass
    try:
        _remove_stale_report(config)
    except OSError:
        pass
    try:
        if config.output_db_path.exists():
            connection = sqlite3.connect(config.output_db_path)
            try:
                quoted = quote_identifier(config.result_table)
                connection.execute(f"DROP TABLE IF EXISTS {quoted}")
                connection.commit()
            finally:
                connection.close()
    except (OSError, sqlite3.Error, SorterError):
        pass
