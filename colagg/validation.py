from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from .canonical import (
    canonical_dumps,
    normalize_float_cell,
    sha256_bytes,
    strict_json_loads,
)
from .errors import (
    E_BAD_BATCH,
    E_BAD_CONFIG,
    E_BAD_MANIFEST,
    E_BAD_QUERY,
    E_DIGEST_MISMATCH,
    E_IO,
    EngineError,
    fail,
)
from .models import (
    AGGREGATE_OPS,
    INJECTION_KINDS,
    NUMERIC_TYPES,
    PHASES,
    SCALAR_TYPES,
    Aggregate,
    Batch,
    Column,
    ExecutionConfig,
    Injection,
    InjectionManifest,
    Plan,
    Query,
    WindowSpec,
    parse_group_key,
)


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def _read_json(path: str | Path, code: str) -> Any:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise EngineError(code, f"cannot read {path}: {exc}", path=str(path)) from exc
    try:
        return strict_json_loads(text)
    except ValueError as exc:
        raise EngineError(code, f"invalid JSON in {path}: {exc}", path=str(path)) from exc


def load_query(path: str | Path) -> Query:
    return parse_query(_read_json(path, E_BAD_QUERY))


def parse_query(raw: Any) -> Query:
    if not isinstance(raw, dict):
        fail(E_BAD_QUERY, "query must be a JSON object")
    allowed = {"query_id", "group_by", "aggregates", "window"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        fail(E_BAD_QUERY, "query has unknown fields", fields=unknown)

    query_id = raw.get("query_id")
    if not isinstance(query_id, str) or not query_id:
        fail(E_BAD_QUERY, "query_id must be a non-empty string")

    group_raw = raw.get("group_by")
    if not isinstance(group_raw, list) or not group_raw:
        fail(E_BAD_QUERY, "group_by must be a non-empty array")
    group_by: list[str] = []
    for item in group_raw:
        if not isinstance(item, str) or not item:
            fail(E_BAD_QUERY, "group_by entries must be non-empty strings")
        if item in group_by:
            fail(E_BAD_QUERY, "group_by contains duplicate columns", column=item)
        group_by.append(item)

    aggregates_raw = raw.get("aggregates")
    if not isinstance(aggregates_raw, list) or not aggregates_raw:
        fail(E_BAD_QUERY, "aggregates must be a non-empty array")
    aggregates: list[Aggregate] = []
    names: set[str] = set()
    for index, item in enumerate(aggregates_raw):
        if not isinstance(item, dict):
            fail(E_BAD_QUERY, "aggregate entries must be objects", index=index)
        allowed_agg = {"name", "op", "column"}
        unknown_agg = sorted(set(item) - allowed_agg)
        if unknown_agg:
            fail(
                E_BAD_QUERY,
                "aggregate has unknown fields",
                index=index,
                fields=unknown_agg,
            )
        name = item.get("name")
        op = item.get("op")
        column = item.get("column")
        if not isinstance(name, str) or not name:
            fail(E_BAD_QUERY, "aggregate name must be a non-empty string", index=index)
        if name in names:
            fail(E_BAD_QUERY, "aggregate names must be unique", aggregate=name)
        if op not in AGGREGATE_OPS:
            fail(E_BAD_QUERY, "unsupported aggregate operation", aggregate=name, op=op)
        if not isinstance(column, str) or not column:
            fail(
                E_BAD_QUERY,
                "aggregate column must be a non-empty string",
                aggregate=name,
            )
        names.add(name)
        aggregates.append(Aggregate(name=name, op=op, column=column))

    window_raw = raw.get("window")
    if not isinstance(window_raw, dict):
        fail(E_BAD_QUERY, "window must be an object")
    allowed_window = {"length", "stride", "column", "outputs"}
    unknown_window = sorted(set(window_raw) - allowed_window)
    if unknown_window:
        fail(E_BAD_QUERY, "window has unknown fields", fields=unknown_window)
    length = window_raw.get("length")
    stride = window_raw.get("stride")
    column = window_raw.get("column")
    if not _positive_int(length):
        fail(E_BAD_QUERY, "window.length must be a positive integer")
    if not _positive_int(stride):
        fail(E_BAD_QUERY, "window.stride must be a positive integer")
    if stride > length:
        fail(E_BAD_QUERY, "window.stride must not exceed window.length")
    if not isinstance(column, str) or not column:
        fail(E_BAD_QUERY, "window.column must be a non-empty string")
    outputs_raw = window_raw.get("outputs", ["count", "sum", "mean", "variance"])
    if not isinstance(outputs_raw, list) or not outputs_raw:
        fail(E_BAD_QUERY, "window.outputs must be a non-empty array")
    outputs: list[str] = []
    for item in outputs_raw:
        if item not in {"count", "sum", "mean", "variance"}:
            fail(E_BAD_QUERY, "unsupported window output", output=item)
        if item in outputs:
            fail(E_BAD_QUERY, "window.outputs contains duplicates", output=item)
        outputs.append(item)

    return Query(
        query_id=query_id,
        group_by=tuple(group_by),
        aggregates=tuple(aggregates),
        window=WindowSpec(
            length=length,
            stride=stride,
            column=column,
            outputs=tuple(outputs),
        ),
    )


def load_config(
    path: str | Path,
    numeric_mode_override: str | None = None,
) -> ExecutionConfig:
    raw = _read_json(path, E_BAD_CONFIG)
    if numeric_mode_override is not None:
        if not isinstance(raw, dict):
            fail(E_BAD_CONFIG, "config must be a JSON object")
        raw = dict(raw)
        raw["numeric_mode"] = numeric_mode_override
    return parse_config(raw)


def parse_config(raw: Any) -> ExecutionConfig:
    if not isinstance(raw, dict):
        fail(E_BAD_CONFIG, "config must be a JSON object")
    required = {
        "memory_budget_mib",
        "max_batch_rows",
        "spill_dir",
        "fixed_scale",
        "fixed_int_bits",
        "float_tolerance",
        "logical_clock_step",
        "numeric_mode",
    }
    missing = sorted(required - set(raw))
    unknown = sorted(set(raw) - required)
    if missing:
        fail(E_BAD_CONFIG, "config is missing required fields", fields=missing)
    if unknown:
        fail(E_BAD_CONFIG, "config has unknown fields", fields=unknown)

    memory_budget_mib = raw["memory_budget_mib"]
    max_batch_rows = raw["max_batch_rows"]
    fixed_scale = raw["fixed_scale"]
    fixed_int_bits = raw["fixed_int_bits"]
    logical_clock_step = raw["logical_clock_step"]
    if not _positive_int(memory_budget_mib):
        fail(E_BAD_CONFIG, "memory_budget_mib must be a positive integer")
    if not _positive_int(max_batch_rows):
        fail(E_BAD_CONFIG, "max_batch_rows must be a positive integer")
    if not _positive_int(fixed_scale):
        fail(E_BAD_CONFIG, "fixed_scale must be a positive integer")
    if not _positive_int(fixed_int_bits) or not 2 <= fixed_int_bits <= 64:
        fail(E_BAD_CONFIG, "fixed_int_bits must be an integer in [2, 64]")
    if not _positive_int(logical_clock_step):
        fail(E_BAD_CONFIG, "logical_clock_step must be a positive integer")

    spill_dir = raw["spill_dir"]
    if not isinstance(spill_dir, str) or not spill_dir:
        fail(E_BAD_CONFIG, "spill_dir must be a non-empty relative path")
    spill_path = Path(spill_dir)
    if spill_path.is_absolute() or ".." in spill_path.parts:
        fail(E_BAD_CONFIG, "spill_dir must stay within the workspace root")

    tolerance = raw["float_tolerance"]
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)):
        fail(E_BAD_CONFIG, "float_tolerance must be a non-negative finite number")
    tolerance = float(tolerance)
    if not math.isfinite(tolerance) or tolerance < 0:
        fail(E_BAD_CONFIG, "float_tolerance must be a non-negative finite number")

    numeric_mode = raw["numeric_mode"]
    if numeric_mode not in {"float", "fixed"}:
        fail(E_BAD_CONFIG, "numeric_mode must be 'float' or 'fixed'")

    return ExecutionConfig(
        memory_budget_mib=memory_budget_mib,
        max_batch_rows=max_batch_rows,
        spill_dir=spill_dir,
        fixed_scale=fixed_scale,
        fixed_int_bits=fixed_int_bits,
        float_tolerance=tolerance,
        logical_clock_step=logical_clock_step,
        numeric_mode=numeric_mode,
    )


