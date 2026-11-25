"""Deterministic sample generation for the proxy diagnostics workbench."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

from .jsonio import canonical_hash, sha256_file, write_json_atomic
from .paths import Paths

SEED = 43420261126
BASE_ABSOLUTE_MS = 1767225600000
CLOCK_WATERMARK_MS = 42000


def _checksum(connection_id: str, rng: np.random.Generator) -> int:
    codes = np.frombuffer(connection_id.encode("utf-8"), dtype=np.uint8).astype(np.int64)
    weights = np.arange(1, codes.size + 1, dtype=np.int64)
    padded = rng.integers(1, 251, size=8, dtype=np.int64)
    return int((np.dot(codes, weights) + int(np.sum(padded))) % 1_000_003)


def response_digest(connection_id: str, hop: int, status: int, checksum: int) -> str:
    return canonical_hash(
        {
            "connection_id": connection_id,
            "hop": hop,
            "status": status,
            "seed": SEED,
            "checksum": checksum,
        }
    )


def _headers(connection_id: str, route_key: str, table_version: str, hop: int, clock_ms: int) -> dict[str, str]:
    return {
        "X-Connection-Id": connection_id,
        "X-Hop": str(hop),
        "X-Logical-Clock": str(clock_ms),
        "X-Route-Key": route_key,
        "X-Seed": "434-fixed",
        "X-Table-Version": table_version,
    }


def make_specs() -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = [
        {"id": "conn-alpha", "route": "route.alpha", "kind": "success", "hops": 2, "v1": True, "v2": True},
        {"id": "conn-bravo", "route": "route.bravo", "kind": "success", "hops": 2, "v1": True, "v2": True},
        {"id": "conn-charlie", "route": "route.charlie", "kind": "upstream_failure", "hops": 2, "v1": True, "v2": True},
        {"id": "conn-delta", "route": "route.delta", "kind": "success", "hops": 1, "v1": True, "v2": False},
        {"id": "conn-echo", "route": "route.echo", "kind": "success", "hops": 2, "v1": False, "v2": True},
        {"id": "conn-foxtrot", "route": "route.foxtrot", "kind": "base_tombstone", "hops": 2, "v1": True, "v2": True},
        {"id": "conn-golf", "route": "route.golf", "kind": "success", "hops": 2, "v1": True, "v2": True, "depends_on": ["route.alpha"]},
        {"id": "conn-hotel", "route": "route.hotel", "kind": "success", "hops": 3, "v1": True, "v2": True},
        {"id": "conn-format-tie", "route": "route.tie", "kind": "clock_tie", "hops": 2, "v1": True, "v2": True},
    ]
    for index in range(8):
        specs.append(
            {
                "id": f"conn-parallel-{index:02d}",
                "route": f"route.parallel.{index:02d}",
                "kind": "slow" if index == 0 else "success",
                "hops": 2,
                "v1": True,
                "v2": True,
            }
        )
    return specs


def make_entry(
    route_key: str,
    version: str,
    *,
    depends_on: list[str] | None = None,
    tombstone: bool = False,
    weight: int = 5,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "route_key": route_key,
        "target_ref": f"loopback://mock-target/{route_key}",
        "depends_on": sorted(depends_on or []),
        "weight": weight,
        "tombstone": tombstone,
        "table_version": version,
    }
    entry["entry_digest"] = canonical_hash(
        {k: v for k, v in entry.items() if k not in {"entry_digest", "table_version"}}
    )
    return entry


def make_tables(specs: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    routes = {spec["route"]: spec.get("depends_on", []) for spec in specs}
    v1_entries: list[dict[str, Any]] = []
    v2_entries: list[dict[str, Any]] = []
    for route in sorted(routes):
        depends_on = routes[route]
        if route != "route.echo":
            v1_entries.append(make_entry(route, "ft-100", depends_on=depends_on))
        if route == "route.alpha":
            v2_entries.append(make_entry(route, "ft-103", depends_on=depends_on, weight=8))
        elif route == "route.foxtrot":
            v2_entries.append(make_entry(route, "ft-103", depends_on=depends_on, tombstone=True, weight=0))
        elif route == "route.echo":
            v2_entries.append(make_entry(route, "ft-103", depends_on=depends_on, weight=6))
        else:
            v2_entries.append(make_entry(route, "ft-103", depends_on=depends_on, weight=5))
    v1_table = {"format": "forward-table", "version": "ft-100", "entries": v1_entries}
    v2_table = {"format": "forward-table", "version": "ft-103", "entries": v2_entries}
    return v1_table, v2_table


def make_plan(specs: list[dict[str, Any]]) -> dict[str, Any]:
    rng = np.random.default_rng(SEED)
    calls: dict[str, Any] = {}
    for spec in specs:
        checksum = _checksum(spec["id"], rng)
        hops: dict[str, Any] = {}
        for hop in range(1, int(spec["hops"]) + 1):
            if spec["kind"] == "upstream_failure" and hop == 2:
                status = 502
            else:
                status = 200
            hops[str(hop)] = {
                "status": status,
                "digest": response_digest(spec["id"], hop, status, checksum),
                "checksum": checksum,
                "delay_ms": 350 if spec["kind"] == "slow" and hop == 1 else 0,
            }
        calls[spec["id"]] = {"route_key": spec["route"], "hops": hops}
    return {"format": "loopback-proxy-plan", "seed": SEED, "host": "127.0.0.1", "calls": calls}


def _record_set(spec: dict[str, Any], table_version: str, fmt: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    sequence = 1
    hops = int(spec["hops"])
    if spec["kind"] == "clock_tie":
        start_clock = 9000
        start_second = 9
        clock_step = 1000
    else:
        start_clock = 20000 + (int(spec_index(spec["id"])) * 37)
        start_second = 0
        clock_step = 500

    def add(direction: str, hop: int, index: int) -> None:
        nonlocal sequence
        clock_ms = start_clock + index * clock_step
        record: dict[str, Any] = {
            "seq": sequence,
            "direction": direction,
            "hop": hop,
            "request_status_code": 202 if direction == "forward" else 200,
            "response_status_code": 502 if spec["kind"] == "upstream_failure" and hop == 2 else 200,
            "request_headers": _headers(spec["id"], spec["route"], table_version, hop, clock_ms),
        }
        if fmt == "v1":
            record["record_second"] = start_second + index
        else:
            record["logical_clock_ms"] = clock_ms
            record["absolute_ms"] = BASE_ABSOLUTE_MS + clock_ms
        records.append(record)
        sequence += 1

    index = 0
    for hop in range(1, hops + 1):
        add("forward", hop, index)
        index += 1
    for hop in range(hops, 0, -1):
        add("reverse", hop, index)
        index += 1
    return records


def spec_index(connection_id: str) -> int:
    digits = "".join(ch for ch in connection_id if ch.isdigit())
    if digits:
        return int(digits)
    return sum((index + 1) * ord(char) for index, char in enumerate(connection_id)) % 97


def make_v1(specs: list[dict[str, Any]], table: dict[str, Any]) -> dict[str, Any]:
    entries_by_route = {entry["route_key"]: entry for entry in table["entries"]}
    sessions: list[dict[str, Any]] = []
    for spec in specs:
        if not spec["v1"]:
            continue
        route = spec["route"]
        session: dict[str, Any] = {
            "connection_id": spec["id"],
            "session_generation": 2 if spec["kind"] == "clock_tie" else 1,
            "table_version": table["version"],
            "table_refs": [route],
            "forward_table_entries": [entries_by_route[route]],
            "records": _record_set(spec, table["version"], "v1"),
        }
        sessions.append(session)
    return {
        "format": "proxy-log-v1",
        "schema_version": "1.0",
        "export_id": "v1-seed-434",
        "relative_time_field": "record_second",
        "table_version": table["version"],
        "flat_forward_table": table["entries"],
        "sessions": sessions,
    }


def make_v2_sessions(specs: list[dict[str, Any]], table_version: str, manifest_version: int = 1) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for spec in specs:
        if not spec["v2"]:
            continue
        if manifest_version >= 3 and spec["id"] == "conn-alpha":
            spec = {**spec, "kind": "success"}
        generation = 3 if manifest_version >= 3 and spec["id"] == "conn-alpha" else (2 if spec["kind"] != "clock_tie" else 2)
        records = _record_set(spec, table_version, "v2")
        if manifest_version >= 3 and spec["id"] == "conn-alpha":
            for offset, record in enumerate(records):
                record["logical_clock_ms"] = 40000 + offset * 500
                record["absolute_ms"] = BASE_ABSOLUTE_MS + record["logical_clock_ms"]
                record["request_headers"]["X-Logical-Clock"] = str(record["logical_clock_ms"])
        logical_clock = max(int(record["logical_clock_ms"]) for record in records)
        sessions.append(
            {
                "connection_id": spec["id"],
                "generation": generation,
                "table_version": table_version,
                "table_refs": [spec["route"]],
                "logical_clock_ms": logical_clock,
                "absolute_ms": BASE_ABSOLUTE_MS + logical_clock,
                "records": records,
            }
        )
    return sorted(sessions, key=lambda item: item["connection_id"])


def write_metadata_db(
    db_path: Path,
    sessions: list[dict[str, Any]],
    tombstones: list[dict[str, Any]],
    table_versions: list[dict[str, str]],
    manifest_version: int,
) -> None:
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE sessions (
                connection_id TEXT PRIMARY KEY,
                generation INTEGER NOT NULL,
                logical_clock_ms INTEGER NOT NULL,
                absolute_ms INTEGER NOT NULL,
                table_version TEXT NOT NULL,
                shard_id TEXT NOT NULL
            );
            CREATE TABLE session_generation_history (
                history_seq INTEGER NOT NULL,
                connection_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                logical_clock_ms INTEGER NOT NULL,
                absolute_ms INTEGER NOT NULL,
                table_version TEXT NOT NULL,
                manifest_version INTEGER NOT NULL,
                PRIMARY KEY(connection_id, history_seq)
            );
            CREATE TABLE tombstones (
                tombstone_id TEXT PRIMARY KEY,
                route_key TEXT NOT NULL,
                table_version TEXT NOT NULL,
                observed_absolute_ms INTEGER NOT NULL,
                op TEXT NOT NULL,
                is_tombstone INTEGER NOT NULL,
                reason TEXT NOT NULL
            );
            CREATE TABLE table_versions (
                version TEXT PRIMARY KEY,
                digest TEXT NOT NULL,
                entry_count INTEGER NOT NULL
            );
            """
        )
        connection.execute("INSERT INTO meta VALUES ('schema','proxy-v2-metadata-1')")
        for table in table_versions:
            connection.execute(
                "INSERT INTO table_versions VALUES (?, ?, ?)",
                (table["version"], table["digest"], int(table["entry_count"])),
            )
        history_seq = 0
        for session in sessions:
            shard_id = session.get("_shard_id", "shard-000")
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session["connection_id"],
                    session["generation"],
                    session["logical_clock_ms"],
                    session["absolute_ms"],
                    session["table_version"],
                    shard_id,
                ),
            )
            if manifest_version >= 3 and session["connection_id"] == "conn-alpha":
                prior = dict(session)
                prior["generation"] = 2
                prior["logical_clock_ms"] = 21500
                prior["absolute_ms"] = BASE_ABSOLUTE_MS + 21500
                connection.execute(
                    "INSERT INTO session_generation_history VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        history_seq,
                        prior["connection_id"],
                        prior["generation"],
                        prior["logical_clock_ms"],
                        prior["absolute_ms"],
                        prior["table_version"],
                        max(1, manifest_version - 1),
                    ),
                )
                history_seq += 1
            connection.execute(
                "INSERT INTO session_generation_history VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    history_seq,
                    session["connection_id"],
                    session["generation"],
                    session["logical_clock_ms"],
                    session["absolute_ms"],
                    session["table_version"],
                    manifest_version,
                ),
            )
            history_seq += 1
        for tombstone in tombstones:
            connection.execute(
                "INSERT INTO tombstones VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    tombstone["tombstone_id"],
                    tombstone["route_key"],
                    tombstone["table_version"],
                    tombstone["observed_absolute_ms"],
                    tombstone["op"],
                    1 if tombstone["is_tombstone"] else 0,
                    tombstone["reason"],
                ),
            )
        connection.commit()
    finally:
        connection.close()


