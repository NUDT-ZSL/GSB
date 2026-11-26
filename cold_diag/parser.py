from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .errors import PolicyIntegrityError, SampleValidationError
from .models import FunctionSpec, PolicyRecord, RequestRecord
from .utils import read_json, sha256_file


def _require_int(data: dict[str, Any], aliases: tuple[str, ...], label: str) -> int:
    for alias in aliases:
        if alias in data and data[alias] is not None:
            value = data[alias]
            if isinstance(value, bool) or not isinstance(value, int):
                raise SampleValidationError(f"{label} must be an integer")
            return value
    raise SampleValidationError(f"{label} is required")


def _optional_int(
    data: dict[str, Any], aliases: tuple[str, ...], default: int | None = None
) -> int | None:
    for alias in aliases:
        if alias in data and data[alias] is not None:
            value = data[alias]
            if isinstance(value, bool) or not isinstance(value, int):
                raise SampleValidationError(f"{aliases[0]} must be an integer")
            return value
    return default


def _as_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    raise SampleValidationError("value must be a string or number")


def _records(payload: Any, container_key: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in (container_key, "items", "data"):
            if key in payload and isinstance(payload[key], list):
                return [item for item in payload[key] if isinstance(item, dict)]
    raise SampleValidationError(f"expected a list under {container_key!r}")


def parse_function_specs(payload: Any) -> list[FunctionSpec]:
    rows = _records(payload, "functions")
    functions: list[FunctionSpec] = []
    seen: set[str] = set()
    errors: list[str] = []

    for index, row in enumerate(rows):
        try:
            name = row.get("name") or row.get("function_name") or row.get("function")
            if not isinstance(name, str) or not name.strip():
                raise SampleValidationError("function name must be a non-empty string")
            name = name.strip()
            if name in seen:
                raise SampleValidationError(f"duplicate function name: {name}")
            cold_start_ms = _require_int(
                row,
                (
                    "cold_start_ms",
                    "cold_start_cost_ms",
                    "cold_start_duration_ms",
                    "cold_start_milliseconds",
                ),
                f"functions[{index}].cold_start_ms",
            )
            if cold_start_ms <= 0:
                raise SampleValidationError("cold_start_ms must be a positive integer")
            concurrency_limit = _require_int(
                row,
                (
                    "concurrency_limit",
                    "instance_concurrency",
                    "max_concurrency",
                    "concurrency",
                ),
                f"functions[{index}].concurrency_limit",
            )
            if concurrency_limit <= 0:
                raise SampleValidationError("concurrency_limit must be a positive integer")
            warmup_window_ms = _optional_int(
                row,
                ("warmup_window_ms", "warmup_window_milliseconds", "warmup_ttl_ms"),
                60_000,
            )
            assert warmup_window_ms is not None
            if warmup_window_ms < 0:
                raise SampleValidationError("warmup_window_ms must be non-negative")
            priority = _optional_int(row, ("priority", "function_priority"), 100)
            assert priority is not None
            container = row.get("container_spec", row.get("container", "default"))
            if isinstance(container, (dict, list)):
                from .utils import canonical_json

                container_spec = canonical_json(container)
            else:
                container_spec = str(container)
            dependencies = tuple(row.get("dependencies", ()) or ())
            if not all(isinstance(item, str) and item for item in dependencies):
                raise SampleValidationError("dependencies must contain non-empty strings")

            spec = FunctionSpec(
                name=name,
                cold_start_ms=cold_start_ms,
                concurrency_limit=concurrency_limit,
                warmup_window_ms=warmup_window_ms,
                container_spec=container_spec,
                priority=priority,
                dependencies=dependencies,
                raw=dict(row),
            )
            functions.append(spec)
            seen.add(name)
        except SampleValidationError as exc:
            errors.append(str(exc))

    if errors:
        raise SampleValidationError("; ".join(errors))
    if not functions:
        raise SampleValidationError("at least one function declaration is required")
    return functions


def load_function_specs(path: str | Path) -> list[FunctionSpec]:
    return parse_function_specs(read_json(path))


def parse_requests(payload: Any, known_functions: Iterable[str] | None = None) -> list[RequestRecord]:
    rows = _records(payload, "requests")
    known = set(known_functions or [])
    requests: list[RequestRecord] = []
    previous_seq: int | None = None
    errors: list[str] = []

    for index, row in enumerate(rows):
        try:
            seq = _require_int(
                row, ("seq", "request_seq", "sequence", "request_index"), f"requests[{index}].seq"
            )
            if previous_seq is not None and seq <= previous_seq:
                raise SampleValidationError(
                    f"request seq must be strictly increasing: {seq} follows {previous_seq}"
                )
            previous_seq = seq

            function_name = row.get("function_name") or row.get("function") or row.get("name")
            if not isinstance(function_name, str) or not function_name.strip():
                raise SampleValidationError(f"requests[{seq}] function name is required")
            function_name = function_name.strip()
            if known and function_name not in known:
                raise SampleValidationError(
                    f"requests[{seq}] references undeclared function {function_name!r}"
                )

            clock = _require_int(
                row,
                (
                    "logical_clock_ms",
                    "request_logical_clock_ms",
                    "clock_ms",
                    "logical_clock",
                    "request_clock",
                ),
                f"requests[{seq}].logical_clock_ms",
            )
            if clock < 0:
                raise SampleValidationError(f"requests[{seq}] logical clock must be non-negative")

            priority = _optional_int(row, ("priority", "request_priority"), 100)
            assert priority is not None
            expected_latency = _optional_int(
                row,
                ("expected_latency_ms", "expected_delay_ms", "expected_latency"),
                1,
            )
            assert expected_latency is not None
            if expected_latency < 0:
                raise SampleValidationError(
                    f"requests[{seq}] expected_latency_ms must be non-negative"
                )
            duration_ms = _optional_int(row, ("duration_ms", "actual_latency_ms"), None)
            if duration_ms is not None and duration_ms < 0:
                raise SampleValidationError(f"requests[{seq}] duration_ms must be non-negative")
            status = row.get("call_status", row.get("status", "submitted"))
            if not isinstance(status, str) or not status.strip():
                raise SampleValidationError(f"requests[{seq}] call_status must be a string")

            requests.append(
                RequestRecord(
                    seq=seq,
                    function_name=function_name,
                    logical_clock_ms=clock,
                    priority=priority,
                    expected_latency_ms=expected_latency,
                    call_status=status,
                    duration_ms=duration_ms,
                    raw=dict(row),
                )
            )
        except SampleValidationError as exc:
            errors.append(str(exc))

    if errors:
        raise SampleValidationError("; ".join(errors))
    if not requests:
        raise SampleValidationError("at least one request is required")
    return requests


def load_requests(
    path: str | Path, known_functions: Iterable[str] | None = None
) -> list[RequestRecord]:
    return parse_requests(read_json(path), known_functions)


def _policy_key(row: dict[str, Any]) -> str:
    key = row.get("key") or row.get("policy_key") or row.get("id")
    if key is not None:
        return str(key)
    function_name = row.get("function") or row.get("function_name") or row.get("name")
    if function_name:
        return f"function:{function_name}"
    raise SampleValidationError("policy record requires key or function name")


def _pool_params(row: dict[str, Any]) -> dict[str, Any]:
    pool = row.get("pool")
    result: dict[str, Any] = {}
    if isinstance(pool, dict):
        result.update(pool)
    for key in (
        "min_ready",
        "max_instances",
        "warmup_batch",
        "idle_ttl_ms",
        "scale_step",
        "global_cold_start_limit",
        "target_concurrency",
    ):
        if key in row:
            result[key] = row[key]
    return result


def parse_v1_policy(
    payload: Any,
    *,
    source: str = "v1.json",
    declared_clock_ms: int | None = None,
    source_version: str = "",
) -> list[PolicyRecord]:
    if not isinstance(payload, dict):
        raise SampleValidationError("v1 policy document must be an object")
    if declared_clock_ms is None:
        declared_clock_ms = _optional_int(
            payload, ("declared_clock_ms", "logical_clock_ms", "base_clock_ms"), 0
        )
    assert declared_clock_ms is not None
    if declared_clock_ms < 0:
        raise SampleValidationError("v1 declared_clock_ms must be non-negative")
    version = str(payload.get("version", source_version or "v1"))

    if isinstance(payload.get("policies"), list):
        rows = payload["policies"]
    elif isinstance(payload.get("records"), list):
        rows = payload["records"]
    elif payload.get("pools") and isinstance(payload["pools"], dict):
        rows = []
        for key, value in payload["pools"].items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("key", key)
                rows.append(row)
    elif payload.get("key") or payload.get("function") or payload.get("function_name"):
        rows = [payload]
    else:
        raise SampleValidationError("v1 policy must contain policies/records or a flat record")

    records: list[PolicyRecord] = []
    errors: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"v1.policies[{index}] must be an object")
            continue
        try:
            relative_seconds = _require_int(
                row,
                (
                    "effective_relative_seconds",
                    "relative_effective_seconds",
                    "effective_after_seconds",
                    "relative_seconds",
                ),
                f"v1.policies[{index}].effective_relative_seconds",
            )
            if relative_seconds < 0:
                raise SampleValidationError("v1 effective_relative_seconds must be non-negative")
            row_declared = _optional_int(
                row, ("declared_clock_ms", "logical_clock_ms", "base_clock_ms"), declared_clock_ms
            )
            assert row_declared is not None
            generation = _optional_int(row, ("generation", "policy_generation"), 1)
            assert generation is not None
            if generation < 1:
                raise SampleValidationError("policy generation must be >= 1")
            function_name = row.get("function_name") or row.get("function")
            priority = _optional_int(row, ("priority", "function_priority"), None)
            records.append(
                PolicyRecord(
                    key=_policy_key(row),
                    format_generation="v1",
                    generation=generation,
                    declared_clock_ms=row_declared,
                    effective_clock_ms=row_declared + relative_seconds * 1000,
                    original_time_kind="relative_seconds",
                    original_time_value=relative_seconds,
                    source=source,
                    source_version=str(row.get("version", version)),
                    function_name=str(function_name) if function_name else None,
                    tombstone=bool(row.get("tombstone", False)),
                    pool=_pool_params(row),
                    priority=priority,
                    raw=dict(row),
                )
            )
        except SampleValidationError as exc:
            errors.append(str(exc))
    if errors:
        raise SampleValidationError("; ".join(errors))
    return records