def load_manifest(path: str | Path | None) -> InjectionManifest:
    if path is None:
        return InjectionManifest()
    return parse_manifest(_read_json(path, E_BAD_MANIFEST))


def parse_manifest(raw: Any) -> InjectionManifest:
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        unknown = sorted(set(raw) - {"injections"})
        if unknown:
            fail(E_BAD_MANIFEST, "manifest has unknown fields", fields=unknown)
        items = raw.get("injections", [])
    else:
        fail(E_BAD_MANIFEST, "manifest must be an object or an array")
    if not isinstance(items, list):
        fail(E_BAD_MANIFEST, "manifest.injections must be an array")

    injections: list[Injection] = []
    ids: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            fail(E_BAD_MANIFEST, "injection entries must be objects", index=index)
        allowed = {"id", "phase", "batch_seq", "kind", "column", "group", "target"}
        unknown = sorted(set(item) - allowed)
        if unknown:
            fail(
                E_BAD_MANIFEST,
                "injection has unknown fields",
                index=index,
                fields=unknown,
            )
        injection_id = item.get("id")
        phase = item.get("phase")
        batch_seq = item.get("batch_seq")
        kind = item.get("kind")
        if not isinstance(injection_id, str) or not injection_id:
            fail(E_BAD_MANIFEST, "injection id must be a non-empty string", index=index)
        if injection_id in ids:
            fail(E_BAD_MANIFEST, "injection ids must be unique", id=injection_id)
        ids.add(injection_id)
        if phase not in PHASES:
            fail(E_BAD_MANIFEST, "invalid injection phase", id=injection_id, phase=phase)
        if not _positive_int(batch_seq):
            fail(E_BAD_MANIFEST, "injection batch_seq must be positive", id=injection_id)
        if kind not in INJECTION_KINDS:
            fail(E_BAD_MANIFEST, "invalid injection kind", id=injection_id, kind=kind)

        column = item.get("column")
        group = parse_group_key(item.get("group"))
        target = item.get("target")
        if column is not None and (not isinstance(column, str) or not column):
            fail(E_BAD_MANIFEST, "injection column must be a string", id=injection_id)
        if item.get("group") is not None and group is None:
            fail(E_BAD_MANIFEST, "injection group must be an array of scalars", id=injection_id)
        if target is not None and (not isinstance(target, str) or not target):
            fail(E_BAD_MANIFEST, "injection target must be a string", id=injection_id)

        _validate_injection_shape(
            injection_id,
            phase,
            kind,
            column,
            group,
        )
        injections.append(
            Injection(
                injection_id=injection_id,
                phase=phase,
                batch_seq=batch_seq,
                kind=kind,
                column=column,
                group=group,
                target=target,
            )
        )
    return InjectionManifest(tuple(injections))


