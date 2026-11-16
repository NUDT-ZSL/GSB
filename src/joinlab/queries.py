"""Loading and validating repository-local query sample files."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .errors import QueryError, SchemaError, TypeMismatchError
from .query import Query, parse_query


@dataclass(frozen=True)
class NamedQuery:
    name: str
    sql: str
    query: Query

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "query_id": self.query.query_id,
            "canonical_sql": self.query.canonical_sql,
        }


def load_query_file(path: str | Path, catalog: Catalog) -> tuple[NamedQuery, ...]:
    source = Path(path).expanduser().resolve()
    try:
        with source.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError as exc:
        raise SchemaError("query file does not exist", str(source)) from exc
    except json.JSONDecodeError as exc:
        raise SchemaError(
            f"invalid query JSON: {exc.msg}",
            f"{source}@{exc.lineno}:{exc.colno}",
        ) from exc
    except OSError as exc:
        raise SchemaError(f"cannot read query file: {exc}", str(source)) from exc
    if not isinstance(document, dict):
        raise SchemaError("query file root must be an object", "$")
    raw_queries = document.get("queries")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise SchemaError("queries must be a non-empty array", "$.queries")
    names: set[str] = set()
    result: list[NamedQuery] = []
    for index, raw in enumerate(raw_queries):
        path_label = f"$.queries[{index}]"
        if not isinstance(raw, dict):
            raise SchemaError("query entry must be an object", path_label)
        name = raw.get("name")
        sql = raw.get("sql")
        if not isinstance(name, str) or not name.strip():
            raise SchemaError("query name must be a non-empty string", f"{path_label}.name")
        if name.lower() in names:
            raise SchemaError(f"duplicate query name {name!r}", f"{path_label}.name")
        names.add(name.lower())
        if not isinstance(sql, str) or not sql.strip():
            raise SchemaError("query SQL must be a non-empty string", f"{path_label}.sql")
        try:
            parsed = parse_query(sql, catalog)
        except QueryError as exc:
            # A missing/ambiguous column in a query sample is a schema
            # reference failure, while genuine grammar/feature rejection stays
            # E_QUERY.  This distinction is required by the load contract.
            if exc.details.get("reason") in {"unknown_ref", "ambiguous_ref"}:
                raise SchemaError(
                    f"query references invalid schema object: {exc.message}",
                    f"{path_label}.sql",
                    original_code=exc.code,
                    sql_offset=exc.path,
                    **exc.details,
                ) from exc
            raise
        except TypeMismatchError as exc:
            raise TypeMismatchError(
                exc.message,
                f"{path_label}.sql",
                sql_offset=exc.path,
                **exc.details,
            ) from exc
        result.append(NamedQuery(name=name, sql=sql, query=parsed))
    return tuple(result)
