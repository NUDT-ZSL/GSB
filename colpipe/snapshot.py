"""Session snapshots, version comparison and whole-state replacement restore."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .catalog import INPUT_FORMAT, Dataset, PipelineRequest, load_pipeline_dict
from .codes import E_DATA, E_VERSION
from .errors import PipeError
from .executor import ExecutionResult
from .plan import PlanBundle
from .stats import build_statistics, statistics_core_sha256
from .util import canonical_json, read_json, sha256_json, write_json

SNAPSHOT_FORMAT = "colpipe.snapshot"
SNAPSHOT_FORMAT_VERSION = 1


@dataclass(frozen=True)
class RestoredSession:
    request: PipelineRequest
    statistics: dict[str, Any]
    plan_history: list[dict[str, Any]]
    report: dict[str, Any]


def _plan_history_entry(sequence: int, execution: ExecutionResult) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "mode": execution.mode,
        "plan_summary_sha256": execution.plan_summary_sha256,
        "result_sha256": execution.canonical_sha256,
        "scan_tasks": execution.stats["scan_tasks"],
        "scanned_rows": execution.stats["scanned_rows"],
        "aggregate_input_rows": execution.stats["aggregate_input_rows"],
        "result_rows": execution.stats["result_rows"],
        "business_code": "OK",
    }


def create_snapshot(
    bundle: PlanBundle,
    executions: Mapping[str, ExecutionResult],
    comparison: dict[str, Any],
    *,
    plan_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    request = bundle.request
    execution_payloads = {mode: result.to_artifact() for mode, result in executions.items()}
    if plan_history is None:
        plan_history = [
            _plan_history_entry(index + 1, executions[mode])
            for index, mode in enumerate([mode for mode in ("baseline", "optimized") if mode in executions])
        ]
    payload: dict[str, Any] = {
        "format": SNAPSHOT_FORMAT,
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "input_format": INPUT_FORMAT,
        "session_id": request.dataset.session_id,
        "data_version": request.dataset.data_version,
        "schema_fingerprint": request.dataset.schema_fingerprint,
        "schema_versions": request.dataset.schema_versions(),
        "data_digest": request.dataset.data_digest,
        "query_summary_sha256": request.query.summary_sha256,
        "dataset": {
            "session_id": request.dataset.session_id,
            "data_version": request.dataset.data_version,
            "tables": [table.data_dict() for table in request.dataset.tables],
        },
        "query": request.query_text,
        "settings": request.settings.to_dict(),
        "statistics": bundle.statistics,
        "index_set": bundle.indexes,
        "plan_history": plan_history,
        "plans": {
            "optimized": {
                "summary_sha256": bundle.optimized_summary_sha256,
                "plan": bundle.optimized_plan,
                "summary": bundle.optimized_summary,
            },
            "baseline": {
                "summary_sha256": bundle.baseline_summary_sha256,
                "plan": bundle.baseline_plan,
                "summary": bundle.baseline_summary,
            },
            "pushdown": bundle.pushdown,
            "pruning": bundle.pruning,
            "estimation": bundle.estimates,
            "split_plan": bundle.split_plan,
            "baseline_split_plan": bundle.baseline_split_plan,
        },
        "executions": execution_payloads,
        "comparison": comparison,
        "artifact_contract": {
            "result_encoding": "CPAGG1 big-endian typed columnar bytes",
            "trace_clock": "time.perf_counter_ns converted to milliseconds",
            "memory_unit": "bytes",
            "statistics_scope": "scan/filter task peak column buffers and shared aggregate statistics",
        },
    }
    payload["snapshot_id"] = sha256_json(payload)
    return payload


def save_snapshot(path: str | Path, snapshot: dict[str, Any]) -> None:
    write_json(path, snapshot)


def load_snapshot(path: str | Path) -> dict[str, Any]:
    try:
        payload = read_json(path)
    except (OSError, ValueError) as exc:
        raise PipeError(E_VERSION, f"cannot read snapshot: {exc}", "$.snapshot") from exc
    if not isinstance(payload, dict):
        raise PipeError(E_VERSION, "snapshot must be a JSON object", "$.snapshot")
    snapshot_id = payload.get("snapshot_id")
    content = dict(payload)
    content.pop("snapshot_id", None)
    expected = sha256_json(content)
    if not isinstance(snapshot_id, str) or snapshot_id != expected:
        raise PipeError(
            E_VERSION,
            "snapshot checksum does not match its content",
            "$.snapshot_id",
            {"expected": expected, "actual": snapshot_id},
        )
    return payload


def _table_differences(current: Dataset, snapshot_dataset: Dataset) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    current_tables = current.table_map
    snapshot_tables = snapshot_dataset.table_map
    for name in sorted(set(current_tables) | set(snapshot_tables)):
        if name not in current_tables:
            differences.append({"kind": "table_added", "table": name})
            continue
        if name not in snapshot_tables:
            differences.append({"kind": "table_removed", "table": name})
            continue
        left = current_tables[name]
        right = snapshot_tables[name]
        if left.schema_version != right.schema_version:
            differences.append(
                {
                    "kind": "schema_version_changed",
                    "table": name,
                    "current": left.schema_version,
                    "snapshot": right.schema_version,
                }
            )
        if [column.schema_dict() for column in left.columns] != [column.schema_dict() for column in right.columns]:
            differences.append({"kind": "schema_changed", "table": name})
        left_digest = sha256_json(left.data_dict())
        right_digest = sha256_json(right.data_dict())
        if left_digest != right_digest:
            differences.append(
                {
                    "kind": "table_data_changed",
                    "table": name,
                    "current_digest": left_digest,
                    "snapshot_digest": right_digest,
                }
            )
    return differences


def _validate_index_set(snapshot: Mapping[str, Any], dataset: Dataset) -> None:
    indexes = snapshot.get("index_set", [])
    if not isinstance(indexes, list):
        raise PipeError(E_VERSION, "snapshot index_set must be an array", "$.index_set")
    for index, item in enumerate(indexes):
        if not isinstance(item, dict):
            raise PipeError(E_VERSION, "snapshot index entry must be an object", f"$.index_set[{index}]")
        table_name = item.get("table")
        columns = item.get("columns")
        if table_name not in dataset.table_map:
            raise PipeError(
                E_VERSION,
                "snapshot references an index on a missing table",
                f"$.index_set[{index}].table",
                {"table": table_name},
            )
        table = dataset.table(str(table_name))
        for column_index, column in enumerate(columns or []):
            try:
                table.column(str(column))
            except KeyError as exc:
                raise PipeError(
                    E_VERSION,
                    "snapshot references an index on a missing column",
                    f"$.index_set[{index}].columns[{column_index}]",
                    {"table": table_name, "column": column},
                ) from exc


def _restore_request(snapshot: Mapping[str, Any]) -> PipelineRequest:
    dataset = snapshot.get("dataset")
    if not isinstance(dataset, dict):
        raise PipeError(E_VERSION, "snapshot is missing its embedded dataset", "$.dataset")
    pipeline_dict = {
        "format": snapshot.get("input_format"),
        "session_id": dataset.get("session_id"),
        "data_version": dataset.get("data_version"),
        "tables": dataset.get("tables"),
        "query": snapshot.get("query"),
        "settings": snapshot.get("settings"),
    }
    try:
        return load_pipeline_dict(pipeline_dict, source_path="$.snapshot")
    except PipeError as exc:
        raise PipeError(
            E_VERSION,
            "snapshot dataset or query cannot be reconstructed",
            exc.path,
            {"cause": exc.to_dict()},
        ) from exc


def restore_snapshot(snapshot: Mapping[str, Any], current: PipelineRequest) -> RestoredSession:
    """Restore by replacing the entire table/statistics state.

    A different data version on the same session lineage is a legitimate
    replacement.  A different session id or an unsupported snapshot format is
    not a match and is rejected with E_VERSION and a difference list.
    """

    if snapshot.get("format") != SNAPSHOT_FORMAT:
        raise PipeError(
            E_VERSION,
            "unsupported snapshot format",
            "$.format",
            {"expected": SNAPSHOT_FORMAT, "actual": snapshot.get("format")},
        )
    if snapshot.get("format_version") != SNAPSHOT_FORMAT_VERSION:
        raise PipeError(
            E_VERSION,
            "unsupported snapshot format version",
            "$.format_version",
            {
                "expected": SNAPSHOT_FORMAT_VERSION,
                "actual": snapshot.get("format_version"),
            },
        )
    if snapshot.get("input_format") != INPUT_FORMAT:
        raise PipeError(
            E_VERSION,
            "snapshot input format cannot be mapped to the current loader",
            "$.input_format",
            {"expected": INPUT_FORMAT, "actual": snapshot.get("input_format")},
        )
    if snapshot.get("session_id") != current.dataset.session_id:
        differences = [
            {
                "kind": "session_id_changed",
                "current": current.dataset.session_id,
                "snapshot": snapshot.get("session_id"),
            },
            {
                "kind": "data_version_changed",
                "current": current.dataset.data_version,
                "snapshot": snapshot.get("data_version"),
            },
        ]
        raise PipeError(
            E_VERSION,
            "snapshot session version cannot be mapped to the current session",
            "$.session_id",
            {"differences": differences},
        )

    restored = _restore_request(snapshot)
    if restored.dataset.session_id != snapshot.get("session_id"):
        raise PipeError(
            E_VERSION,
            "embedded session id does not match the snapshot session id",
            "$.dataset.session_id",
            {"snapshot": snapshot.get("session_id"), "embedded": restored.dataset.session_id},
        )
    if restored.dataset.data_version != snapshot.get("data_version"):
        raise PipeError(
            E_VERSION,
            "embedded data version does not match the snapshot data version",
            "$.dataset.data_version",
            {"snapshot": snapshot.get("data_version"), "embedded": restored.dataset.data_version},
        )
    if restored.dataset.schema_versions() != snapshot.get("schema_versions"):
        raise PipeError(
            E_VERSION,
            "embedded table schema versions do not match the snapshot schema versions",
            "$.schema_versions",
            {"snapshot": snapshot.get("schema_versions"), "embedded": restored.dataset.schema_versions()},
        )
    if restored.dataset.data_digest != snapshot.get("data_digest"):
        raise PipeError(
            E_VERSION,
            "embedded dataset digest does not match the snapshot digest",
            "$.data_digest",
            {
                "snapshot": snapshot.get("data_digest"),
                "embedded": restored.dataset.data_digest,
            },
        )
    if restored.dataset.schema_fingerprint != snapshot.get("schema_fingerprint"):
        raise PipeError(
            E_VERSION,
            "embedded schema fingerprint does not match the snapshot fingerprint",
            "$.schema_fingerprint",
            {
                "snapshot": snapshot.get("schema_fingerprint"),
                "embedded": restored.dataset.schema_fingerprint,
            },
        )
    stored_statistics = snapshot.get("statistics")
    if not isinstance(stored_statistics, dict):
        raise PipeError(E_VERSION, "snapshot statistics are missing", "$.statistics")
    rebuilt_statistics = build_statistics(restored.dataset)
    stored_hash = stored_statistics.get("statistics_sha256")
    rebuilt_hash = statistics_core_sha256(rebuilt_statistics)
    if stored_hash != rebuilt_hash:
        raise PipeError(
            E_VERSION,
            "snapshot statistics cannot be reproduced from the embedded tables",
            "$.statistics.statistics_sha256",
            {"stored": stored_hash, "rebuilt": rebuilt_hash},
        )
    _validate_index_set(snapshot, restored.dataset)

    differences = _table_differences(current.dataset, restored.dataset)
    report = {
        "status": "PASS",
        "mode": "replace_all_not_merge",
        "session_id": restored.dataset.session_id,
        "current_data_version": current.dataset.data_version,
        "snapshot_data_version": restored.dataset.data_version,
        "version_changed": current.dataset.data_version != restored.dataset.data_version,
        "data_digest_changed": current.dataset.data_digest != restored.dataset.data_digest,
        "schema_fingerprint_changed": current.dataset.schema_fingerprint
        != restored.dataset.schema_fingerprint,
        "statistics_replaced": True,
        "tables_replaced": len(restored.dataset.tables),
        "indexes_restored": len(snapshot.get("index_set", [])),
        "plan_history_entries_restored": len(snapshot.get("plan_history", [])),
        "execution_traces_restored": sorted(snapshot.get("executions", {}).keys()),
        "differences": differences,
    }
    plan_history = snapshot.get("plan_history", [])
    if not isinstance(plan_history, list):
        raise PipeError(E_VERSION, "snapshot plan_history must be an array", "$.plan_history")
    return RestoredSession(restored, rebuilt_statistics, plan_history, report)