def _validate_injection_shape(
    injection_id: str,
    phase: str,
    kind: str,
    column: str | None,
    group: tuple[Any, ...] | None,
) -> None:
    expected_phase = {
        "malformed_column": "read",
        "digest_mismatch": "read",
        "fixed_overflow": "aggregate",
        "window_corrupt": "window",
        "spill_tamper": "write",
        "spill_readback_corrupt": "read",
        "crash_after_spill": "write",
        "crash_before_commit": "write",
    }[kind]
    if phase != expected_phase:
        fail(
            E_BAD_MANIFEST,
            "injection kind is not valid for this phase",
            id=injection_id,
            kind=kind,
            expected_phase=expected_phase,
            phase=phase,
        )
    if kind == "malformed_column" and not column:
        fail(E_BAD_MANIFEST, "malformed_column requires a column", id=injection_id)
    if kind in {"fixed_overflow", "window_corrupt"} and group is None:
        fail(E_BAD_MANIFEST, f"{kind} requires a group", id=injection_id)


def load_batches(
    paths: Sequence[str | Path],
    config: ExecutionConfig,
) -> tuple[Batch, ...]:
    if not paths:
        fail(E_BAD_BATCH, "at least one batch file is required")
    batches: list[Batch] = []
    previous_seq = 0
    for path in paths:
        raw = _read_json(path, E_BAD_BATCH)
        batch = parse_batch(raw, config, source=str(path))
        if batch.batch_seq <= previous_seq:
            fail(
                E_BAD_BATCH,
                "batch_seq must be strictly increasing in input order",
                batch_seq=batch.batch_seq,
                previous_batch_seq=previous_seq,
            )
        previous_seq = batch.batch_seq
        batches.append(batch)
    return tuple(batches)