def load_v1_policy(path: str | Path, declared_clock_ms: int | None = None) -> list[PolicyRecord]:
    path = Path(path)
    return parse_v1_policy(
        read_json(path), source=str(path), declared_clock_ms=declared_clock_ms
    )


def _parse_v2_record(row: dict[str, Any], source: str) -> PolicyRecord:
    effective_ms = _require_int(
        row,
        ("effective_at_ms", "effective_clock_ms", "absolute_effective_ms", "effective_ms"),
        "v2.effective_at_ms",
    )
    if effective_ms < 0:
        raise SampleValidationError("v2 effective_at_ms must be non-negative")
    generation = _require_int(row, ("generation", "policy_generation"), "v2.generation")
    if generation < 1:
        raise SampleValidationError("v2 generation must be >= 1")
    declared_clock = _optional_int(
        row, ("declared_clock_ms", "logical_clock_ms"), effective_ms
    )
    assert declared_clock is not None
    function_name = row.get("function_name") or row.get("function")
    priority = _optional_int(row, ("priority", "function_priority"), None)
    version = row.get("version", "")
    return PolicyRecord(
        key=_policy_key(row),
        format_generation="v2",
        generation=generation,
        declared_clock_ms=declared_clock,
        effective_clock_ms=effective_ms,
        original_time_kind="absolute_milliseconds",
        original_time_value=effective_ms,
        source=source,
        source_version=str(version),
        function_name=str(function_name) if function_name else None,
        tombstone=bool(row.get("tombstone", row.get("deleted", False))),
        pool=_pool_params(row),
        priority=priority,
        raw=dict(row),
    )


