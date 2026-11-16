"""Columnar data block / query model and strict local JSON loading."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from .errors import BenchError, ErrorCode

INT = "int"
FLOAT = "float"
BOOL = "bool"
SUPPORTED_TYPES = (INT, FLOAT, BOOL)

_DTYPE = {
    INT: np.dtype("int64"),
    FLOAT: np.dtype("float64"),
    BOOL: np.dtype("bool"),
}
_WIDTH = {INT: 8, FLOAT: 8, BOOL: 1}


def dtype_of(type_name: str) -> np.dtype:
    return _DTYPE[type_name]


def width_of(type_name: str) -> int:
    return _WIDTH[type_name]


@dataclass(frozen=True)
class Column:
    name: str
    type_name: str
    values: np.ndarray
    nullmap: np.ndarray  # bool ndarray, True means null
    block: str = ""

    @property
    def row_count(self) -> int:
        return int(self.values.shape[0])

    def content_digest(self) -> str:
        hasher = hashlib.sha256()
        hasher.update(self.name.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(self.type_name.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(np.ascontiguousarray(self.values).tobytes())
        hasher.update(b"\x00")
        hasher.update(np.ascontiguousarray(self.nullmap.astype(np.uint8)).tobytes())
        return hasher.hexdigest()


@dataclass
class DataBlock:
    name: str
    row_count: int
    columns: list[Column]
    version: str = ""

    @property
    def column_names(self) -> list[str]:
        return [column.name for column in self.columns]

    def column(self, name: str) -> Column:
        for column in self.columns:
            if column.name == name:
                return column
        raise KeyError(name)

    def content_digest(self) -> str:
        hasher = hashlib.sha256()
        hasher.update(self.name.encode("utf-8"))
        hasher.update(str(self.row_count).encode("ascii"))
        for column in self.columns:  # declaration order is significant
            hasher.update(column.content_digest().encode("ascii"))
        return hasher.hexdigest()

    def scaled(self, factor: int) -> "DataBlock":
        """Deterministically repeat columns ``factor`` times (whole blocks)."""
        if factor < 1:
            raise BenchError(
                ErrorCode.E_DATA, "scale factor must be >= 1", {"factor": factor}
            )
        repeated_columns = []
        for column in self.columns:
            values = np.tile(column.values, factor)
            nullmap = np.tile(column.nullmap, factor)
            repeated_columns.append(
                Column(column.name, column.type_name, values, nullmap, self.name)
            )
        scaled = DataBlock(self.name, self.row_count * factor, repeated_columns, self.version)
        return scaled


@dataclass(frozen=True)
class QuerySet:
    name: str
    expressions: tuple[str, ...]


def _load_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise BenchError(ErrorCode.E_DATA, "input file not found", {"path": path}) from exc
    except json.JSONDecodeError as exc:
        raise BenchError(
            ErrorCode.E_DATA,
            "invalid JSON document",
            {"path": path, "line": exc.lineno, "column": exc.colno},
        ) from exc


def _validate_scalar(value: Any, type_name: str, block_name: str, column_name: str, index: int) -> Any:
    # bool must be checked before int because bool is a subclass of int.
    if type_name == BOOL:
        if not isinstance(value, bool):
            raise BenchError(
                ErrorCode.E_TYPE,
                "value does not match declared boolean column type",
                {"block": block_name, "column": column_name, "row": index, "value": value},
            )
        return bool(value)
    if type_name == INT:
        if isinstance(value, bool) or not isinstance(value, int):
            raise BenchError(
                ErrorCode.E_TYPE,
                "value does not match declared integer column type",
                {"block": block_name, "column": column_name, "row": index, "value": value},
            )
        return int(value)
    if type_name == FLOAT:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BenchError(
                ErrorCode.E_TYPE,
                "value does not match declared float column type",
                {"block": block_name, "column": column_name, "row": index, "value": value},
            )
        return float(value)
    raise BenchError(
        ErrorCode.E_DATA,
        "unsupported column type",
        {"block": block_name, "column": column_name, "type": type_name},
    )


def _build_column(block_name: str, row_count: int, payload: Any, position: int) -> Column:
    if not isinstance(payload, dict):
        raise BenchError(
            ErrorCode.E_DATA, "column declaration must be an object", {"block": block_name, "index": position}
        )
    name = payload.get("name")
    if not isinstance(name, str) or not name:
        raise BenchError(
            ErrorCode.E_DATA, "column name must be a non-empty string",
            {"block": block_name, "index": position},
        )
    type_name = payload.get("type")
    if type_name not in SUPPORTED_TYPES:
        raise BenchError(
            ErrorCode.E_DATA,
            "column type must be one of int/float/bool",
            {"block": block_name, "column": name, "type": type_name},
        )
    raw_values = payload.get("values")
    if not isinstance(raw_values, list) or len(raw_values) != row_count:
        raise BenchError(
            ErrorCode.E_DATA,
            "column values length must equal row_count",
            {"block": block_name, "column": name, "expected": row_count,
             "actual": len(raw_values) if isinstance(raw_values, list) else None},
        )
    raw_nulls = payload.get("nullmap")
    if not isinstance(raw_nulls, list):
        raise BenchError(
            ErrorCode.E_DATA,
            "nullmap must be a list of booleans",
            {"block": block_name, "column": name},
        )
    if len(raw_nulls) != row_count:
        raise BenchError(
            ErrorCode.E_NULLMAP,
            "nullmap length must equal row_count",
            {"block": block_name, "column": name, "expected": row_count,
             "actual": len(raw_nulls)},
        )
    if not all(isinstance(flag, bool) for flag in raw_nulls):
        raise BenchError(
            ErrorCode.E_NULLMAP,
            "nullmap entries must be booleans",
            {"block": block_name, "column": name},
        )
    checked_values = [
        _validate_scalar(value, type_name, block_name, name, index)
        for index, value in enumerate(raw_values)
    ]
    values = np.array(checked_values, dtype=dtype_of(type_name))
    nullmap = np.array(raw_nulls, dtype=np.dtype("bool"))
    return Column(name, type_name, values, nullmap, block_name)


def load_blocks(path: str) -> list[DataBlock]:
    payload = _load_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("blocks"), list):
        raise BenchError(ErrorCode.E_DATA, "top-level document must contain a blocks list", {"path": path})
    blocks: list[DataBlock] = []
    seen_block_names: set[str] = set()
    for block_index, block_payload in enumerate(payload["blocks"]):
        if not isinstance(block_payload, dict):
            raise BenchError(ErrorCode.E_DATA, "block declaration must be an object", {"index": block_index})
        name = block_payload.get("name")
        if not isinstance(name, str) or not name:
            raise BenchError(ErrorCode.E_DATA, "block name must be a non-empty string", {"index": block_index})
        if name in seen_block_names:
            raise BenchError(ErrorCode.E_DATA, "duplicate block name", {"block": name})
        seen_block_names.add(name)
        row_count = block_payload.get("row_count")
        if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 0:
            raise BenchError(
                ErrorCode.E_DATA, "row_count must be a non-negative integer",
                {"block": name, "row_count": row_count},
            )
        columns_payload = block_payload.get("columns")
        if not isinstance(columns_payload, list):
            raise BenchError(ErrorCode.E_DATA, "block must contain a columns list", {"block": name})
        seen_columns: set[str] = set()
        columns: list[Column] = []
        for column_index, column_payload in enumerate(columns_payload):
            column = _build_column(name, row_count, column_payload, column_index)
            if column.name in seen_columns:
                raise BenchError(ErrorCode.E_DATA, "duplicate column name",
                                 {"block": name, "column": column.name})
            seen_columns.add(column.name)
            columns.append(column)
        version = block_payload.get("version")
        version = str(version) if version is not None else block_payload.get("data_version", "")
        blocks.append(DataBlock(name, row_count, columns, str(version or "")))
    return blocks


def load_queries(path: str, block: Optional[DataBlock] = None) -> QuerySet:
    payload = _load_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("expressions"), list):
        raise BenchError(ErrorCode.E_DATA, "query document must contain an expressions list", {"path": path})
    expressions = payload["expressions"]
    if not expressions or not all(isinstance(item, str) and item.strip() for item in expressions):
        raise BenchError(ErrorCode.E_DATA, "at least one non-empty expression string is required", {"path": path})
    name = str(payload.get("name", "queries"))
    query_set = QuerySet(name, tuple(expressions))
    if block is not None:
        # Eager parse + reference validation yields concrete expression positions.
        from .parser import parse_expression  # local import avoids an import cycle

        available = set(block.column_names)
        for position, text in enumerate(query_set.expressions):
            tree = parse_expression(text)
            for referenced in sorted(tree.column_refs()):
                if referenced not in available:
                    raise BenchError(
                        ErrorCode.E_DATA,
                        "expression references an unknown column",
                        {"query_index": position, "column": referenced, "block": block.name},
                    )
    return query_set