def write_v2_bundle(
    bundle_dir: Path,
    shard_sessions: list[list[dict[str, Any]]],
    *,
    package_id: str,
    manifest_version: int,
    generation_floor: int,
    table_version: str,
    tombstones: list[dict[str, Any]],
    table_digests: dict[str, str],
) -> Path:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    shard_dir = bundle_dir / "shards"
    shard_dir.mkdir(exist_ok=True)
    manifest_shards: list[dict[str, Any]] = []
    all_sessions: list[dict[str, Any]] = []
    for index, sessions in enumerate(shard_sessions):
        shard_id = f"shard-{index:03d}"
        for session in sessions:
            session["_shard_id"] = shard_id
        shard_path = shard_dir / f"{shard_id}.json"
        payload = {"format": "proxy-log-v2-shard", "shard_id": shard_id, "sessions": sessions}
        write_json_atomic(shard_path, payload)
        manifest_shards.append(
            {
                "shard_id": shard_id,
                "path": f"shards/{shard_id}.json",
                "sha256": sha256_file(shard_path),
                "session_count": len(sessions),
            }
        )
        all_sessions.extend(sessions)
    db_path = bundle_dir / "metadata.sqlite3"
    write_metadata_db(
        db_path,
        all_sessions,
        tombstones,
        [{"version": version, "digest": digest, "entry_count": 0} for version, digest in table_digests.items()],
        manifest_version,
    )
    manifest = {
        "format": "proxy-log-v2-manifest",
        "package_id": package_id,
        "manifest_version": manifest_version,
        "session_generation_floor": generation_floor,
        "metadata_db": "metadata.sqlite3",
        "metadata_sha256": sha256_file(db_path),
        "table_version": table_version,
        "table_digests": table_digests,
        "shards": manifest_shards,
    }
    manifest_path = bundle_dir / "v2_manifest.json"
    write_json_atomic(manifest_path, manifest)
    return manifest_path