def parse_v2_shard(payload: Any, source: str) -> list[PolicyRecord]:
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise PolicyIntegrityError(f"v2 shard {source} must contain a records list")
    records: list[PolicyRecord] = []
    for index, row in enumerate(payload["records"]):
        if not isinstance(row, dict):
            raise PolicyIntegrityError(f"v2 shard {source}[{index}] must be an object")
        try:
            records.append(_parse_v2_record(row, source))
        except SampleValidationError as exc:
            raise PolicyIntegrityError(f"{source}[{index}]: {exc}") from exc
    return records


def read_v2_metadata(db_path: str | Path) -> list[dict[str, Any]]:
    path = Path(db_path)
    if not path.exists():
        raise PolicyIntegrityError(f"v2 metadata database does not exist: {path}")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT key, generation, effective_at_ms, tombstone, shard_path, shard_hash
                FROM policy_metadata
                ORDER BY key ASC, effective_at_ms ASC, generation ASC
                """
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise PolicyIntegrityError(f"cannot read v2 metadata database: {exc}") from exc
    return [dict(row) for row in rows]


def load_v2_package(
    path: str | Path,
) -> tuple[list[PolicyRecord], dict[str, Any], list[dict[str, Any]]]:
    root = Path(path)
    manifest_path = root / "manifest.json" if root.is_dir() else root
    package_dir = manifest_path.parent
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise PolicyIntegrityError("v2 manifest must be an object")
    shards = manifest.get("shards")
    if not isinstance(shards, list) or not shards:
        raise PolicyIntegrityError("v2 manifest requires a non-empty shards list")

    records: list[PolicyRecord] = []
    previous_generation_max: int | None = None
    for index, shard in enumerate(shards):
        if not isinstance(shard, dict) or "path" not in shard:
            raise PolicyIntegrityError(f"shard descriptor {index} requires path")
        shard_path = package_dir / str(shard["path"])
        if not shard_path.exists():
            raise PolicyIntegrityError(f"missing v2 shard: {shard_path}")
        actual_hash = sha256_file(shard_path)
        expected_hash = shard.get("sha256")
        if expected_hash and actual_hash != expected_hash:
            raise PolicyIntegrityError(
                f"shard hash mismatch for {shard['path']}: expected {expected_hash}, got {actual_hash}"
            )
        payload = read_json(shard_path)
        shard_records = parse_v2_shard(payload, str(shard_path))
        if not shard_records:
            raise PolicyIntegrityError(f"shard {shard['path']} contains no records")
        generations = [record.generation for record in shard_records]
        if generations != sorted(generations):
            raise PolicyIntegrityError(
                f"policy generations are not monotonic inside shard {shard['path']}"
            )
        generation_min = int(shard.get("generation_min", min(generations)))
        generation_max = int(shard.get("generation_max", max(generations)))
        if generation_min != min(generations) or generation_max != max(generations):
            raise PolicyIntegrityError(f"shard generation bounds mismatch for {shard['path']}")
        if previous_generation_max is not None and generation_min < previous_generation_max:
            raise PolicyIntegrityError(
                f"shard generation order decreases at {shard['path']}: "
                f"{generation_min} < previous max {previous_generation_max}"
            )
        previous_generation_max = generation_max
        records.extend(shard_records)

    db_name = manifest.get("metadata_db", "metadata.sqlite")
    metadata_rows = read_v2_metadata(package_dir / str(db_name))
    _validate_metadata_matches(records, metadata_rows, shards, package_dir)
    _validate_key_generation_monotonic(metadata_rows)

    expected_digest = manifest.get("key_set_digest") or manifest.get("keys_digest")
    if expected_digest:
        from .utils import stable_key_digest

        actual_digest = stable_key_digest([record.key for record in records])
        if expected_digest != actual_digest:
            raise PolicyIntegrityError(
                f"v2 key set digest mismatch: expected {expected_digest}, got {actual_digest}"
            )
    return records, manifest, metadata_rows


def _validate_metadata_matches(
    records: list[PolicyRecord],
    metadata_rows: list[dict[str, Any]],
    shards: list[dict[str, Any]],
    package_dir: Path,
) -> None:
    shard_hash_by_path = {
        str(shard["path"]): str(shard.get("sha256", "")) for shard in shards if isinstance(shard, dict)
    }
    record_index = {(record.key, record.generation): record for record in records}
    if len(record_index) != len(records):
        raise PolicyIntegrityError("duplicate v2 (key, generation) record in shards")

    metadata_index = {(str(row["key"]), int(row["generation"])): row for row in metadata_rows}
    for identity, record in record_index.items():
        row = metadata_index.get(identity)
        if row is None:
            raise PolicyIntegrityError(f"shard record {identity} is absent from SQLite metadata")
        if int(row["effective_at_ms"]) != record.effective_clock_ms:
            raise PolicyIntegrityError(f"effective time mismatch for {identity}")
        if bool(row["tombstone"]) != record.tombstone:
            raise PolicyIntegrityError(f"tombstone mismatch for {identity}")

    for row in metadata_rows:
        identity = (str(row["key"]), int(row["generation"]))
        shard_path = str(row["shard_path"])
        expected_hash = shard_hash_by_path.get(shard_path)
        if expected_hash is not None:
            shard_file = package_dir / shard_path
            actual_hash = sha256_file(shard_file)
            if actual_hash != expected_hash or str(row["shard_hash"]) != expected_hash:
                raise PolicyIntegrityError(f"metadata shard hash mismatch for {identity}")


def _validate_key_generation_monotonic(metadata_rows: list[dict[str, Any]]) -> None:
    by_key: dict[str, list[dict[str, Any]]] = {}
    for row in metadata_rows:
        by_key.setdefault(str(row["key"]), []).append(row)
    for key, rows in by_key.items():
        ordered = sorted(rows, key=lambda row: (int(row["effective_at_ms"]), int(row["generation"])))
        generations = [int(row["generation"]) for row in ordered]
        if any(current < previous for previous, current in zip(generations, generations[1:])):
            raise PolicyIntegrityError(f"policy generation decreases for key {key!r}")
