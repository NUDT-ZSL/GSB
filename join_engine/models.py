from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_json, sha256_value
from .errors import fail


SUPPORTED_TYPES = {"int", "string"}
JOIN_TYPES = {"inner", "left", "right"}
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    data_type: str
    ndv: int
    width_bytes: int
    null_fraction: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.data_type,
            "ndv": self.ndv,
            "width_bytes": self.width_bytes,
            "null_fraction": self.null_fraction,
        }


@dataclass(frozen=True)
class SkewSpec:
    key: Any
    share: float

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "share": self.share}


@dataclass(frozen=True)
class TableSpec:
    name: str
    rows: int
    join_key: str
    columns: tuple[ColumnSpec, ...]
    skew: SkewSpec | None = None

    @property
    def column_map(self) -> dict[str, ColumnSpec]:
        return {column.name: column for column in self.columns}

    @property
    def row_width_bytes(self) -> int:
        return sum(column.width_bytes for column in self.columns)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "rows": self.rows,
            "join_key": self.join_key,
            "columns": [column.to_dict() for column in self.columns],
            "skew": None,
        }
        if self.skew is not None:
            payload["skew"] = self.skew.to_dict()
        return payload


@dataclass(frozen=True)
class QuerySpec:
    query_id: str
    left_table: str
    right_table: str
    join_type: str
    left_key: str
    right_key: str
    filter_selectivity: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.query_id,
            "left_table": self.left_table,
            "right_table": self.right_table,
            "join_type": self.join_type,
            "predicate": {"left_key": self.left_key, "right_key": self.right_key},
            "filter_selectivity": self.filter_selectivity,
        }


@dataclass(frozen=True)
class ScaleSample:
    name: str
    left_rows: int
    right_rows: int

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "left_rows": self.left_rows, "right_rows": self.right_rows}


@dataclass(frozen=True)
class ExecutionConfig:
    partitions: int
    memory_limit_mib: float
    chunk_rows: int
    skew_threshold: float
    logical_clock_step: float
    tolerance_relative: float
    tolerance_absolute: float
    broadcast_fraction: float
    hash_fraction: float
    scale_samples: tuple[ScaleSample, ...]
    default_scale: str

    @property
    def memory_limit_bytes(self) -> int:
        return int(self.memory_limit_mib * 1024 * 1024)

    def to_dict(self) -> dict[str, Any]:
        return {
            "partitions": self.partitions,
            "memory_limit_mib": self.memory_limit_mib,
            "chunk_rows": self.chunk_rows,
            "skew_threshold": self.skew_threshold,
            "logical_clock_step": self.logical_clock_step,
            "tolerance": {
                "relative": self.tolerance_relative,
                "absolute": self.tolerance_absolute,
            },
            "broadcast_fraction": self.broadcast_fraction,
            "hash_fraction": self.hash_fraction,
            "default_scale": self.default_scale,
            "scale_samples": [scale.to_dict() for scale in self.scale_samples],
        }


@dataclass(frozen=True)
class SampleConfig:
    seed: int
    tables: tuple[TableSpec, ...]
    query: QuerySpec
    execution: ExecutionConfig
    source_path: str = ""

    @property
    def table_map(self) -> dict[str, TableSpec]:
        return {table.name: table for table in self.tables}

    def scale(self, name: str | None = None) -> ScaleSample:
        selected_name = name or self.execution.default_scale
        for scale in self.execution.scale_samples:
            if scale.name == selected_name:
                return scale
        fail(
            "E_BAD_SCALE",
            f"unknown scale sample: {selected_name}",
            details={"available": [scale.name for scale in self.execution.scale_samples]},
        )

    def row_counts(self, scale_name: str | None = None) -> dict[str, int]:
        scale = self.scale(scale_name)
        return {
            self.query.left_table: scale.left_rows,
            self.query.right_table: scale.right_rows,
        }

    def fingerprint_payload(self, scale_name: str | None = None) -> dict[str, Any]:
        scale = self.scale(scale_name)
        tables: list[dict[str, Any]] = []
        for table in self.tables:
            item = table.to_dict()
            if table.name == self.query.left_table:
                item["rows"] = scale.left_rows
            elif table.name == self.query.right_table:
                item["rows"] = scale.right_rows
            tables.append(item)
        return {
            "format": "local-shuffle-join-v1",
            "seed": self.seed,
            "tables": tables,
            "query": self.query.to_dict(),
            "execution": self.execution.to_dict(),
            "scale": scale.to_dict(),
        }

    def fingerprint(self, scale_name: str | None = None) -> str:
        return sha256_value(self.fingerprint_payload(scale_name))


