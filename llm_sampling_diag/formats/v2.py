from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any

from ..canonical import canonical_json
from ..errors import DiagnosticError, ErrorCode
from ..jsonutil import (
    load_json_file,
    parse_float,
    parse_int,
    parse_logits,
    parse_shape,
    require_field,
    require_list,
    require_mapping,
)
from ..model import (
    CandidateMask,
    ChunkPlan,
    DEFAULT_REVISION,
    DEFAULT_REVISION_ID,
    ReadResult,
    UnifiedTask,
)
from .common import check_logits_shape, parse_flat_mask


def _task_line(lines: list[str], case_id: str, occurrence: int) -> int:
    needle = f'"{case_id}"'
    found = [index + 1 for index, line in enumerate(lines) if needle in line]
    if not found:
        return 1
    return found[min(occurrence, len(found) - 1)]


def _resolve_shard_path(root: Path, shard_path: str) -> Path:
    candidate = (root / shard_path).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            "shard path escapes the v2 dataset root",
            field="shard_path",
            details={"shard_path": shard_path},
        )
    return candidate


def _meta_value(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM dataset_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _load_metadata(meta_path: Path) -> tuple[sqlite3.Connection, list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        conn = sqlite3.connect(f"file:{meta_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("SELECT 1 FROM dataset_meta LIMIT 1").fetchone()
        conn.execute("SELECT 1 FROM shards LIMIT 1").fetchone()
        conn.execute("SELECT 1 FROM tasks LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            f"invalid v2 SQLite metadata: {exc}",
            field="meta.db",
            details={"path": str(meta_path)},
        ) from exc
    version = _meta_value(conn, "version")
    if version is None:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            "SQLite metadata is missing dataset version",
            field="dataset_meta.version",
            details={"path": str(meta_path)},
        )
    if str(version).lower().replace("v", "") not in {"2"}:
        raise DiagnosticError(
            ErrorCode.E_VERSION_UNSUPPORTED,
            f"unsupported v2 metadata version {version!r}",
            field="dataset_meta.version",
        )
    shards = [dict(row) for row in conn.execute("SELECT * FROM shards ORDER BY ordinal")]
    tasks = [
        dict(row)
        for row in conn.execute("SELECT rowid AS _rowid, * FROM tasks ORDER BY shard_id, row_index")
    ]
    return conn, shards, tasks


def _compare_metadata_value(
    *,
    field: str,
    meta_value: Any,
    json_value: Any,
    line: int,
    case_id: str,
    rowid: int | None,
) -> None:
    equal = meta_value == json_value
    if isinstance(meta_value, float) or isinstance(json_value, float):
        try:
            equal = math.isclose(float(meta_value), float(json_value), rel_tol=0.0, abs_tol=0.0)
        except (TypeError, ValueError):
            equal = False
    if not equal:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            f"SQLite metadata does not match shard JSON for {field}",
            field=field,
            line=line,
            case_id=case_id,
            details={"metadata_value": meta_value, "shard_json_value": json_value, "db_rowid": rowid},
        )


def read_v2(source: str | Path, *, meta_path: str | Path | None = None) -> ReadResult:
    source_path = Path(source)
    if source_path.is_dir():
        root = source_path
        metadata_path = Path(meta_path) if meta_path is not None else root / "meta.db"
    else:
        metadata_path = Path(meta_path) if meta_path is not None else source_path
        root = metadata_path.parent
    if not metadata_path.exists():
        raise DiagnosticError(
            ErrorCode.E_IO,
            "v2 SQLite metadata file does not exist",
            field="meta.db",
            details={"path": str(metadata_path)},
        )
    conn, shard_rows, meta_tasks = _load_metadata(metadata_path)
    tasks_by_case = {str(row["case_id"]): row for row in meta_tasks}
    expected_tasks = {str(row["case_id"]) for row in meta_tasks}
    observed_tasks: set[str] = set()
    tasks: list[UnifiedTask] = []
    records: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    previous_effective_ms: int | None = None
    try:
        for shard in shard_rows:
            shard_id = str(shard["shard_id"])
            shard_path_value = str(shard["shard_path"])
            shard_path = _resolve_shard_path(root, shard_path_value)
            if not shard_path.exists():
                raise DiagnosticError(
                    ErrorCode.E_IO,
                    "v2 shard file does not exist",
                    field="shard_path",
                    details={"shard_id": shard_id, "path": str(shard_path)},
                )
            document = load_json_file(shard_path)
            shard_root = document.data
            if not isinstance(shard_root, dict):
                raise DiagnosticError(
                    ErrorCode.E_FORMAT_CORRUPT,
                    "v2 shard root must be an object",
                    field="$",
                    line=1,
                    details={"shard_id": shard_id},
                )
            if "version" in shard_root and shard_root["version"] not in {2, "2", "v2"}:
                raise DiagnosticError(
                    ErrorCode.E_VERSION_UNSUPPORTED,
                    f"unsupported shard version {shard_root['version']!r}",
                    field="version",
                    line=document.key_line("version"),
                    details={"shard_id": shard_id},
                )
            if shard_root.get("shard_id") != shard_id:
                raise DiagnosticError(
                    ErrorCode.E_FORMAT_CORRUPT,
                    "shard header shard_id does not match SQLite metadata",
                    field="shard_id",
                    line=document.key_line("shard_id"),
                    details={"metadata_value": shard_id, "shard_json_value": shard_root.get("shard_id")},
                )
            shard_tasks = require_list(
                require_field(shard_root, "tasks", "tasks", 1),
                "tasks",
                document.key_line("tasks"),
            )
            if len(shard_tasks) != int(shard["task_count"]):
                raise DiagnosticError(
                    ErrorCode.E_FORMAT_CORRUPT,
                    "shard task_count does not match SQLite metadata",
                    field="task_count",
                    line=document.key_line("tasks"),
                    details={"metadata_value": int(shard["task_count"]), "shard_json_value": len(shard_tasks)},
                )
            for row_index, task_raw in enumerate(shard_tasks):
                task = require_mapping(
                    task_raw,
                    f"{shard_id}.tasks[{row_index}]",
                    document.key_line("tasks"),
                )
                case_raw = task.get("case_id")
                line = (
                    _task_line(document.lines, case_raw, row_index)
                    if isinstance(case_raw, str)
                    else document.key_line("tasks")
                )
                if not isinstance(case_raw, str) or not case_raw:
                    raise DiagnosticError(
                        ErrorCode.E_FORMAT_CORRUPT,
                        "case_id must be a non-empty string",
                        field="case_id",
                        line=line,
                        details={"shard_id": shard_id, "row_index": row_index},
                    )
                case_id = case_raw
                if case_id in seen:
                    raise DiagnosticError(
                        ErrorCode.E_DUP_CASE,
                        f"duplicate case_id {case_id!r}",
                        field="case_id",
                        line=line,
                        case_id=case_id,
                        details={"first_line": seen[case_id], "second_line": line},
                    )
                seen[case_id] = line
                if case_id not in tasks_by_case:
                    raise DiagnosticError(
                        ErrorCode.E_FORMAT_CORRUPT,
                        "shard task has no SQLite metadata row",
                        field="case_id",
                        line=line,
                        case_id=case_id,
                        details={"shard_id": shard_id, "row_index": row_index},
                    )
                meta = tasks_by_case[case_id]
                observed_tasks.add(case_id)
                if str(meta["shard_id"]) != shard_id or int(meta["row_index"]) != row_index:
                    raise DiagnosticError(
                        ErrorCode.E_FORMAT_CORRUPT,
                        "SQLite row location does not match shard position",
                        field="shard_id,row_index",
                        line=line,
                        case_id=case_id,
                        details={
                            "metadata_shard_id": meta["shard_id"],
                            "metadata_row_index": meta["row_index"],
                            "json_shard_id": shard_id,
                            "json_row_index": row_index,
                            "db_rowid": meta.get("_rowid"),
                        },
                    )
                effective_at_ms = parse_int(
                    require_field(task, "effective_at_ms", "effective_at_ms", line, case_id),
                    "effective_at_ms",
                    line,
                    case_id,
                )
                _compare_metadata_value(
                    field="effective_at_ms",
                    meta_value=int(meta["effective_at_ms"]),
                    json_value=effective_at_ms,
                    line=line,
                    case_id=case_id,
                    rowid=meta.get("_rowid"),
                )
                if previous_effective_ms is not None and effective_at_ms < previous_effective_ms:
                    raise DiagnosticError(
                        ErrorCode.E_FORMAT_CORRUPT,
                        "shard task timestamp regressed",
                        field="effective_at_ms",
                        line=line,
                        case_id=case_id,
                        details={"previous_effective_at_ms": previous_effective_ms, "current_effective_at_ms": effective_at_ms},
                    )
                previous_effective_ms = effective_at_ms
                logits = parse_logits(
                    require_field(task, "logits", "logits", line, case_id),
                    "logits",
                    line,
                    case_id,
                )
                declared_shape = (
                    parse_shape(task["logits_shape"], "logits_shape", line, case_id)
                    if "logits_shape" in task
                    else (len(logits),)
                )
                check_logits_shape(declared_shape, len(logits), field="logits_shape", line=line, case_id=case_id)
                k_mode = require_field(task, "k_mode", "k_mode", line, case_id)
                if k_mode not in {"top_k", "top_p"}:
                    raise DiagnosticError(
                        ErrorCode.E_FORMAT_CORRUPT,
                        "k_mode must be top_k or top_p",
                        field="k_mode",
                        line=line,
                        case_id=case_id,
                    )
                k_value = parse_float(require_field(task, "k_value", "k_value", line, case_id), "k_value", line, case_id)
                temperature = parse_float(
                    require_field(task, "temperature", "temperature", line, case_id),
                    "temperature",
                    line,
                    case_id,
                )
                extended = require_mapping(
                    require_field(task, "extended_sampling", "extended_sampling", line, case_id),
                    "extended_sampling",
                    line,
                    case_id,
                )
                if canonical_json(extended) != str(meta["extended_json"]):
                    raise DiagnosticError(
                        ErrorCode.E_FORMAT_CORRUPT,
                        "SQLite extended_json does not match shard JSON",
                        field="extended_sampling",
                        line=line,
                        case_id=case_id,
                        details={"metadata_json": meta["extended_json"], "shard_json": extended},
                    )
                if "tie_policy" not in extended or extended["tie_policy"] in {None, ""}:
                    raise DiagnosticError(
                        ErrorCode.E_TIE_POLICY_UNSPECIFIED,
                        "extended_sampling.tie_policy is not declared",
                        field="extended_sampling.tie_policy",
                        line=line,
                        case_id=case_id,
                    )
                tie_policy = extended["tie_policy"]
                if not isinstance(tie_policy, str):
                    raise DiagnosticError(
                        ErrorCode.E_FORMAT_CORRUPT,
                        "tie_policy must be a string",
                        field="extended_sampling.tie_policy",
                        line=line,
                        case_id=case_id,
                    )
                if "seed" not in extended or extended["seed"] is None:
                    raise DiagnosticError(
                        ErrorCode.E_SEED_UNSPECIFIED,
                        "extended_sampling.seed is not declared",
                        field="extended_sampling.seed",
                        line=line,
                        case_id=case_id,
                    )
                seed = parse_int(extended["seed"], "extended_sampling.seed", line, case_id, minimum=0)
                num_samples = parse_int(
                    extended.get("num_samples", 1),
                    "extended_sampling.num_samples",
                    line,
                    case_id,
                    minimum=1,
                )
                top_p = (
                    parse_float(extended["top_p"], "extended_sampling.top_p", line, case_id)
                    if extended.get("top_p") is not None
                    else None
                )
                chunk = require_mapping(
                    require_field(task, "chunk_plan", "chunk_plan", line, case_id),
                    "chunk_plan",
                    line,
                    case_id,
                )
                chunk_size = parse_int(
                    require_field(chunk, "chunk_size", "chunk_plan.chunk_size", line, case_id),
                    "chunk_plan.chunk_size",
                    line,
                    case_id,
                    minimum=1,
                )
                chunk_count = parse_int(
                    require_field(chunk, "chunk_count", "chunk_plan.chunk_count", line, case_id),
                    "chunk_plan.chunk_count",
                    line,
                    case_id,
                    minimum=1,
                )
                _compare_metadata_value(
                    field="chunk_plan.chunk_size",
                    meta_value=int(meta["chunk_size"]),
                    json_value=chunk_size,
                    line=line,
                    case_id=case_id,
                    rowid=meta.get("_rowid"),
                )
                _compare_metadata_value(
                    field="chunk_plan.chunk_count",
                    meta_value=int(meta["chunk_count"]),
                    json_value=chunk_count,
                    line=line,
                    case_id=case_id,
                    rowid=meta.get("_rowid"),
                )
                error_budget = parse_float(
                    require_field(task, "error_budget", "error_budget", line, case_id),
                    "error_budget",
                    line,
                    case_id,
                )
                _compare_metadata_value(
                    field="error_budget",
                    meta_value=float(meta["error_budget"]),
                    json_value=error_budget,
                    line=line,
                    case_id=case_id,
                    rowid=meta.get("_rowid"),
                )
                mask = parse_flat_mask(
                    require_field(task, "candidate_mask_flat", "candidate_mask_flat", line, case_id),
                    field="candidate_mask_flat",
                    line=line,
                    case_id=case_id,
                )
                if canonical_json(list(mask)) != str(meta["mask_json"]):
                    raise DiagnosticError(
                        ErrorCode.E_FORMAT_CORRUPT,
                        "SQLite mask_json does not match shard JSON",
                        field="candidate_mask_flat",
                        line=line,
                        case_id=case_id,
                        details={"metadata_json": meta["mask_json"], "shard_json": list(mask)},
                    )
                actions = [
                    {
                        "field": "candidate_mask",
                        "action": "preserve_flat",
                        "source": "v2_explicit",
                        "value": list(mask),
                    },
                    {
                        "field": "revision",
                        "action": "default",
                        "source": "v2_has_no_revision_field",
                        "value": DEFAULT_REVISION,
                    },
                ]
                unified = UnifiedTask(
                    case_id=case_id,
                    effective_at_ms=effective_at_ms,
                    logits=logits,
                    logits_shape=declared_shape,
                    k_mode=k_mode,
                    k_value=k_value,
                    temperature=temperature,
                    tie_policy=tie_policy,
                    seed=seed,
                    candidate_mask=CandidateMask(
                        excluded_indices=mask,
                        selectors=(),
                        mask_version=1,
                        source="v2_flat",
                    ),
                    error_budget=error_budget,
                    revision=DEFAULT_REVISION,
                    revision_id=DEFAULT_REVISION_ID,
                    chunk_plan=ChunkPlan(chunk_size=chunk_size, chunk_count=chunk_count),
                    num_samples=num_samples,
                    top_p=top_p,
                    source_format="v2",
                    source_ref=f"{shard_path}:{line}",
                    migration_actions=tuple(actions),
                )
                tasks.append(unified)
                records.append(
                    {
                        "case_id": case_id,
                        "from_format": "v2",
                        "to_format": "unified",
                        "verified_metadata": True,
                        "upgrade_defaults": actions,
                    }
                )
    finally:
        conn.close()
    missing = expected_tasks - observed_tasks
    if missing:
        missing_case = sorted(missing)[0]
        row = tasks_by_case[missing_case]
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            "SQLite metadata row has no matching shard task",
            field="case_id",
            line=int(row["row_index"]) + 1,
            case_id=missing_case,
            details={"missing_case_ids": sorted(missing), "db_rowid": row.get("_rowid")},
        )
    return ReadResult(tasks=tasks, records=records)


