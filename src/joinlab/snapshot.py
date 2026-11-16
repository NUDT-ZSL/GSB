"""Whole-session snapshots and replay verification."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .catalog import Catalog, load_catalog_document
from .cost import CostParams
from .errors import JoinLabError, SchemaError, SnapshotError
from .session import Evaluation, ExperimentSession, SessionVersions
from .typesys import json_dumps_stable


SNAPSHOT_FORMAT = "joinlab.snapshot.v1"


@dataclass(frozen=True)
class RestoreResult:
    session: ExperimentSession
    snapshot_path: str
    mode: str
    version_comparison: dict[str, Any]
    replayed_queries: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_path": self.snapshot_path,
            "mode": self.mode,
            "version_comparison": self.version_comparison,
            "replayed_queries": self.replayed_queries,
            "versions": self.session.versions.to_dict(),
            "history_count": len(self.session.history),
            "sample_scale": self.session.sample_scale,
        }


def save_snapshot(session: ExperimentSession, path: str | Path) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    with session._lock:  # Snapshot must capture one coherent version.
        versions = session.versions
        params = session.params
        sample_scale = session.sample_scale
        all_history = list(session.history)
    # Results from superseded parameter/data versions are deliberately not
    # serialized as replayable current-version evidence.  They must never enter
    # selection, and replaying them under the snapshot's current parameters
    # would be expected to change the selected plan.
    history = [entry for entry in all_history if entry.versions == versions]
    latest_by_query: dict[str, Any] = {}
    for entry in history:
        latest_by_query[entry.query.query_id] = entry
    history = sorted(latest_by_query.values(), key=lambda entry: entry.sequence)
    stale_omitted = len(all_history) - len(history)
    payload: dict[str, Any] = {
        "format": SNAPSHOT_FORMAT,
        "versions": versions.to_dict(),
        "catalog": session.catalog.to_snapshot_dict(),
        "cost_params": params.to_dict(),
        "sample_scale": sample_scale,
        "stale_history_omitted": stale_omitted,
        "history": [
            {
                **entry.to_dict(include_rows=False, include_candidates=True),
                "source_sql": entry.query.source_text,
                "result_summary": entry.summary,
            }
            for entry in history
        ],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json_dumps_stable(payload, pretty=True), encoding="utf-8")
    return {
        "ok": True,
        "snapshot_path": str(target),
        "versions": versions.to_dict(),
        "history_count": len(history),
        "stale_history_omitted": stale_omitted,
        "sample_scale": sample_scale,
    }


def _read_snapshot(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise SnapshotError("snapshot file does not exist", str(path)) from exc
    except json.JSONDecodeError as exc:
        raise SnapshotError(
            f"invalid snapshot JSON: {exc.msg}",
            f"{path}@{exc.lineno}:{exc.colno}",
        ) from exc
    except OSError as exc:
        raise SnapshotError(f"cannot read snapshot: {exc}", str(path)) from exc


def _required_int(mapping: dict[str, Any], key: str, path: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SnapshotError(f"snapshot is missing a positive integer {key}", path)
    return value


def _expect(mapping: dict[str, Any], key: str, path: str) -> Any:
    if key not in mapping:
        raise SnapshotError(f"snapshot is missing required field {key!r}", path)
    return mapping[key]


def restore_snapshot(
    path: str | Path,
    *,
    current_session: ExperimentSession | None = None,
) -> RestoreResult:
    """Build a replacement session and replay every recorded query.

    A snapshot is never merged into the current session.  Table definitions,
    rows, cost parameters, versions, and result caches are all reconstructed
    from the snapshot in a fresh session object.
    """

    target = Path(path).expanduser().resolve()
    document = _read_snapshot(target)
    if not isinstance(document, dict):
        raise SnapshotError("snapshot root must be an object", "$")
    if document.get("format") != SNAPSHOT_FORMAT:
        raise SnapshotError(
            f"unsupported snapshot format; expected {SNAPSHOT_FORMAT}",
            "$.format",
        )
    versions_raw = document.get("versions")
    if not isinstance(versions_raw, dict):
        raise SnapshotError("snapshot versions must be an object", "$.versions")
    schema_version = _required_int(versions_raw, "schema_version", "$.versions.schema_version")
    data_version = _required_int(versions_raw, "data_version", "$.versions.data_version")
    # This explicit check is important for the injected missing-version case.
    param_version = _required_int(versions_raw, "param_version", "$.versions.param_version")

    catalog_raw = _expect(document, "catalog", "$.catalog")
    try:
        catalog = load_catalog_document(catalog_raw, base_dir=target.parent, source_path=str(target))
    except JoinLabError as exc:
        raise SnapshotError(
            f"snapshot catalog is invalid: {exc.message}",
            f"$.catalog:{exc.path}",
            original_code=exc.code,
        ) from exc
    if catalog.schema_version != schema_version:
        raise SnapshotError(
            "snapshot catalog schema_version does not match snapshot versions",
            "$.catalog.schema_version",
            expected=schema_version,
            actual=catalog.schema_version,
        )

    try:
        params = CostParams.from_mapping(_expect(document, "cost_params", "$.cost_params"))
    except (SchemaError, TypeError, ValueError) as exc:
        raise SnapshotError(
            f"snapshot cost parameters are invalid: {getattr(exc, 'message', str(exc))}",
            "$.cost_params",
        ) from exc
    sample_scale = document.get("sample_scale")
    if isinstance(sample_scale, bool) or not isinstance(sample_scale, (int, float)):
        raise SnapshotError("sample_scale must be a number", "$.sample_scale")
    try:
        session = ExperimentSession(
            catalog,
            params=params,
            sample_scale=float(sample_scale),
            param_version=param_version,
        )
        session.replace_sample_scale(float(sample_scale), data_version=data_version)
    except (SchemaError, TypeError, ValueError) as exc:
        raise SnapshotError(
            f"snapshot session metadata is invalid: {getattr(exc, 'message', str(exc))}",
            "$.sample_scale",
        ) from exc

    history_raw = document.get("history", [])
    if not isinstance(history_raw, list):
        raise SnapshotError("history must be an array", "$.history")

    current_versions = current_session.versions.to_dict() if current_session is not None else None
    same = (
        current_versions
        == {"schema_version": schema_version, "data_version": data_version, "param_version": param_version}
    )
    version_comparison = {
        "current": current_versions,
        "snapshot": {
            "schema_version": schema_version,
            "data_version": data_version,
            "param_version": param_version,
        },
        "same_versions": same,
        "strategy": "full_replace",
    }

    differences: list[dict[str, Any]] = []
    replayed = 0
    for index, entry in enumerate(history_raw):
        entry_path = f"$.history[{index}]"
        if not isinstance(entry, dict):
            differences.append({"path": entry_path, "field": "entry", "expected": "object", "actual": type(entry).__name__})
            continue
        entry_versions = entry.get("versions")
        expected_entry_versions = {
            "schema_version": schema_version,
            "data_version": data_version,
            "param_version": param_version,
        }
        if entry_versions != expected_entry_versions:
            differences.append(
                {
                    "path": entry_path,
                    "field": "versions",
                    "expected": expected_entry_versions,
                    "actual": entry_versions,
                }
            )
        source_sql = entry.get("source_sql")
        if not isinstance(source_sql, str) or not source_sql.strip():
            differences.append(
                {
                    "path": entry_path,
                    "field": "source_sql",
                    "expected": "non-empty SQL text",
                    "actual": source_sql,
                }
            )
            continue
        name = entry.get("name")
        query_info = entry.get("query") if isinstance(entry.get("query"), dict) else {}
        expected_query_id = query_info.get("query_id")
        try:
            replay = session.evaluate(source_sql, name=name)
        except JoinLabError as exc:
            differences.append(
                {
                    "path": entry_path,
                    "field": "replay",
                    "expected": "successful deterministic replay",
                    "actual": exc.to_dict(),
                }
            )
            continue
        replayed += 1

        selected_plan = entry.get("selected_plan") if isinstance(entry.get("selected_plan"), dict) else {}
        execution = entry.get("execution") if isinstance(entry.get("execution"), dict) else {}
        fallback = entry.get("fallback_execution") if isinstance(entry.get("fallback_execution"), dict) else {}
        consistency = entry.get("consistency") if isinstance(entry.get("consistency"), dict) else {}
        expected = {
            "query_id": expected_query_id,
            "selected_plan_id": entry.get("selected_plan_id"),
            "selected_plan_signature": selected_plan.get("plan_signature"),
            "result_digest": execution.get("result_digest"),
            "row_count": execution.get("row_count"),
            "stats_fingerprint": execution.get("stats_fingerprint"),
            "fallback_plan_id": fallback.get("plan_id"),
            "consistent": consistency.get("consistent"),
        }
        actual = {
            "query_id": replay.query.query_id,
            "selected_plan_id": replay.execution.selected.plan_id,
            "selected_plan_signature": replay.selected_plan.plan_signature,
            "result_digest": replay.execution.selected.digest,
            "row_count": replay.execution.selected.row_count,
            "stats_fingerprint": replay.execution.selected.stats_fingerprint,
            "fallback_plan_id": replay.execution.fallback.plan_id,
            "consistent": replay.execution.consistency.consistent,
        }
        for field, expected_value in expected.items():
            actual_value = actual[field]
            if expected_value != actual_value:
                differences.append(
                    {
                        "path": entry_path,
                        "field": field,
                        "query_id": expected_query_id,
                        "expected": expected_value,
                        "actual": actual_value,
                    }
                )

    if differences:
        raise SnapshotError(
            "snapshot replay differs from the recorded session",
            "$.history",
            differences=differences,
            version_comparison=version_comparison,
            mode="full_replace",
        )
    return RestoreResult(
        session=session,
        snapshot_path=str(target),
        mode="full_replace",
        version_comparison=version_comparison,
        replayed_queries=replayed,
    )