def parse_batch(
    raw: Any,
    config: ExecutionConfig,
    *,
    source: str | None = None,
    expected_seq: int | None = None,
) -> Batch:
    if not isinstance(raw, dict):
        fail(E_BAD_BATCH, "batch must be a JSON object", source=source)
    required = {"batch_seq", "row_count", "columns", "batch_digest"}
    missing = sorted(required - set(raw))
    unknown = sorted(set(raw) - required)
    if missing:
        fail(E_BAD_BATCH, "batch is missing required fields", fields=missing, source=source)
    if unknown:
        fail(E_BAD_BATCH, "batch has unknown fields", fields=unknown, source=source)

    batch_seq = raw["batch_seq"]
    row_count = raw["row_count"]
    columns_raw = raw["columns"]
    digest = raw["batch_digest"]
    if not _positive_int(batch_seq):
        fail(E_BAD_BATCH, "batch_seq must be a positive integer", source=source)
    if expected_seq is not None and batch_seq != expected_seq:
        fail(
            E_BAD_BATCH,
            "stored batch sequence does not match its key",
            batch_seq=batch_seq,
            expected_batch_seq=expected_seq,
        )
    if not _nonnegative_int(row_count):
        fail(E_BAD_BATCH, "row_count must be a non-negative integer", batch_seq=batch_seq)
    if row_count > config.max_batch_rows:
        fail(
            E_BAD_BATCH,
            "row_count exceeds max_batch_rows",
            batch_seq=batch_seq,
            row_count=row_count,
            max_batch_rows=config.max_batch_rows,
        )
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        fail(E_BAD_BATCH, "batch_digest must be a lowercase SHA-256 hex string", batch_seq=batch_seq)
    if not isinstance(columns_raw, list) or not columns_raw:
        fail(E_BAD_BATCH, "columns must be a non-empty array", batch_seq=batch_seq)

    columns: list[Column] = []
    seen: set[str] = set()
    for index, column_raw in enumerate(columns_raw):
        if not isinstance(column_raw, dict):
            fail(E_BAD_BATCH, "column entries must be objects", batch_seq=batch_seq, index=index)
        unknown_col = sorted(set(column_raw) - {"name", "type", "values"})
        if unknown_col:
            fail(
                E_BAD_BATCH,
                "column has unknown fields",
                batch_seq=batch_seq,
                index=index,
                fields=unknown_col,
            )
        name = column_raw.get("name")
        col_type = column_raw.get("type")
        values = column_raw.get("values")
        if not isinstance(name, str) or not name:
            fail(E_BAD_BATCH, "column name must be a non-empty string", batch_seq=batch_seq)
        if name in seen:
            fail(E_BAD_BATCH, "column names must be unique", batch_seq=batch_seq, column=name)
        seen.add(name)
        if col_type not in SCALAR_TYPES:
            fail(
                E_BAD_BATCH,
                "unsupported column type",
                batch_seq=batch_seq,
                column=name,
                column_type=col_type,
            )
        if not isinstance(values, list):
            fail(E_BAD_BATCH, "column values must be an array", batch_seq=batch_seq, column=name)
        if len(values) != row_count:
            fail(
                E_BAD_BATCH,
                "column length must equal row_count",
                batch_seq=batch_seq,
                column=name,
                expected=row_count,
                actual=len(values),
            )
        normalized = tuple(
            _normalize_cell(value, col_type, batch_seq, name, row_index)
            for row_index, value in enumerate(values)
        )
        columns.append(Column(name=name, type=col_type, values=normalized))

    payload = {
        "batch_seq": batch_seq,
        "row_count": row_count,
        "columns": [column.as_payload() for column in columns],
        "batch_digest": digest,
    }
    computed = sha256_bytes(
        canonical_dumps(
            {
                "batch_seq": batch_seq,
                "row_count": row_count,
                "columns": [column.as_payload() for column in columns],
            }
        ).encode("utf-8")
    )
    if computed != digest:
        fail(
            E_DIGEST_MISMATCH,
            "batch content digest does not match columns",
            batch_seq=batch_seq,
            expected_digest=computed,
            actual_digest=digest,
        )

    return Batch(
        batch_seq=batch_seq,
        row_count=row_count,
        columns=tuple(columns),
        digest=digest,
        payload=payload,
    )