def write_v2_bundle(
    tasks: list[UnifiedTask],
    root: str | Path,
    *,
    shard_size: int = 2,
) -> Path:
    if shard_size <= 0:
        raise ValueError("shard_size must be positive")
    root_path = Path(root)
    shards_dir = root_path / "shards"
    shards_dir.mkdir(parents=True, exist_ok=True)
    meta_path = root_path / "meta.db"
    if meta_path.exists():
        meta_path.unlink()
    conn = sqlite3.connect(meta_path)
    try:
        conn.executescript(
            """
            CREATE TABLE dataset_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE shards (
                ordinal INTEGER PRIMARY KEY,
                shard_id TEXT UNIQUE NOT NULL,
                shard_path TEXT NOT NULL,
                task_count INTEGER NOT NULL
            );
            CREATE TABLE tasks (
                case_id TEXT PRIMARY KEY,
                shard_id TEXT NOT NULL,
                row_index INTEGER NOT NULL,
                effective_at_ms INTEGER NOT NULL,
                chunk_size INTEGER NOT NULL,
                chunk_count INTEGER NOT NULL,
                error_budget REAL NOT NULL,
                mask_json TEXT NOT NULL,
                extended_json TEXT NOT NULL,
                UNIQUE(shard_id, row_index)
            );
            """
        )
        conn.execute("INSERT INTO dataset_meta(key, value) VALUES ('format', 'task-v2-meta')")
        conn.execute("INSERT INTO dataset_meta(key, value) VALUES ('version', '2')")
        shard_count = (len(tasks) + shard_size - 1) // shard_size
        for shard_index in range(shard_count):
            shard_id = f"shard-{shard_index:03d}"
            shard_tasks = tasks[shard_index * shard_size : (shard_index + 1) * shard_size]
            relative_path = f"shards/{shard_id}.json"
            document = {
                "format": "task-v2-shard",
                "version": 2,
                "shard_id": shard_id,
                "tasks": [_task_to_v2_json(task) for task in shard_tasks],
            }
            (root_path / relative_path).write_text(
                json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            conn.execute(
                "INSERT INTO shards(ordinal, shard_id, shard_path, task_count) VALUES (?, ?, ?, ?)",
                (shard_index, shard_id, relative_path, len(shard_tasks)),
            )
            for row_index, task in enumerate(shard_tasks):
                extended = _extended_sampling(task)
                conn.execute(
                    """
                    INSERT INTO tasks(
                        case_id, shard_id, row_index, effective_at_ms,
                        chunk_size, chunk_count, error_budget, mask_json, extended_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.case_id,
                        shard_id,
                        row_index,
                        task.effective_at_ms,
                        task.chunk_plan.chunk_size,
                        task.chunk_plan.chunk_count,
                        task.error_budget,
                        canonical_json(list(task.candidate_mask.excluded_indices)),
                        canonical_json(extended),
                    ),
                )
        conn.commit()
    finally:
        conn.close()
    return meta_path


def _extended_sampling(task: UnifiedTask) -> dict[str, Any]:
    return {
        "tie_policy": task.tie_policy,
        "seed": task.seed,
        "num_samples": task.num_samples,
        "top_p": task.top_p,
    }


def _task_to_v2_json(task: UnifiedTask) -> dict[str, Any]:
    return {
        "case_id": task.case_id,
        "effective_at_ms": task.effective_at_ms,
        "logits": list(task.logits),
        "logits_shape": list(task.logits_shape),
        "k_mode": task.k_mode,
        "k_value": task.k_value,
        "temperature": task.temperature,
        "extended_sampling": _extended_sampling(task),
        "candidate_mask_flat": list(task.candidate_mask.excluded_indices),
        "error_budget": task.error_budget,
        "chunk_plan": task.chunk_plan.to_dict(),
    }


def task_to_v2_json(task: UnifiedTask) -> dict[str, Any]:
    return _task_to_v2_json(task)


def extended_sampling(task: UnifiedTask) -> dict[str, Any]:
    return _extended_sampling(task)

