"""Deterministic fault-injection runner for parser/merge/lag validation."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .errors import DiagnosticsError
from .lag import compute_lag
from .pipeline import load_sources
from .util import read_json, sha256_file, stable_json_text


def _copy_source_tree(config: Config, destination: Path) -> Config:
    shutil.copy2(config.v1_path, destination / "positions.jsonl")
    if config.v2_manifest is not None:
        shutil.copytree(config.v2_manifest.parent, destination / "v2")
    shutil.copytree(config.logs_path, destination / "logs")

    v2_value = "v2/manifest.json" if config.v2_manifest is not None else None
    payload = {
        "v1_path": "positions.jsonl",
        "v2_manifest": v2_value,
        "logs_path": "logs",
        "v1_base_clock_ms": config.v1_base_clock_ms,
        "v1_clock_step_ms": config.v1_clock_step_ms,
        "v1_scale": {
            "numerator": config.v1_scale.numerator,
            "denominator": config.v1_scale.denominator,
        },
        "v1_rounding": config.v1_rounding,
        "window_size": config.window_size,
        "window_stride": config.window_stride,
        "tolerance_ms": f"{config.tolerance.numerator}/{config.tolerance.denominator}",
        "observation_error_ms": (
            f"{config.observation_error.numerator}/{config.observation_error.denominator}"
        ),
        "v1_observation_error_ms": (
            f"{config.v1_observation_error.numerator}/{config.v1_observation_error.denominator}"
        ),
        "require_contiguous": config.require_contiguous,
        "strict_clock_order": config.strict_clock_order,
    }
    config_path = destination / "config.json"
    config_path.write_text(stable_json_text(payload), encoding="utf-8")
    return load_config(config_path)


def _rewrite_jsonl(path: Path, mutator) -> None:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows = mutator(rows)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _refresh_manifest_hashes(manifest_path: Path) -> None:
    manifest = read_json(manifest_path)
    base = manifest_path.parent
    for shard in manifest["shards"]:
        shard["sha256"] = sha256_file(base / shard["path"])
    manifest["metadata_db"]["sha256"] = sha256_file(
        base / manifest["metadata_db"]["path"]
    )
    manifest_path.write_text(stable_json_text(manifest), encoding="utf-8")


def _apply_injection(config: Config, injection: dict[str, Any]) -> None:
    kind = injection.get("kind")
    target = injection.get("target")
    if kind == "set_v1_field":
        line = injection.get("line")
        field = injection.get("field")
        value = injection.get("value")

        def mutate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            rows[int(line) - 1][str(field)] = value
            return rows

        _rewrite_jsonl(config.v1_path, mutate)
    elif kind == "set_log_field":
        line = int(injection.get("line", 1))
        field = str(injection["field"])
        value = injection.get("value")
        log_files = sorted(
            config.logs_path.rglob("*.jsonl"),
            key=lambda item: item.relative_to(config.logs_path).as_posix(),
        )
        if not log_files:
            raise DiagnosticsError("E_INJECTION", details={"reason": "no replay log"})

        def mutate_log(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            rows[line - 1][field] = value
            return rows

        _rewrite_jsonl(log_files[0], mutate_log)
    elif kind == "drop_log_line":
        line = int(injection.get("line", 1))
        log_files = sorted(
            config.logs_path.rglob("*.jsonl"),
            key=lambda item: item.relative_to(config.logs_path).as_posix(),
        )
        if not log_files:
            raise DiagnosticsError("E_INJECTION", details={"reason": "no replay log"})

        def mutate_log(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            del rows[line - 1]
            return rows

        _rewrite_jsonl(log_files[0], mutate_log)
    elif kind == "drop_v1_line":
        line = int(injection["line"])

        def mutate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            del rows[line - 1]
            return rows

        _rewrite_jsonl(config.v1_path, mutate)
    elif kind == "set_manifest_shard_hash":
        if config.v2_manifest is None:
            raise DiagnosticsError("E_INJECTION", details={"reason": "no v2 manifest"})
        manifest = read_json(config.v2_manifest)
        index = int(injection.get("index", 0))
        manifest["shards"][index]["sha256"] = injection.get("value", "0" * 64)
        config.v2_manifest.write_text(stable_json_text(manifest), encoding="utf-8")
    elif kind == "set_shard_metadata_digest":
        if config.v2_manifest is None:
            raise DiagnosticsError("E_INJECTION", details={"reason": "no v2 manifest"})
        manifest = read_json(config.v2_manifest)
        shard_path = config.v2_manifest.parent / manifest["shards"][0]["path"]
        shard = read_json(shard_path)
        shard["records"][int(injection.get("index", 0))]["metadata_digest"] = injection.get(
            "value", "0" * 64
        )
        shard_path.write_text(stable_json_text(shard), encoding="utf-8")
        _refresh_manifest_hashes(config.v2_manifest)
    elif kind == "sqlite_update":
        if config.v2_manifest is None:
            raise DiagnosticsError("E_INJECTION", details={"reason": "no v2 manifest"})
        manifest = read_json(config.v2_manifest)
        metadata_path = config.v2_manifest.parent / manifest["metadata_db"]["path"]
        column = str(injection["column"])
        allowed = {
            "replay_generation",
            "commit_time_ms",
            "replay_time_ms",
            "tombstone_applied",
            "logical_clock",
        }
        if column not in allowed:
            raise DiagnosticsError("E_INJECTION", details={"reason": f"unsupported column {column}"})
        values = injection.get("values")
        if values is None:
            values = {column: injection["value"]}
        if not isinstance(values, dict):
            raise DiagnosticsError(
                "E_INJECTION", details={"reason": "sqlite_update values must be an object"}
            )
        if any(key not in allowed for key in values):
            raise DiagnosticsError(
                "E_INJECTION",
                details={"reason": "unsupported sqlite column", "columns": sorted(values)},
            )
        assignments = ", ".join(f"{key}=?" for key in sorted(values))
        connection = sqlite3.connect(metadata_path)
        try:
            connection.execute(
                f"UPDATE tx_metadata SET {assignments} WHERE tx_id=?",
                (*[values[key] for key in sorted(values)], injection["tx_id"]),
            )
            connection.commit()
        finally:
            connection.close()
        _refresh_manifest_hashes(config.v2_manifest)
    elif kind == "corrupt_metadata_hash":
        if config.v2_manifest is None:
            raise DiagnosticsError("E_INJECTION", details={"reason": "no v2 manifest"})
        manifest = read_json(config.v2_manifest)
        metadata_path = config.v2_manifest.parent / manifest["metadata_db"]["path"]
        with metadata_path.open("ab") as handle:
            handle.write(b"\x00injected")
    else:
        raise DiagnosticsError(
            "E_INJECTION", details={"reason": f"unsupported injection kind {kind}", "target": target}
        )


def run_injection(config_path: str | Path, injection: dict[str, Any]) -> dict[str, Any]:
    base_config = load_config(config_path)
    with tempfile.TemporaryDirectory(prefix="replication-diag-inject-") as temp:
        temp_path = Path(temp)
        config = _copy_source_tree(base_config, temp_path)
        _apply_injection(config, injection)
        observed_code: str | None = None
        observed_message: str | None = None
        try:
            bundle = load_sources(config)
            from .merge import merge_records

            merged = merge_records(bundle.v1_records, bundle.v2_records, config)
            compute_lag(
                merged,
                bundle.log_records,
                config,
                snapshot_version=1,
                package_version=(
                    bundle.manifest["package_version"] if bundle.manifest else "1.0.0-v1"
                ),
                logical_clock_floor=(
                    bundle.manifest["logical_clock_floor"] if bundle.manifest else config.v1_base_clock_ms
                ),
            )
        except DiagnosticsError as exc:
            observed_code = exc.code
            observed_message = exc.message
        expected = injection.get("expected_error")
        passed = observed_code == expected
        return {
            "id": injection.get("id"),
            "target": injection.get("target"),
            "kind": injection.get("kind"),
            "expected_error": expected,
            "observed_error": observed_code,
            "observed_message": observed_message,
            "passed": passed,
        }


def run_injection_manifest(
    config_path: str | Path,
    manifest_path: str | Path,
    injection_id: str | None = None,
) -> dict[str, Any]:
    data = read_json(Path(manifest_path))
    if not isinstance(data, list):
        raise DiagnosticsError("E_INJECTION", details={"reason": "array required"})
    selected = [
        item
        for item in data
        if injection_id is None or (isinstance(item, dict) and item.get("id") == injection_id)
    ]
    if injection_id is not None and not selected:
        raise DiagnosticsError(
            "E_INJECTION", details={"reason": f"unknown injection id {injection_id}"}
        )
    results = [run_injection(config_path, item) for item in selected]
    return {
        "ok": all(result["passed"] for result in results),
        "status": "passed" if all(result["passed"] for result in results) else "failed",
        "count": len(results),
        "results": results,
    }