def validate_plan_inputs(
    query: Query,
    config: ExecutionConfig,
    manifest: InjectionManifest,
    batches: Sequence[Batch],
) -> Plan:
    if not batches:
        fail(E_BAD_BATCH, "at least one batch file is required")

    type_by_column: dict[str, str] = {}
    for batch in batches:
        for column in batch.columns:
            prior = type_by_column.setdefault(column.name, column.type)
            if prior != column.type:
                fail(
                    E_BAD_BATCH,
                    "column type changed between batches",
                    column=column.name,
                    batch_seq=batch.batch_seq,
                    expected_type=prior,
                    actual_type=column.type,
                )

    for name in query.group_by:
        if name not in type_by_column:
            fail(E_BAD_QUERY, "group_by references a missing column", column=name)

    aggregate_columns: set[str] = set()
    for aggregate in query.aggregates:
        if aggregate.column not in type_by_column:
            fail(
                E_BAD_QUERY,
                "aggregate references a missing column",
                aggregate=aggregate.name,
                column=aggregate.column,
            )
        if aggregate.op != "count":
            col_type = type_by_column[aggregate.column]
            if col_type not in NUMERIC_TYPES:
                fail(
                    E_BAD_QUERY,
                    "numeric aggregate requires an int or float column",
                    aggregate=aggregate.name,
                    column=aggregate.column,
                    column_type=col_type,
                )
            aggregate_columns.add(aggregate.column)

    if query.window.column not in type_by_column:
        fail(E_BAD_QUERY, "window references a missing column", column=query.window.column)
    if type_by_column[query.window.column] not in NUMERIC_TYPES:
        fail(
            E_BAD_QUERY,
            "window requires an int or float column",
            column=query.window.column,
            column_type=type_by_column[query.window.column],
        )
    if not aggregate_columns and all(item.op == "count" for item in query.aggregates):
        # Count-only queries remain valid; they use the group row counter.
        pass

    batch_seqs = {batch.batch_seq for batch in batches}
    for injection in manifest.injections:
        if injection.batch_seq not in batch_seqs:
            fail(
                E_BAD_MANIFEST,
                "injection references a batch that is not loaded",
                id=injection.injection_id,
                batch_seq=injection.batch_seq,
            )
        if injection.kind == "malformed_column":
            batch = next(item for item in batches if item.batch_seq == injection.batch_seq)
            if injection.column not in batch.by_name:
                fail(
                    E_BAD_MANIFEST,
                    "malformed_column references a missing column",
                    id=injection.injection_id,
                    batch_seq=injection.batch_seq,
                    column=injection.column,
                )
        if injection.group is not None:
            if len(injection.group) != len(query.group_by):
                fail(
                    E_BAD_MANIFEST,
                    "injection group length does not match group_by",
                    id=injection.injection_id,
                    expected=len(query.group_by),
                    actual=len(injection.group),
                )

    return Plan.build(query, config, manifest)


def _normalize_cell(
    value: Any,
    col_type: str,
    batch_seq: int,
    column: str,
    row_index: int,
) -> Any:
    if col_type == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            fail(
                E_BAD_BATCH,
                "int column contains a non-integer value",
                batch_seq=batch_seq,
                column=column,
                row_index=row_index,
            )
        return value
    if col_type == "float":
        try:
            return normalize_float_cell(value)
        except TypeError as exc:
            fail(
                E_BAD_BATCH,
                str(exc),
                batch_seq=batch_seq,
                column=column,
                row_index=row_index,
            )
    if col_type == "bool":
        if not isinstance(value, bool):
            fail(
                E_BAD_BATCH,
                "bool column contains a non-boolean value",
                batch_seq=batch_seq,
                column=column,
                row_index=row_index,
            )
        return value
    if col_type == "string":
        if not isinstance(value, str):
            fail(
                E_BAD_BATCH,
                "string column contains a non-string value",
                batch_seq=batch_seq,
                column=column,
                row_index=row_index,
            )
        return value
    fail(E_BAD_BATCH, "unsupported column type", column=column)


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def discover_batch_files(directory: str | Path) -> list[Path]:
    root = Path(directory)
    if not root.is_dir():
        fail(E_IO, "batch directory does not exist", directory=str(root))
    return sorted(root.glob("*.json"))
