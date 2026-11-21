"""Parse and merge v1 relative and v2 sharded cold-start observations."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .errors import bad
from .models import Observation, SnapshotMerge
from .util import (
    as_nonnegative_int,
    as_positive_int,
    read_json,
    require_list,
    require_object,
    require_string,
)


def _round_half_up_ratio(value: int, numerator: int, denominator: int) -> int:
    scaled = value * numerator
    quotient, remainder = divmod(scaled, denominator)
    if remainder * 2 >= denominator:
        quotient += 1
    return quotient


def _parse_observation_base(item: Any, path: str, source: str) -> dict[str, Any]:
    obj = require_object(item, path, "E_BAD_SNAPSHOT")
    return {
        "function": require_string(obj.get("function"), f"{path}.function", "E_BAD_SNAPSHOT"),
        "warm_generation": as_nonnegative_int(
            obj.get("warm_generation"), f"{path}.warm_generation", "E_BAD_SNAPSHOT"
        ),
        "warm_hit": _require_bool(
            obj.get("warm_hit"), f"{path}.warm_hit", "E_BAD_SNAPSHOT"
        ),
        "observed_clock_ms": as_nonnegative_int(
            obj.get("observed_clock_ms", 0),
            f"{path}.observed_clock_ms",
            "E_BAD_SNAPSHOT",
        ),
        "source": source,
    }


def _require_bool(value: Any, path: str, code: str) -> bool:
    if not isinstance(value, bool):
        raise bad(code, "expected a boolean", path)
    return value


def parse_v1_snapshot(path: str, known_functions: set[str]) -> list[Observation]:
    payload = require_object(read_json(path), "$", "E_BAD_SNAPSHOT")
    if payload.get("schema_version") != 1:
        raise bad("E_BAD_SNAPSHOT", "v1 snapshot must declare schema_version 1", "$.schema_version")
    clock = require_object(payload.get("clock"), "$.clock", "E_BAD_SNAPSHOT")
    base_ms = as_nonnegative_int(clock.get("base_ms", 0), "$.clock.base_ms", "E_BAD_SNAPSHOT")
    scale_num = as_positive_int(
        clock.get("scale_num"), "$.clock.scale_num", "E_BAD_SNAPSHOT"
    )
    scale_den = as_positive_int(
        clock.get("scale_den"), "$.clock.scale_den", "E_BAD_SNAPSHOT"
    )
    rounding = require_string(clock.get("rounding"), "$.clock.rounding", "E_BAD_SNAPSHOT")
    if rounding != "half_up":
        raise bad(
            "E_BAD_SNAPSHOT",
            "only deterministic half_up rounding is supported",
            "$.clock.rounding",
        )
    observations: list[Observation] = []
    seen: set[str] = set()
    for index, item in enumerate(
        require_list(payload.get("observations"), "$.observations", "E_BAD_SNAPSHOT")
    ):
        item_path = f"$.observations[{index}]"
        base = _parse_observation_base(item, item_path, "v1")
        function = base["function"]
        if function not in known_functions:
            raise bad(
                "E_BAD_SNAPSHOT",
                f"snapshot references unknown function: {function}",
                f"{item_path}.function",
            )
        if function in seen:
            raise bad(
                "E_BAD_SNAPSHOT", f"duplicate v1 observation for {function}", item_path
            )
        seen.add(function)
        raw = require_object(item, item_path, "E_BAD_SNAPSHOT")
        relative_init_ms = as_nonnegative_int(
            raw.get("relative_init_ms"),
            f"{item_path}.relative_init_ms",
            "E_BAD_SNAPSHOT",
        )
        converted = base_ms + _round_half_up_ratio(
            relative_init_ms, scale_num, scale_den
        )
        observations.append(
            Observation(
                function=function,
                source="v1",
                absolute_init_ms=converted,
                observed_clock_ms=base["observed_clock_ms"],
                warm_generation=base["warm_generation"],
                warm_hit=base["warm_hit"],
                hit_watermark=0,
                scale_num=scale_num,
                scale_den=scale_den,
                rounding=rounding,
                conversion_error_bound_ms=0.5,
                selection_reason="v1 relative value converted to absolute logical time",
            )
        )
    return observations


def parse_v2_snapshot(
    index_path: str, known_functions: set[str]
) -> tuple[list[Observation], dict[str, Any]]:
    index_file = Path(index_path)
    payload = require_object(read_json(index_file), "$", "E_BAD_SNAPSHOT")
    if payload.get("schema_version") != 2:
        raise bad("E_BAD_SNAPSHOT", "v2 snapshot must declare schema_version 2", "$.schema_version")
    metadata_name = require_string(
        payload.get("metadata_db"), "$.metadata_db", "E_BAD_SNAPSHOT"
    )
    metadata_path = (index_file.parent / metadata_name).resolve()
    shard_names = require_list(payload.get("shards"), "$.shards", "E_BAD_SNAPSHOT")
    if not shard_names:
        raise bad("E_BAD_SNAPSHOT", "v2 snapshot must contain at least one shard", "$.shards")

    metadata: dict[str, dict[str, Any]] = {}
    try:
        connection = sqlite3.connect(f"file:{metadata_path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT function, logical_clock_ms, warm_generation,
                       hit_watermark, shard_id
                FROM snapshot_metadata
                ORDER BY function
                """
            ).fetchall()
        finally:
            connection.close()
    except (sqlite3.Error, OSError) as exc:
        raise bad(
            "E_BAD_SNAPSHOT",
            f"cannot read v2 metadata database: {exc}",
            str(metadata_path),
        )
    for row in rows:
        function = row["function"]
        if function in metadata:
            raise bad("E_BAD_SNAPSHOT", f"duplicate v2 metadata row for {function}", "$.metadata_db")
        metadata[function] = {
            "logical_clock_ms": as_nonnegative_int(
                row["logical_clock_ms"], "$.metadata_db.logical_clock_ms", "E_BAD_SNAPSHOT"
            ),
            "warm_generation": as_nonnegative_int(
                row["warm_generation"], "$.metadata_db.warm_generation", "E_BAD_SNAPSHOT"
            ),
            "hit_watermark": as_nonnegative_int(
                row["hit_watermark"], "$.metadata_db.hit_watermark", "E_BAD_SNAPSHOT"
            ),
            "shard_id": row["shard_id"],
        }

    observations: list[Observation] = []
    seen: set[str] = set()
    shard_payloads: list[dict[str, Any]] = []
    for shard_index, shard_name_raw in enumerate(shard_names):
        shard_name = require_string(
            shard_name_raw, f"$.shards[{shard_index}]", "E_BAD_SNAPSHOT"
        )
        shard_path = (index_file.parent / shard_name).resolve()
        shard = require_object(read_json(shard_path), "$", "E_BAD_SNAPSHOT")
        if shard.get("schema_version") != 2:
            raise bad(
                "E_BAD_SNAPSHOT",
                "v2 shard must declare schema_version 2",
                str(shard_path),
            )
        shard_id = require_string(shard.get("shard_id"), f"{shard_path}.shard_id", "E_BAD_SNAPSHOT")
        shard_payloads.append({"file": shard_name, "shard_id": shard_id})
        for obs_index, item in enumerate(
            require_list(shard.get("observations"), f"{shard_path}.observations", "E_BAD_SNAPSHOT")
        ):
            item_path = f"{shard_path}.observations[{obs_index}]"
            base = _parse_observation_base(item, item_path, "v2")
            function = base["function"]
            if function not in known_functions:
                raise bad(
                    "E_BAD_SNAPSHOT",
                    f"snapshot references unknown function: {function}",
                    f"{item_path}.function",
                )
            if function in seen:
                raise bad(
                    "E_BAD_SNAPSHOT", f"duplicate v2 observation for {function}", item_path
                )
            seen.add(function)
            if function not in metadata:
                raise bad(
                    "E_BAD_SNAPSHOT",
                    f"v2 shard observation has no metadata row: {function}",
                    item_path,
                )
            meta = metadata[function]
            if meta["shard_id"] != shard_id:
                raise bad(
                    "E_BAD_SNAPSHOT",
                    f"metadata shard mismatch for {function}: {meta['shard_id']} != {shard_id}",
                    item_path,
                )
            raw = require_object(item, item_path, "E_BAD_SNAPSHOT")
            absolute_init_ms = as_nonnegative_int(
                raw.get("absolute_init_ms"),
                f"{item_path}.absolute_init_ms",
                "E_BAD_SNAPSHOT",
            )
            if base["observed_clock_ms"] != meta["logical_clock_ms"]:
                raise bad(
                    "E_BAD_SNAPSHOT",
                    "shard logical clock disagrees with metadata",
                    f"{item_path}.observed_clock_ms",
                )
            if base["warm_generation"] != meta["warm_generation"]:
                raise bad(
                    "E_BAD_SNAPSHOT",
                    "shard warm generation disagrees with metadata",
                    f"{item_path}.warm_generation",
                )
            expected_hit = meta["hit_watermark"] > 0
            if base["warm_hit"] != expected_hit:
                raise bad(
                    "E_BAD_SNAPSHOT",
                    "shard hit marker disagrees with hit watermark",
                    f"{item_path}.warm_hit",
                )
            observations.append(
                Observation(
                    function=function,
                    source="v2",
                    absolute_init_ms=absolute_init_ms,
                    observed_clock_ms=base["observed_clock_ms"],
                    warm_generation=base["warm_generation"],
                    warm_hit=base["warm_hit"],
                    hit_watermark=meta["hit_watermark"],
                    scale_num=1,
                    scale_den=1,
                    rounding="none",
                    conversion_error_bound_ms=0.0,
                    selection_reason="v2 absolute logical timestamp",
                )
            )
    missing = sorted(set(metadata) - seen)
    if missing:
        raise bad(
            "E_BAD_SNAPSHOT",
            f"v2 metadata rows missing shard observations: {missing}",
            "$.metadata_db",
        )
    v2_payload = {
        "schema_version": 2,
        "index": index_file.name,
        "metadata_db": metadata_name,
        "shards": shard_payloads,
        "metadata": [
            {
                "function": function,
                **meta,
            }
            for function, meta in sorted(metadata.items())
        ],
    }
    return observations, v2_payload