def _parse_column(raw: Any, table_name: str) -> ColumnSpec:
    if not isinstance(raw, dict):
        fail("E_BAD_TABLE", "column specification must be an object", details={"table": table_name})
    name = raw.get("name")
    if not isinstance(name, str) or not name or name == "row_id":
        fail(
            "E_BAD_TABLE",
            "column name must be a non-empty string and cannot be row_id",
            details={"table": table_name, "column": name},
        )
    data_type = raw.get("type")
    if data_type not in SUPPORTED_TYPES:
        fail(
            "E_BAD_TABLE",
            f"unsupported column type: {data_type!r}",
            details={"table": table_name, "column": name, "supported": sorted(SUPPORTED_TYPES)},
        )
    ndv = raw.get("ndv")
    if not _is_positive_int(ndv):
        fail(
            "E_BAD_NDV",
            "column NDV must be a positive integer",
            details={"table": table_name, "column": name, "ndv": ndv},
        )
    width = raw.get("width_bytes")
    if not _is_positive_int(width) or width > 1024 * 1024:
        fail(
            "E_BAD_COLUMN_WIDTH",
            "column width_bytes must be in [1, 1048576]",
            details={"table": table_name, "column": name, "width_bytes": width},
        )
    null_fraction = raw.get("null_fraction", 0.0)
    if not _is_number(null_fraction) or not 0.0 <= float(null_fraction) < 1.0:
        fail(
            "E_BAD_TABLE",
            "column null_fraction must be in [0, 1)",
            details={"table": table_name, "column": name, "null_fraction": null_fraction},
        )
    return ColumnSpec(name, data_type, ndv, width, float(null_fraction))