def build_patch(clock_ms: int = 25000) -> dict[str, Any]:
    alpha_new = make_entry(
        "route.alpha",
        "ft-104",
        depends_on=[],
        weight=11,
    )
    return {
        "format": "forward-table-patch",
        "patch_id": "patch-2026-0001",
        "base_version": "ft-103",
        "new_version": "ft-104",
        "logical_clock_ms": clock_ms,
        "operations": [
            {"op": "add", "key": "route.november", "entry": make_entry("route.november", "ft-104", weight=2)},
            {
                "op": "modify",
                "key": "route.alpha",
                "expected_digest": next(
                    entry["entry_digest"]
                    for entry in make_tables(make_specs())[1]["entries"]
                    if entry["route_key"] == "route.alpha"
                ),
                "entry": alpha_new,
            },
            {
                "op": "tombstone",
                "key": "route.bravo",
                "tombstone": {
                    "tombstone_id": "tomb-bravo-20260001",
                    "route_key": "route.bravo",
                    "table_version": "ft-104",
                    "observed_absolute_ms": BASE_ABSOLUTE_MS + clock_ms,
                    "op": "tombstone",
                    "is_tombstone": True,
                    "reason": "route-retired-by-seed-patch",
                },
            },
        ],
    }


def generate(root: str | Path | None = None) -> Paths:
    paths = Paths.open(root)
    samples = paths.samples
    for directory in (samples / "v1", samples / "v2", samples / "tables", samples / "patches", samples / "packages"):
        directory.mkdir(parents=True, exist_ok=True)

    specs = make_specs()
    v1_table, v2_table = make_tables(specs)
    plan = make_plan(specs)
    write_json_atomic(samples / "tables" / "v1_forward_table.json", v1_table)
    write_json_atomic(samples / "tables" / "v2_forward_table.json", v2_table)
    write_json_atomic(samples / "test_service_plan.json", plan)

    clock = {
        "clock_id": "inject-lc-434-fixed",
        "base_absolute_ms": BASE_ABSOLUTE_MS,
        "v1_origin_logical_ms": 0,
        "scale_numerator": 1000,
        "scale_denominator": 1,
        "rounding": "nearest",
        "watermark_ms": CLOCK_WATERMARK_MS,
    }
    write_json_atomic(samples / "logical_clock.json", clock)

    v1 = make_v1(specs, v1_table)
    v1_path = samples / "v1" / "proxy_log_v1.json"
    write_json_atomic(v1_path, v1)

    v2_sessions = make_v2_sessions(specs, "ft-103", 1)
    core_ids = {"conn-alpha", "conn-bravo", "conn-charlie", "conn-foxtrot"}
    core = [session for session in v2_sessions if session["connection_id"] in core_ids]
    rest = [session for session in v2_sessions if session["connection_id"] not in core_ids]
    tombstone = {
        "tombstone_id": "tomb-foxtrot-seed",
        "route_key": "route.foxtrot",
        "table_version": "ft-103",
        "observed_absolute_ms": BASE_ABSOLUTE_MS + 20500,
        "op": "tombstone",
        "is_tombstone": True,
        "reason": "seed-retired-route",
    }
    v2_digest = canonical_hash(v2_table["entries"])
    write_v2_bundle(
        samples / "v2",
        [core, rest],
        package_id="pkg-base-seed",
        manifest_version=1,
        generation_floor=1,
        table_version="ft-103",
        tombstones=[tombstone],
        table_digests={"ft-103": v2_digest},
    )

    patch = build_patch()
    write_json_atomic(samples / "patches" / "forward_table_patch_0001.json", patch)

    incremental_specs = [*specs, {"id": "conn-kilo", "route": "route.echo", "kind": "success", "hops": 1, "v1": False, "v2": True}]
    incremental_sessions = make_v2_sessions(incremental_specs, "ft-103", 2)
    inc_core = [session for session in incremental_sessions if session["connection_id"] in core_ids]
    inc_rest = [session for session in incremental_sessions if session["connection_id"] not in core_ids and session["connection_id"] != "conn-kilo"]
    kilo = [session for session in incremental_sessions if session["connection_id"] == "conn-kilo"]
    write_v2_bundle(
        samples / "packages_data" / "incremental_v2",
        [inc_core, inc_rest, kilo],
        package_id="pkg-incremental-kilo",
        manifest_version=2,
        generation_floor=1,
        table_version="ft-103",
        tombstones=[tombstone],
        table_digests={"ft-103": v2_digest},
    )

    replace_specs = [*specs, {"id": "conn-kilo", "route": "route.echo", "kind": "success", "hops": 1, "v1": False, "v2": True}]
    replace_sessions = make_v2_sessions(replace_specs, "ft-103", 3)
    write_v2_bundle(
        samples / "packages_data" / "replace_v2",
        [replace_sessions],
        package_id="pkg-replace-generation-3",
        manifest_version=3,
        generation_floor=3,
        table_version="ft-103",
        tombstones=[tombstone],
        table_digests={"ft-103": v2_digest},
    )

    package = {
        "format": "proxy-log-package",
        "package_id": "pkg-base-seed",
        "manifest_version": 1,
        "clock_file": "logical_clock.json",
        "v1_file": "v1/proxy_log_v1.json",
        "v2_manifest": "v2/v2_manifest.json",
        "v1_table": "tables/v1_forward_table.json",
        "v2_table": "tables/v2_forward_table.json",
        "test_plan": "test_service_plan.json",
    }
    write_json_atomic(samples / "current_package.json", package)
    incremental_package = {
        "format": "proxy-log-package",
        "package_id": "pkg-incremental-kilo",
        "manifest_version": 2,
        "clock_file": "../logical_clock.json",
        "v1_file": "../v1/proxy_log_v1.json",
        "v2_manifest": "../packages_data/incremental_v2/v2_manifest.json",
        "v1_table": "../tables/v1_forward_table.json",
        "v2_table": "../tables/v2_forward_table.json",
        "test_plan": "../test_service_plan.json",
    }
    write_json_atomic(samples / "packages" / "incremental_package.json", incremental_package)
    replace_package = {
        **incremental_package,
        "package_id": "pkg-replace-generation-3",
        "manifest_version": 3,
        "v2_manifest": "../packages_data/replace_v2/v2_manifest.json",
    }
    write_json_atomic(samples / "packages" / "replace_package.json", replace_package)

    expectations = {
        "initial_success_examples": ["conn-alpha", "conn-delta", "conn-hotel"],
        "initial_failed": ["conn-charlie"],
        "initial_skipped": ["conn-foxtrot"],
        "patch_affected_sessions": ["conn-alpha", "conn-bravo", "conn-golf"],
        "patch_direct_keys": ["route.alpha", "route.bravo", "route.november"],
        "patch_closure_keys": ["route.alpha", "route.bravo", "route.golf", "route.november"],
        "patch_newly_skipped": ["conn-bravo"],
        "incremental_new_session": "conn-kilo",
        "replace_generation_floor": 3,
    }
    write_json_atomic(samples / "expectations.json", expectations)
    return paths


def main() -> int:
    paths = generate()
    print(f"generated deterministic samples under {paths.samples}")
    return 0
