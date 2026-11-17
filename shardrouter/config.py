"""JSON configuration loading and all-or-nothing strong validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .algorithms import build_virtual_ring
from .errors import E_CONFIG, E_RING, RouterError
from .models import (
    ColumnDef,
    ConsistentHashAlgorithm,
    ModAlgorithm,
    RangeAlgorithm,
    RingNode,
    RouteUnit,
    RouterConfig,
    ShardAlgorithm,
    TableConfig,
)
from .util import canonical_json, sha256_text


FORMAT_VERSION = 1
SUPPORTED_ALGORITHMS = {"mod", "range", "consistent_hash"}
RANGE_BOUNDARY_POLICY = "lower_inclusive_upper_exclusive"


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _config_error(message: str, field_path: str, errors: Sequence[Mapping[str, Any]] | None = None) -> RouterError:
    details: dict[str, Any] = {}
    if errors is not None:
        details["errors"] = list(errors)
    return RouterError(
        code=E_CONFIG,
        message=message,
        field_path=field_path,
        details=details,
    )


class _Errors:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, field_path: str, message: str) -> None:
        self.items.append({"field_path": field_path, "message": message})

    def __bool__(self) -> bool:
        return bool(self.items)

    def raise_if_any(self) -> None:
        if not self.items:
            return
        ordered = sorted(self.items, key=lambda item: (item["field_path"], item["message"]))
        first = ordered[0]
        raise _config_error(
            f"configuration rejected with {len(ordered)} validation error(s)",
            first["field_path"],
            ordered,
        )


def _column_defs(raw_columns: Any, path: str, errors: _Errors) -> tuple[ColumnDef, ...]:
    if not isinstance(raw_columns, list) or not raw_columns:
        errors.add(path, "columns must be a non-empty array")
        return ()
    columns: list[ColumnDef] = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(raw_columns):
        item_path = f"{path}[{index}]"
        if not isinstance(raw, dict):
            errors.add(item_path, "column definition must be an object")
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.add(f"{item_path}.name", "column name must be a non-empty string")
            continue
        folded = name.casefold()
        if folded in seen:
            errors.add(f"{item_path}.name", f"duplicate column name; first declared at index {seen[folded]}")
        else:
            seen[folded] = index
        type_name = raw.get("type")
        if not isinstance(type_name, str) or not type_name.strip():
            errors.add(f"{item_path}.type", "column type must be a non-empty string")
        nullable = raw.get("nullable", True)
        if not isinstance(nullable, bool):
            errors.add(f"{item_path}.nullable", "nullable must be a boolean")
        columns.append(ColumnDef(name=name, type=type_name if isinstance(type_name, str) else "", nullable=nullable if isinstance(nullable, bool) else True))
    return tuple(columns)


def _algorithm(
    raw: Any,
    path: str,
    shard_keys: Sequence[str],
    database_count: Any,
    tables_per_database: Any,
    errors: _Errors,
) -> ShardAlgorithm | None:
    if not isinstance(raw, dict):
        errors.add(path, "algorithm must be an object")
        return None
    type_name = raw.get("type")
    if type_name not in SUPPORTED_ALGORITHMS:
        errors.add(f"{path}.type", f"algorithm type must be one of {sorted(SUPPORTED_ALGORITHMS)}")
        return None
    if type_name == "mod":
        modulus = raw.get("modulus")
        if not _is_positive_int(modulus):
            errors.add(f"{path}.modulus", "modulus must be a positive integer")
        unknown = sorted(set(raw) - {"type", "modulus"})
        if unknown:
            errors.add(path, f"unsupported mod algorithm fields: {unknown}")
        if _is_positive_int(modulus):
            return ModAlgorithm(modulus=modulus)
        return None
    if type_name == "range":
        if len(shard_keys) != 1:
            errors.add(path, "range algorithm requires exactly one shard key")
        boundaries = raw.get("boundaries")
        parsed_boundaries: list[int | float] = []
        if not isinstance(boundaries, list) or not boundaries:
            errors.add(f"{path}.boundaries", "boundaries must be a non-empty array")
        else:
            previous: int | float | None = None
            for index, value in enumerate(boundaries):
                item_path = f"{path}.boundaries[{index}]"
                if not _is_number(value):
                    errors.add(item_path, "boundary must be a finite number")
                    continue
                if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
                    errors.add(item_path, "boundary must be a finite number")
                    continue
                if previous is not None and value <= previous:
                    errors.add(item_path, "boundaries must be strictly increasing")
                parsed_boundaries.append(value)
                previous = value
        policy = raw.get("boundary_policy", RANGE_BOUNDARY_POLICY)
        if policy != RANGE_BOUNDARY_POLICY:
            errors.add(f"{path}.boundary_policy", f"boundary_policy must be {RANGE_BOUNDARY_POLICY!r}")
        unknown = sorted(set(raw) - {"type", "boundaries", "boundary_policy"})
        if unknown:
            errors.add(path, f"unsupported range algorithm fields: {unknown}")
        if not errors.items:
            return RangeAlgorithm(boundaries=tuple(parsed_boundaries), boundary_policy=policy)
        if parsed_boundaries and len(parsed_boundaries) == len(boundaries) and policy == RANGE_BOUNDARY_POLICY and len(shard_keys) == 1:
            return RangeAlgorithm(boundaries=tuple(parsed_boundaries), boundary_policy=policy)
        return None

    # consistent_hash
    ring_version = raw.get("ring_version")
    if not _is_positive_int(ring_version):
        errors.add(f"{path}.ring_version", "ring_version must be a positive integer")
    nodes_raw = raw.get("nodes")
    nodes: list[RingNode] = []
    if not isinstance(nodes_raw, list) or not nodes_raw:
        errors.add(f"{path}.nodes", "nodes must be a non-empty array")
    else:
        for index, node_raw in enumerate(nodes_raw):
            item_path = f"{path}.nodes[{index}]"
            if not isinstance(node_raw, dict):
                errors.add(item_path, "ring node must be an object")
                continue
            database = node_raw.get("database")
            table = node_raw.get("table")
            weight = node_raw.get("weight")
            if not _is_positive_int(weight):
                errors.add(f"{item_path}.weight", "virtual node weight must be a positive integer")
            if not isinstance(database, int) or isinstance(database, bool) or database < 0:
                errors.add(f"{item_path}.database", "database index must be a non-negative integer")
            elif _is_positive_int(database_count) and database >= database_count:
                errors.add(f"{item_path}.database", f"database index must be less than database_count ({database_count})")
            if not isinstance(table, int) or isinstance(table, bool) or table < 0:
                errors.add(f"{item_path}.table", "table index must be a non-negative integer")
            elif _is_positive_int(tables_per_database) and table >= tables_per_database:
                errors.add(f"{item_path}.table", f"table index must be less than tables_per_database ({tables_per_database})")
            if (
                _is_positive_int(weight)
                and isinstance(database, int)
                and not isinstance(database, bool)
                and isinstance(table, int)
                and not isinstance(table, bool)
            ):
                nodes.append(RingNode(database=database, table=table, weight=weight))
    unknown = sorted(set(raw) - {"type", "ring_version", "nodes"})
    if unknown:
        errors.add(path, f"unsupported consistent_hash algorithm fields: {unknown}")
    if len(nodes) == len(nodes_raw) and _is_positive_int(ring_version):
        return ConsistentHashAlgorithm(ring_version=ring_version, nodes=tuple(nodes))
    return None


def _route_units(
    raw_units: Any,
    table_names: set[str],
    errors: _Errors,
) -> tuple[RouteUnit, ...]:
    if not isinstance(raw_units, list):
        errors.add("$.route_units", "route_units must be an array")
        return ()
    units: list[RouteUnit] = []
    seen_names: dict[str, int] = {}
    raw_names: set[str] = set()
    for index, raw in enumerate(raw_units):
        path = f"$.route_units[{index}]"
        if not isinstance(raw, dict):
            errors.add(path, "route unit must be an object")
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.add(f"{path}.name", "route unit name must be a non-empty string")
            continue
        folded = name.casefold()
        if folded in seen_names:
            errors.add(f"{path}.name", f"duplicate route unit name; first declared at index {seen_names[folded]}")
        else:
            seen_names[folded] = index
        raw_names.add(name)
        table = raw.get("table")
        predicate = raw.get("predicate")
        if table is not None and not isinstance(table, str):
            errors.add(f"{path}.table", "table must be a string when present")
            table = None
        if predicate is not None and not isinstance(predicate, str):
            errors.add(f"{path}.predicate", "predicate must be a string when present")
            predicate = None
        if (table is None) != (predicate is None):
            errors.add(path, "table and predicate must be supplied together")
        if table is not None and table not in table_names:
            errors.add(f"{path}.table", f"referenced table {table!r} does not exist")
        depends_raw = raw.get("depends_on", [])
        depends: list[str] = []
        if not isinstance(depends_raw, list) or any(not isinstance(item, str) for item in depends_raw):
            errors.add(f"{path}.depends_on", "depends_on must be an array of route unit names")
        else:
            seen_dependencies: set[str] = set()
            for dep_index, dependency in enumerate(depends_raw):
                if dependency in seen_dependencies:
                    errors.add(f"{path}.depends_on[{dep_index}]", "duplicate dependency")
                seen_dependencies.add(dependency)
                depends.append(dependency)
        if table is None and predicate is None and not depends:
            errors.add(path, "a route unit needs table/predicate or at least one dependency")
        unknown = sorted(set(raw) - {"name", "table", "predicate", "depends_on"})
        if unknown:
            errors.add(path, f"unsupported route unit fields: {unknown}")
        units.append(RouteUnit(name=name, table=table, predicate=predicate, depends_on=tuple(depends)))

    for index, unit in enumerate(units):
        path = f"$.route_units[{index}].depends_on"
        for dep_index, dependency in enumerate(unit.depends_on):
            if dependency not in raw_names:
                errors.add(f"{path}[{dep_index}]", f"dependency {dependency!r} does not exist")
    _detect_route_cycles(units, raw_names, errors)
    return tuple(units)


def _detect_route_cycles(units: Sequence[RouteUnit], known: set[str], errors: _Errors) -> None:
    graph = {unit.name: tuple(dep for dep in unit.depends_on if dep in known) for unit in units}
    visiting: list[str] = []
    visited: set[str] = set()
    cycle_reported: set[tuple[str, ...]] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            start = visiting.index(name)
            cycle = tuple(visiting[start:] + [name])
            canonical_cycle = min(
                tuple(cycle[index:] + cycle[:index]) for index in range(len(cycle) - 1)
            )
            if canonical_cycle not in cycle_reported:
                cycle_reported.add(canonical_cycle)
                first = next((index for index, unit in enumerate(units) if unit.name == name), 0)
                errors.add(
                    f"$.route_units[{first}].depends_on",
                    "dependency cycle: " + " -> ".join(canonical_cycle),
                )
            return
        visiting.append(name)
        for dependency in graph.get(name, ()):  # dependency -> unit direction is irrelevant for DFS
            visit(dependency)
        visiting.pop()
        visited.add(name)

    for unit in sorted(units, key=lambda item: item.name):
        visit(unit.name)


def _parse_tables(raw_tables: Any, errors: _Errors) -> tuple[TableConfig, ...]:
    if not isinstance(raw_tables, list) or not raw_tables:
        errors.add("$.tables", "tables must be a non-empty array")
        return ()
    tables: list[TableConfig] = []
    seen_names: dict[str, int] = {}
    for index, raw in enumerate(raw_tables):
        path = f"$.tables[{index}]"
        if not isinstance(raw, dict):
            errors.add(path, "table must be an object")
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.add(f"{path}.name", "table name must be a non-empty string")
            continue
        folded = name.casefold()
        if folded in seen_names:
            errors.add(f"{path}.name", f"duplicate table name; first declared at index {seen_names[folded]}")
        else:
            seen_names[folded] = index
        columns = _column_defs(raw.get("columns"), f"{path}.columns", errors)
        column_names = {column.name.casefold() for column in columns}
        shard_keys_raw = raw.get("shard_keys")
        shard_keys: list[str] = []
        if not isinstance(shard_keys_raw, list) or not shard_keys_raw:
            errors.add(f"{path}.shard_keys", "shard_keys must be a non-empty array")
        else:
            seen_keys: set[str] = set()
            for key_index, key in enumerate(shard_keys_raw):
                key_path = f"{path}.shard_keys[{key_index}]"
                if not isinstance(key, str) or not key.strip():
                    errors.add(key_path, "shard key must be a non-empty column name")
                    continue
                if key.casefold() in seen_keys:
                    errors.add(key_path, "duplicate shard key")
                seen_keys.add(key.casefold())
                if key.casefold() not in column_names:
                    errors.add(key_path, f"shard key column {key!r} does not exist in table columns")
                shard_keys.append(key)
        database_count = raw.get("database_count")
        tables_per_database = raw.get("tables_per_database")
        version = raw.get("version")
        if not _is_positive_int(database_count):
            errors.add(f"{path}.database_count", "database_count must be a positive integer")
        if not _is_positive_int(tables_per_database):
            errors.add(f"{path}.tables_per_database", "tables_per_database must be a positive integer")
        if not _is_positive_int(version):
            errors.add(f"{path}.version", "version must be a positive integer")
        algorithm = _algorithm(
            raw.get("algorithm"),
            f"{path}.algorithm",
            shard_keys,
            database_count,
            tables_per_database,
            errors,
        )
        unknown = sorted(
            set(raw)
            - {
                "algorithm",
                "columns",
                "database_count",
                "name",
                "shard_keys",
                "tables_per_database",
                "version",
            }
        )
        if unknown:
            errors.add(path, f"unsupported table fields: {unknown}")
        if (
            isinstance(name, str)
            and name.strip()
            and columns
            and shard_keys
            and _is_positive_int(database_count)
            and _is_positive_int(tables_per_database)
            and _is_positive_int(version)
            and algorithm is not None
        ):
            tables.append(
                TableConfig(
                    name=name,
                    columns=columns,
                    shard_keys=tuple(shard_keys),
                    database_count=database_count,
                    tables_per_database=tables_per_database,
                    algorithm=algorithm,
                    version=version,
                )
            )
    return tuple(tables)


def parse_config(
    data: Any,
    *,
    source: str = "<memory>",
    inject_ring_conflict: bool = False,
) -> RouterConfig:
    """Validate a decoded JSON document and return an immutable config.

    Validation is all-or-nothing: no ``RouterConfig`` is returned if any field
    is invalid.  ``source`` is accepted for call-site context but deliberately
    excluded from the returned model so artifacts remain byte reproducible.
    """

    del source
    errors = _Errors()
    if not isinstance(data, dict):
        raise _config_error("configuration root must be a JSON object", "$")

    format_version = data.get("format_version")
    config_version = data.get("config_version")
    metadata_version = data.get("metadata_version")
    if not _is_positive_int(format_version):
        errors.add("$.format_version", "format_version must be a positive integer")
    elif format_version != FORMAT_VERSION:
        errors.add("$.format_version", f"unsupported format_version; expected {FORMAT_VERSION}")
    if not _is_positive_int(config_version):
        errors.add("$.config_version", "config_version must be a positive integer")
    if not _is_positive_int(metadata_version):
        errors.add("$.metadata_version", "metadata_version must be a positive integer")

    raw_tables = data.get("tables")
    tables = _parse_tables(raw_tables, errors)
    table_names = {table.name for table in tables}
    if isinstance(raw_tables, list):
        for index, raw in enumerate(raw_tables):
            if isinstance(raw, dict) and isinstance(raw.get("name"), str):
                table_names.add(raw["name"])

    route_units = _route_units(data.get("route_units", []), table_names, errors)
    unknown = sorted(set(data) - {"format_version", "config_version", "metadata_version", "tables", "route_units"})
    if unknown:
        errors.add("$", f"unsupported top-level fields: {unknown}")
    errors.raise_if_any()

    if inject_ring_conflict:
        consistent_tables = [table for table in tables if isinstance(table.algorithm, ConsistentHashAlgorithm)]
        if not consistent_tables:
            raise RouterError(
                code=E_RING,
                message="ring conflict injection requested but configuration has no consistent_hash table",
                field_path="$.tables",
                details={"injection": "ring-conflict"},
            )
        target = consistent_tables[0]
        algorithm = target.algorithm
        assert isinstance(algorithm, ConsistentHashAlgorithm)
        first = algorithm.nodes[0]
        algorithm = ConsistentHashAlgorithm(
            ring_version=algorithm.ring_version,
            nodes=algorithm.nodes + (first,),
        )
        replacement = target.with_algorithm(algorithm)
        tables = tuple(replacement if table.name == target.name else table for table in tables)

    for index, table in enumerate(tables):
        if not isinstance(table.algorithm, ConsistentHashAlgorithm):
            continue
        try:
            build_virtual_ring(table)
        except RouterError as error:
            if error.code != E_RING:
                raise
            details = dict(error.details)
            details["table_index"] = index
            raise RouterError(
                code=E_RING,
                message=error.message,
                field_path=f"$.tables[{index}].algorithm.nodes",
                details=details,
            ) from error

    # Rebuild route units to include exact table spelling from validated tables.
    rebuilt_units = tuple(
        RouteUnit(
            name=unit.name,
            table=unit.table,
            predicate=unit.predicate,
            depends_on=unit.depends_on,
        )
        for unit in route_units
    )
    return RouterConfig(
        format_version=format_version,
        config_version=config_version,
        metadata_version=metadata_version,
        tables=tuple(sorted(tables, key=lambda table: table.name)),
        route_units=tuple(sorted(rebuilt_units, key=lambda unit: unit.name)),
    )


def load_config(
    path: str | Path,
    *,
    inject_ring_conflict: bool = False,
) -> RouterConfig:
    """Load and validate a local JSON configuration file."""

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as error:
        raise _config_error(f"configuration file not found: {Path(path).name}", "$") from error
    except PermissionError as error:
        raise _config_error(f"configuration file is not readable: {Path(path).name}", "$") from error
    except json.JSONDecodeError as error:
        raise _config_error(
            f"configuration is not valid JSON: {error.msg}",
            "$",
            [{"field_path": "$", "message": f"JSON decode error at line {error.lineno}, column {error.colno}: {error.msg}"}],
        ) from error
    except UnicodeDecodeError as error:
        raise _config_error("configuration is not valid UTF-8", "$") from error
    return parse_config(data, source=str(path), inject_ring_conflict=inject_ring_conflict)


def validate_config_file(path: str | Path) -> dict[str, Any]:
    config = load_config(path)
    return config_summary(config)


def config_summary(config: RouterConfig) -> dict[str, Any]:
    return {
        "config_version": config.config_version,
        "format_version": config.format_version,
        "metadata_version": config.metadata_version,
        "route_unit_count": len(config.route_units),
        "status": "ok",
        "tables": [
            {
                "algorithm": table.algorithm.type,
                "database_count": table.database_count,
                "name": table.name,
                "ring_version": table.ring_version,
                "rule_version": table.version,
                "shard_keys": list(table.shard_keys),
                "tables_per_database": table.tables_per_database,
                "total_shards": table.total_shards,
            }
            for table in sorted(config.tables, key=lambda item: item.name)
        ],
    }


def table_rule_fingerprint(table: TableConfig) -> str:
    algorithm = table.algorithm
    if isinstance(algorithm, ConsistentHashAlgorithm):
        # Ring placement is versioned independently so a pure ring change does
        # not spuriously bump the table-rule version.
        algorithm_payload = {"type": algorithm.type}
    else:
        algorithm_payload = algorithm.to_dict()
    payload = {
        "algorithm": algorithm_payload,
        "columns": [column.to_dict() for column in table.columns],
        "database_count": table.database_count,
        "name": table.name,
        "shard_keys": list(table.shard_keys),
        "tables_per_database": table.tables_per_database,
    }
    return sha256_text(canonical_json(payload))


def ring_fingerprint(table: TableConfig) -> str:
    algorithm = table.algorithm
    if not isinstance(algorithm, ConsistentHashAlgorithm):
        return "none"
    payload = {
        "nodes": [
            {"database": node.database, "table": node.table, "weight": node.weight}
            for node in algorithm.nodes
        ],
        "table": table.name,
    }
    return sha256_text(canonical_json(payload))


def route_unit_fingerprint(units: Iterable[RouteUnit]) -> str:
    payload = [
        {
            "depends_on": list(unit.depends_on),
            "name": unit.name,
            "predicate": unit.predicate,
            "table": unit.table,
        }
        for unit in sorted(units, key=lambda item: item.name)
    ]
    return sha256_text(canonical_json(payload))