def merge_snapshots(
    v1_path: str,
    v2_index_path: str,
    known_functions: set[str],
) -> SnapshotMerge:
    v1_observations = parse_v1_snapshot(v1_path, known_functions)
    v2_observations, v2_payload = parse_v2_snapshot(v2_index_path, known_functions)
    v1_payload = read_json(v1_path)

    by_source = {
        "v1": {obs.function: obs for obs in v1_observations},
        "v2": {obs.function: obs for obs in v2_observations},
    }
    source_rank = {"v1": 1, "v2": 2}
    merged: dict[str, Observation] = {}
    for function in sorted(set(by_source["v1"]) | set(by_source["v2"])):
        candidates = [
            by_source[source][function]
            for source in ("v1", "v2")
            if function in by_source[source]
        ]
        chosen = max(
            candidates,
            key=lambda obs: (
                obs.observed_clock_ms,
                obs.warm_generation,
                source_rank[obs.source],
            ),
        )
        if len(candidates) == 1:
            reason = f"only {chosen.source} observed the function"
        else:
            reason = (
                "selected by (logical_clock_ms, warm_generation, source_rank); "
                f"clock={chosen.observed_clock_ms}, generation={chosen.warm_generation}, "
                f"source={chosen.source}"
            )
        merged[function] = Observation(
            function=chosen.function,
            source=chosen.source,
            absolute_init_ms=chosen.absolute_init_ms,
            observed_clock_ms=chosen.observed_clock_ms,
            warm_generation=chosen.warm_generation,
            warm_hit=chosen.warm_hit,
            hit_watermark=chosen.hit_watermark,
            scale_num=chosen.scale_num,
            scale_den=chosen.scale_den,
            rounding=chosen.rounding,
            conversion_error_bound_ms=chosen.conversion_error_bound_ms,
            selection_reason=reason,
        )

    migrations: list[dict[str, Any]] = []
    for obs in v1_observations:
        migrations.append(
            {
                "function": obs.function,
                "from_source": "v1",
                "to_source": "absolute_clock",
                "relative_init_ms": next(
                    int(item["relative_init_ms"])
                    for item in v1_payload["observations"]
                    if item["function"] == obs.function
                ),
                "base_clock_ms": int(v1_payload["clock"].get("base_ms", 0)),
                "scale_num": obs.scale_num,
                "scale_den": obs.scale_den,
                "rounding": obs.rounding,
                "absolute_init_ms": obs.absolute_init_ms,
                "rounding_error_bound_ms": obs.conversion_error_bound_ms,
                "warm_hit_before": obs.warm_hit,
                "warm_hit_after": obs.warm_hit,
                "logical_warmed_preserved": True,
            }
        )

    return SnapshotMerge(
        observations=merged,
        migrations=tuple(migrations),
        v1_payload=v1_payload,
        v2_payload=v2_payload,
    )