def _parse_table(raw: Any) -> TableSpec:
    if not isinstance(raw, dict):
        fail("E_BAD_TABLE", "table specification must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        fail("E_BAD_TABLE", "table name must be a non-empty string", details={"table": name})
    if not SAFE_NAME_RE.fullmatch(name):
        fail(
            "E_BAD_TABLE",
            "table name may contain only letters, digits, dot, underscore and hyphen",
            details={"table": name},
        )
    rows = raw.get("rows")
    if not _is_positive_int(rows):
        fail(
            "E_BAD_ROW_COUNT",
            "table rows must be a positive integer",
            details={"table": name, "rows": rows},
        )
    join_key = raw.get("join_key")
    if not isinstance(join_key, str) or not join_key:
        fail(
            "E_BAD_KEY_COLUMN",
            "table join_key must be a non-empty string",
            details={"table": name, "join_key": join_key},
        )
    raw_columns = raw.get("columns")
    if not isinstance(raw_columns, list) or not raw_columns:
        fail("E_BAD_TABLE", "table columns must be a non-empty list", details={"table": name})
    columns = tuple(_parse_column(column, name) for column in raw_columns)
    names = [column.name for column in columns]
    if len(set(names)) != len(names):
        fail("E_BAD_TABLE", "column names must be unique", details={"table": name, "columns": names})
    column_map = {column.name: column for column in columns}
    if join_key not in column_map:
        fail(
            "E_BAD_KEY_COLUMN",
            "join_key must reference an existing column",
            details={"table": name, "join_key": join_key, "columns": names},
        )
    for column in columns:
        if column.ndv > rows:
            fail(
                "E_BAD_NDV",
                "column NDV cannot exceed table row count",
                details={
                    "table": name,
                    "column": column.name,
                    "ndv": column.ndv,
                    "rows": rows,
                },
            )
    raw_skew = raw.get("skew")
    skew: SkewSpec | None = None
    if raw_skew is not None:
        if not isinstance(raw_skew, dict):
            fail("E_BAD_TABLE", "table skew must be null or an object", details={"table": name})
        skew_key = raw_skew.get("key")
        share = raw_skew.get("share")
        key_column = column_map[join_key]
        if key_column.data_type == "int" and (
            not isinstance(skew_key, int) or isinstance(skew_key, bool)
        ):
            fail(
                "E_BAD_TABLE",
                "integer join keys require an integer skew key",
                details={"table": name, "skew_key": skew_key},
            )
        if key_column.data_type == "string" and not isinstance(skew_key, str):
            fail(
                "E_BAD_TABLE",
                "string join keys require a string skew key",
                details={"table": name, "skew_key": skew_key},
            )
        if not _is_number(share) or not 0.0 < float(share) < 1.0:
            fail(
                "E_BAD_TABLE",
                "skew share must be in (0, 1)",
                details={"table": name, "share": share},
            )
        skew = SkewSpec(skew_key, float(share))
    return TableSpec(name, rows, join_key, columns, skew)


def _parse_scale(raw: Any) -> ScaleSample:
    if not isinstance(raw, dict):
        fail("E_BAD_SCALE", "scale sample must be an object")
    name = raw.get("name")
    left_rows = raw.get("left_rows")
    right_rows = raw.get("right_rows")
    if not isinstance(name, str) or not name:
        fail("E_BAD_SCALE", "scale sample name must be a non-empty string")
    if not SAFE_NAME_RE.fullmatch(name):
        fail(
            "E_BAD_SCALE",
            "scale name may contain only letters, digits, dot, underscore and hyphen",
            details={"scale": name},
        )
    if not _is_positive_int(left_rows) or not _is_positive_int(right_rows):
        fail(
            "E_BAD_SCALE",
            "scale sample row counts must be positive integers",
            details={"scale": name, "left_rows": left_rows, "right_rows": right_rows},
        )
    return ScaleSample(name, left_rows, right_rows)


def parse_sample(raw: Any, source_path: str = "") -> SampleConfig:
    if not isinstance(raw, dict):
        fail("E_BAD_INPUT", "sample root must be a JSON object")
    seed = raw.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        fail("E_BAD_INPUT", "seed must be an integer", details={"seed": seed})
    raw_tables = raw.get("tables")
    if not isinstance(raw_tables, list) or len(raw_tables) < 2:
        fail("E_BAD_TABLE", "tables must contain at least the two join sides")
    tables = tuple(_parse_table(table) for table in raw_tables)
    table_names = [table.name for table in tables]
    if len(set(table_names)) != len(table_names):
        fail("E_DUPLICATE_TABLE", "table names must be unique", details={"tables": table_names})
    table_map = {table.name: table for table in tables}

    raw_query = raw.get("query")
    if not isinstance(raw_query, dict):
        fail("E_BAD_QUERY", "query must be an object")
    query_id = raw_query.get("id")
    left_table = raw_query.get("left_table")
    right_table = raw_query.get("right_table")
    join_type = raw_query.get("join_type")
    predicate = raw_query.get("predicate")
    selectivity = raw_query.get("filter_selectivity")
    if not isinstance(query_id, str) or not query_id:
        fail("E_BAD_QUERY", "query id must be a non-empty string")
    if not SAFE_NAME_RE.fullmatch(query_id):
        fail(
            "E_BAD_QUERY",
            "query id may contain only letters, digits, dot, underscore and hyphen",
            details={"query_id": query_id},
        )
    if left_table not in table_map or right_table not in table_map:
        fail(
            "E_BAD_QUERY",
            "query left_table and right_table must reference declared tables",
            details={"left_table": left_table, "right_table": right_table, "tables": table_names},
        )
    if left_table == right_table:
        fail("E_BAD_QUERY", "left_table and right_table must differ", details={"table": left_table})
    if join_type not in JOIN_TYPES:
        fail(
            "E_BAD_JOIN_TYPE",
            "join type must be inner, left, or right",
            details={"join_type": join_type, "supported": sorted(JOIN_TYPES)},
        )
    if not isinstance(predicate, dict):
        fail("E_BAD_QUERY", "query predicate must be an object")
    left_key = predicate.get("left_key")
    right_key = predicate.get("right_key")
    left_table_spec = table_map[left_table]
    right_table_spec = table_map[right_table]
    if left_key != left_table_spec.join_key:
        fail(
            "E_BAD_KEY_COLUMN",
            "predicate left_key must match the left table join_key",
            details={"expected": left_table_spec.join_key, "actual": left_key},
        )
    if right_key != right_table_spec.join_key:
        fail(
            "E_BAD_KEY_COLUMN",
            "predicate right_key must match the right table join_key",
            details={"expected": right_table_spec.join_key, "actual": right_key},
        )
    left_key_type = left_table_spec.column_map[left_key].data_type
    right_key_type = right_table_spec.column_map[right_key].data_type
    if left_key_type != right_key_type:
        fail(
            "E_BAD_QUERY",
            "join key types must be compatible",
            details={"left_type": left_key_type, "right_type": right_key_type},
        )
    if not _is_number(selectivity) or not 0.0 < float(selectivity) < 1.0:
        fail(
            "E_BAD_SELECTIVITY",
            "filter_selectivity must be in the open interval (0, 1)",
            details={"filter_selectivity": selectivity},
        )
    query = QuerySpec(
        query_id,
        left_table,
        right_table,
        join_type,
        left_key,
        right_key,
        float(selectivity),
    )

    raw_exec = raw.get("execution")
    if not isinstance(raw_exec, dict):
        fail("E_BAD_CONFIG", "execution must be an object")
    partitions = raw_exec.get("partitions")
    if not _is_positive_int(partitions):
        fail(
            "E_BAD_PARTITION_COUNT",
            "partitions must be a positive integer",
            details={"partitions": partitions},
        )
    chunk_rows = raw_exec.get("chunk_rows")
    if not _is_positive_int(chunk_rows):
        fail(
            "E_BAD_CHUNK_ROWS",
            "chunk_rows must be a positive integer",
            details={"chunk_rows": chunk_rows},
        )
    memory_mib = raw_exec.get("memory_limit_mib")
    if not _is_number(memory_mib) or float(memory_mib) <= 0:
        fail(
            "E_BAD_CONFIG",
            "memory_limit_mib must be positive",
            details={"memory_limit_mib": memory_mib},
        )
    skew_threshold = raw_exec.get("skew_threshold")
    if not _is_number(skew_threshold) or not 0.0 < float(skew_threshold) <= 1.0:
        fail(
            "E_BAD_CONFIG",
            "skew_threshold must be in (0, 1]",
            details={"skew_threshold": skew_threshold},
        )
    clock_step = raw_exec.get("logical_clock_step")
    if not _is_number(clock_step) or float(clock_step) <= 0:
        fail(
            "E_BAD_CONFIG",
            "logical_clock_step must be positive",
            details={"logical_clock_step": clock_step},
        )
    raw_tolerance = raw_exec.get("tolerance", {})
    if raw_tolerance is None:
        raw_tolerance = {}
    if not isinstance(raw_tolerance, dict):
        fail("E_BAD_TOLERANCE", "tolerance must be an object")
    rel = raw_tolerance.get("relative", 0.0)
    abs_tol = raw_tolerance.get("absolute", 0.0)
    if not _is_number(rel) or float(rel) < 0 or not _is_number(abs_tol) or float(abs_tol) < 0:
        fail(
            "E_BAD_TOLERANCE",
            "tolerance values must be non-negative numbers",
            details={"relative": rel, "absolute": abs_tol},
        )
    broadcast_fraction = raw_exec.get("broadcast_fraction", 0.60)
    hash_fraction = raw_exec.get("hash_fraction", 0.80)
    if not _is_number(broadcast_fraction) or not 0.0 < float(broadcast_fraction) <= 1.0:
        fail(
            "E_BAD_CONFIG",
            "broadcast_fraction must be in (0, 1]",
            details={"broadcast_fraction": broadcast_fraction},
        )
    if not _is_number(hash_fraction) or not 0.0 < float(hash_fraction) <= 1.0:
        fail(
            "E_BAD_CONFIG",
            "hash_fraction must be in (0, 1]",
            details={"hash_fraction": hash_fraction},
        )
    raw_scales = raw_exec.get("scale_samples")
    if not isinstance(raw_scales, list) or not raw_scales:
        fail("E_BAD_SCALE", "scale_samples must be a non-empty list")
    scales = tuple(_parse_scale(scale) for scale in raw_scales)
    scale_names = [scale.name for scale in scales]
    if len(set(scale_names)) != len(scale_names):
        fail("E_BAD_SCALE", "scale sample names must be unique", details={"scales": scale_names})
    for scale in scales:
        for table, rows in (
            (left_table_spec, scale.left_rows),
            (right_table_spec, scale.right_rows),
        ):
            for column in table.columns:
                if column.ndv > rows:
                    fail(
                        "E_BAD_SCALE",
                        "scale sample row count cannot be smaller than a declared NDV",
                        details={
                            "scale": scale.name,
                            "table": table.name,
                            "column": column.name,
                            "ndv": column.ndv,
                            "rows": rows,
                        },
                    )
    default_scale = raw_exec.get("default_scale", scale_names[0])
    if default_scale not in scale_names:
        fail(
            "E_BAD_SCALE",
            "default_scale must reference a declared scale sample",
            details={"default_scale": default_scale, "available": scale_names},
        )
    execution = ExecutionConfig(
        partitions,
        float(memory_mib),
        chunk_rows,
        float(skew_threshold),
        float(clock_step),
        float(rel),
        float(abs_tol),
        float(broadcast_fraction),
        float(hash_fraction),
        scales,
        default_scale,
    )
    return SampleConfig(seed, tables, query, execution, source_path)


def load_sample(path: str | Path) -> SampleConfig:
    sample_path = Path(path)
    try:
        raw = json.loads(sample_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail("E_BAD_INPUT", f"sample file not found: {sample_path}")
    except json.JSONDecodeError as exc:
        fail(
            "E_BAD_INPUT",
            f"sample file is not valid JSON: {exc.msg}",
            details={"line": exc.lineno, "column": exc.colno},
        )
    return parse_sample(raw, str(sample_path.resolve()))


def sample_summary(config: SampleConfig, scale_name: str | None = None) -> dict[str, Any]:
    counts = config.row_counts(scale_name)
    return {
        "seed": config.seed,
        "query": config.query.to_dict(),
        "scale": config.scale(scale_name).to_dict(),
        "tables": [
            {
                **table.to_dict(),
                "effective_rows": counts.get(table.name, table.rows),
            }
            for table in config.tables
        ],
        "execution": config.execution.to_dict(),
        "fingerprint": config.fingerprint(scale_name),
        "canonical_payload_digest": sha256_value(config.fingerprint_payload(scale_name)),
    }


def json_dumps_stable(value: Any, *, indent: int = 2) -> str:
    return canonical_json(value) if indent == 0 else json.dumps(
        value,
        sort_keys=True,
        indent=indent,
        ensure_ascii=False,
        allow_nan=False,
    )
