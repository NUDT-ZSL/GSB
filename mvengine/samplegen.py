"""Deterministic local sample generation for CLI demos and acceptance tests."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .definitions import parse_v1, parse_v2
from .logs import parse_logs
from .merge import merge_definitions
from .runtime import replay
from .util import canonical_json, digest_file, digest_json, pretty_json


BASE_SCHEMA_V1 = {
    "inventory": {
        "columns": ["quantity", "sku"],
        "types": {"quantity": "number", "sku": "string"},
    },
    "sales": {
        "columns": ["amount", "id", "region", "units"],
        "types": {
            "amount": "number",
            "id": "string",
            "region": "string",
            "units": "number",
        },
    },
}

BASE_SCHEMA_V2 = {
    "sales": {
        "columns": ["amount", "id", "region", "units"],
        "types": {
            "amount": "number",
            "id": "string",
            "region": "string",
            "units": "number",
        },
    },
}

AGGREGATE_RULES = [
    {"column": "*", "function": "count"},
    {"column": "amount", "function": "sum"},
    {"column": "amount", "function": "sum_sq"},
]

V1_VIEWS = [
    {
        "aggregate": {"column": "amount", "functions": ["count", "sum", "sum_sq"]},
        "base_table": "sales",
        "filter": 'region == "east" and amount >= 10.0',
        "name": "sales_east",
        "refresh_seconds": 30,
    },
    {
        "aggregate": {"column": "amount", "functions": ["count", "sum", "sum_sq"]},
        "base_table": "sales",
        "filter": 'region == "west"',
        "name": "legacy_west",
        "refresh_seconds": 60,
    },
    {
        "aggregate": {"column": "quantity", "functions": ["count", "sum", "sum_sq"]},
        "base_table": "inventory",
        "filter": "quantity > 0",
        "name": "inventory_total",
        "refresh_seconds": 45,
    },
]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pretty_json(value) + "\n", encoding="utf-8")


def _shard_view(
    *,
    name: str,
    base_tables: list[str],
    filter_text: str,
    rules: list[dict[str, str]],
    upstream: list[str],
) -> dict[str, Any]:
    return {
        "aggregate_rules": rules,
        "base_tables": base_tables,
        "filter": filter_text,
        "name": name,
        "upstream_views": upstream,
    }


def _spec_digest(view: dict[str, Any]) -> str:
    return digest_json(
        {
            "aggregate_rules": view["aggregate_rules"],
            "base_tables": view["base_tables"],
            "filter": view["filter"],
            "name": view["name"],
            "upstream_views": view["upstream_views"],
        }
    )


def _v2_views(variant: str, v1_views: list[dict[str, Any]]) -> list[dict[str, Any]]:
    inventory_rules = [
        {"column": "*", "function": "count"},
        {"column": "quantity", "function": "sum"},
        {"column": "quantity", "function": "sum_sq"},
    ]
    views = [
        _shard_view(
            name="sales_east",
            base_tables=["sales"],
            filter_text='region == "east" and amount >= 10.0',
            rules=[dict(rule) for rule in AGGREGATE_RULES],
            upstream=[],
        ),
        _shard_view(
            name="east_summary",
            base_tables=["sales"],
            filter_text='region == "east" and amount >= 10.0',
            rules=[dict(rule) for rule in AGGREGATE_RULES],
            upstream=["sales_east"],
        ),
        _shard_view(
            name="east_dashboard",
            base_tables=["sales"],
            filter_text='region == "east" and amount >= 10.0',
            rules=[dict(rule) for rule in AGGREGATE_RULES],
            upstream=["east_summary"],
        ),
        _shard_view(
            name="north_total",
            base_tables=["sales"],
            filter_text='region == "north"',
            rules=[dict(rule) for rule in AGGREGATE_RULES],
            upstream=[],
        ),
    ]
    if variant == "next":
        views.append(
            _shard_view(
                name="east_high",
                base_tables=["sales"],
                filter_text='region == "east" and amount >= 20.0',
                rules=[dict(rule) for rule in AGGREGATE_RULES],
                upstream=["sales_east"],
            )
        )
    return sorted(views, key=lambda item: item["name"])


def _revision_rows(variant: str) -> list[dict[str, Any]]:
    views = {view["name"]: view for view in _v2_views(variant, V1_VIEWS)}
    rows: list[dict[str, Any]] = []

    def active(
        name: str,
        generation: int,
        clock: int,
        absolute_ms: int,
    ) -> dict[str, Any]:
        view = views[name]
        return {
            "absolute_refresh_ms": absolute_ms,
            "aggregate_rules": view["aggregate_rules"],
            "base_tables": view["base_tables"],
            "generation": generation,
            "logical_clock_ms": clock,
            "shard_file": "shards/views-00.json",
            "spec_digest": _spec_digest(view),
            "status": "active",
            "tombstone_reason": None,
            "tombstoned_at_ms": None,
            "upstream_views": view["upstream_views"],
            "view_name": name,
        }

    rows.extend(
        [
            active("sales_east", 1, 80000, 90000),
            active("sales_east", 2, 100000, 100000),
            active("east_summary", 2, 105000, 105000),
            active("east_dashboard", 3, 106000, 106000),
            active("north_total", 2, 104000, 100000),
        ]
    )
    legacy = _shard_view(
        name="legacy_west",
        base_tables=["sales"],
        filter_text='region == "west"',
        rules=[dict(rule) for rule in AGGREGATE_RULES],
        upstream=[],
    )
    rows.append(
        {
            "absolute_refresh_ms": 90000,
            "aggregate_rules": legacy["aggregate_rules"],
            "base_tables": legacy["base_tables"],
            "generation": 1,
            "logical_clock_ms": 80000,
            "shard_file": "shards/views-00.json",
            "spec_digest": _spec_digest(legacy),
            "status": "active",
            "tombstone_reason": None,
            "tombstoned_at_ms": None,
            "upstream_views": [],
            "view_name": "legacy_west",
        }
    )
    rows.append(
        {
            "absolute_refresh_ms": 120000,
            "aggregate_rules": legacy["aggregate_rules"],
            "base_tables": legacy["base_tables"],
            "generation": 2,
            "logical_clock_ms": 120000,
            "shard_file": None,
            "spec_digest": digest_json({"status": "tombstoned", "view_name": "legacy_west"}),
            "status": "tombstoned",
            "tombstone_reason": "retired_in_v2",
            "tombstoned_at_ms": 120000,
            "upstream_views": [],
            "view_name": "legacy_west",
        }
    )
    if variant == "next":
        rows.append(active("sales_east", 3, 108000, 100000))
        rows.append(active("east_high", 2, 107000, 107000))
    return rows


def _write_v2_metadata(path: Path, rows: list[dict[str, Any]]) -> None:
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(
            """
            CREATE TABLE metadata_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE view_revisions (
                seq INTEGER PRIMARY KEY,
                view_name TEXT NOT NULL,
                generation INTEGER NOT NULL,
                logical_clock_ms INTEGER NOT NULL,
                absolute_refresh_ms INTEGER NOT NULL,
                status TEXT NOT NULL,
                base_tables_json TEXT NOT NULL,
                aggregate_rules_json TEXT NOT NULL,
                upstream_views_json TEXT NOT NULL,
                shard_file TEXT,
                spec_digest TEXT NOT NULL,
                tombstone_reason TEXT,
                tombstoned_at_ms INTEGER,
                UNIQUE(view_name, generation)
            );
            """
        )
        connection.execute(
            "INSERT INTO metadata_state(key, value) VALUES('format', 'v2')"
        )
        connection.execute(
            "INSERT INTO metadata_state(key, value) VALUES('definition_set', 'demo')"
        )
        for index, row in enumerate(rows, start=1):
            connection.execute(
                """
                INSERT INTO view_revisions(
                    seq, view_name, generation, logical_clock_ms,
                    absolute_refresh_ms, status, base_tables_json,
                    aggregate_rules_json, upstream_views_json, shard_file,
                    spec_digest, tombstone_reason, tombstoned_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    index,
                    row["view_name"],
                    row["generation"],
                    row["logical_clock_ms"],
                    row["absolute_refresh_ms"],
                    row["status"],
                    canonical_json(row["base_tables"]),
                    canonical_json(row["aggregate_rules"]),
                    canonical_json(row["upstream_views"]),
                    row["shard_file"],
                    row["spec_digest"],
                    row["tombstone_reason"],
                    row["tombstoned_at_ms"],
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _write_v2(root: Path, variant: str) -> None:
    shard_views = _v2_views(variant, V1_VIEWS)
    shard = {
        "format": "v2-shard",
        "shard_id": "views-00",
        "views": shard_views,
    }
    shard_path = root / "shards" / "views-00.json"
    _write_json(shard_path, shard)
    _write_v2_metadata(root / "metadata.sqlite", _revision_rows(variant))
    manifest = {
        "base_schema": BASE_SCHEMA_V2,
        "definition_set": "demo-v2",
        "format": "v2",
        "logical_clock_ms": 120000,
        "manifest_version": 2,
        "metadata_db": "metadata.sqlite",
        "shards": {"shards/views-00.json": digest_file(shard_path)},
        "view_generation_floor": 2,
    }
    _write_json(root / "manifest.json", manifest)


def _write_v1(root: Path) -> Path:
    path = root / "views.json"
    _write_json(
        path,
        {
            "base_schema": BASE_SCHEMA_V1,
            "base_view_map": {
                "inventory": ["inventory_total"],
                "sales": ["legacy_west", "sales_east"],
            },
            "format": "v1",
            "logical_clock_ms": 100000,
            "views": V1_VIEWS,
        },
    )
    return path


def _base_event(
    seq: int,
    table: str,
    key: str,
    op: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    clock: int,
) -> dict[str, Any]:
    core = {
        "after": after,
        "base_table": table,
        "before": before,
        "key": key,
        "logical_clock_ms": clock,
        "op": op,
        "seq": seq,
    }
    return {**core, "content_digest": digest_json(core)}


def _base_events() -> list[dict[str, Any]]:
    return [
        _base_event(1, "sales", "s1", "insert", None, {"id": "s1", "region": "east", "amount": 25.0, "units": 2}, 100000),
        _base_event(2, "sales", "s2", "insert", None, {"id": "s2", "region": "west", "amount": 40.0, "units": 4}, 101000),
        _base_event(3, "inventory", "i1", "insert", None, {"sku": "i1", "quantity": 5}, 102000),
        _base_event(4, "sales", "s1", "update", {"id": "s1", "region": "east", "amount": 25.0, "units": 2}, {"id": "s1", "region": "east", "amount": 30.0, "units": 2}, 110000),
        _base_event(5, "sales", "s3", "insert", None, {"id": "s3", "region": "north", "amount": 10.0, "units": 1}, 112000),
        _base_event(6, "sales", "s1", "update", {"id": "s1", "region": "east", "amount": 30.0, "units": 2}, {"id": "s1", "region": "east", "amount": 32.0, "units": 2}, 120000),
        _base_event(7, "sales", "s2", "update", {"id": "s2", "region": "west", "amount": 40.0, "units": 4}, {"id": "s2", "region": "west", "amount": 50.0, "units": 4}, 130000),
        _base_event(8, "inventory", "i1", "delete", {"sku": "i1", "quantity": 5}, None, 140000),
        _base_event(9, "sales", "s4", "insert", None, {"id": "s4", "region": "east", "amount": 15.0, "units": 3}, 150000),
        _base_event(10, "sales", "s3", "delete", {"id": "s3", "region": "north", "amount": 10.0, "units": 1}, None, 160000),
        _base_event(11, "sales", "s4", "update", {"id": "s4", "region": "east", "amount": 15.0, "units": 3}, {"id": "s4", "region": "west", "amount": 16.0, "units": 3}, 170000),
        _base_event(12, "inventory", "i2", "insert", None, {"sku": "i2", "quantity": 7}, 180000),
    ]


def _append_events() -> list[dict[str, Any]]:
    return [
        _base_event(13, "sales", "s5", "insert", None, {"id": "s5", "region": "east", "amount": 21.0, "units": 3}, 185000),
        _base_event(14, "inventory", "i2", "update", {"sku": "i2", "quantity": 7}, {"sku": "i2", "quantity": 8}, 190000),
    ]


def _write_segment(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(canonical_json(event) + "\n" for event in events),
        encoding="utf-8",
    )


def _write_logs(root: Path, *, append: bool = False) -> Path:
    base = _base_events()
    segments: list[tuple[str, list[dict[str, Any]]]] = [
        ("seg-0001", base[:6]),
        ("seg-0002", base[6:]),
    ]
    if append:
        segments.append(("seg-0003", _append_events()))
    manifest_segments = []
    for segment_id, events in segments:
        relative = f"segments/{segment_id}.jsonl"
        path = root / relative
        _write_segment(path, events)
        manifest_segments.append(
            {
                "end_seq": events[-1]["seq"],
                "file": relative,
                "segment_id": segment_id,
                "sha256": digest_file(path),
                "start_seq": events[0]["seq"],
            }
        )
    manifest_path = root / "manifest.json"
    _write_json(
        manifest_path,
        {
            "format": "mv-logs-v1",
            "segments": manifest_segments,
        },
    )
    return manifest_path


def _file_hashes(root: Path, v1_path: Path, v2_root: Path, logs_path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    candidates = [
        v1_path,
        v2_root / "manifest.json",
        *sorted((v2_root / "shards").glob("*.json")),
        logs_path,
        *sorted((logs_path.parent / "segments").glob("*.jsonl")),
    ]
    for path in candidates:
        result[path.relative_to(root).as_posix()] = digest_file(path)
    return result


def _write_package(
    root: Path,
    *,
    package_version: int,
    v1_path: Path,
    v2_root: Path,
    logs_path: Path,
    snapshots_root: Path,
    index: Any,
    logical_clock_ms: int,
) -> None:
    _write_json(
        root / "package.json",
        {
            "definition_digest": index.index_digest,
            "definition_paths": {
                "v1": v1_path.relative_to(root).as_posix(),
                "v2": v2_root.relative_to(root).as_posix(),
            },
            "file_hashes": _file_hashes(root, v1_path, v2_root, logs_path),
            "format": "mv-package-v1",
            "logical_clock_ms": logical_clock_ms,
            "logs_path": logs_path.parent.relative_to(root).as_posix(),
            "package_version": package_version,
            "snapshots_path": snapshots_root.relative_to(root).as_posix(),
            "view_generation_floor": 2,
        },
    )


def _build_index(root: Path) -> Any:
    v1 = parse_v1(root / "v1" / "views.json")
    v2 = parse_v2(root / "v2")
    return merge_definitions(v1, v2)


def _write_snapshots(root: Path, index: Any, logs: Any) -> None:
    for seq in (3, 6, 9, 12):
        _, states, _ = replay(index=index, events=logs.events, through_seq=seq)
        snapshot = {
            "definition_digest": index.definition_digest,
            "format": "mv-snapshot-v1",
            "snapshot_version": 1,
            "views": {
                name: state.as_json()
                for name, state in sorted(states.items())
            },
            "watermark_seq": seq,
        }
        _write_json(root / "snapshots" / f"snapshot-{seq:04d}.json", snapshot)


def generate_sample(out: Path) -> None:
    out = out.resolve()
    if out.exists():
        shutil.rmtree(out)
    (out / "v1").mkdir(parents=True)
    (out / "v2").mkdir(parents=True)
    (out / "logs").mkdir(parents=True)
    (out / "snapshots").mkdir(parents=True)

    v1_path = _write_v1(out / "v1")
    _write_v2(out / "v2", "base")
    logs_path = _write_logs(out / "logs")
    index = _build_index(out)
    logs = parse_logs(logs_path)
    _write_snapshots(out, index, logs)
    _write_package(
        out,
        package_version=1,
        v1_path=v1_path,
        v2_root=out / "v2",
        logs_path=logs_path,
        snapshots_root=out / "snapshots",
        index=index,
        logical_clock_ms=180000,
    )


def generate_append(out: Path) -> None:
    out = out.resolve()
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(out.parent / "sample", out)
    logs_path = _write_logs(out / "logs", append=True)
    shutil.rmtree(out / "snapshots")
    shutil.copytree(out.parent / "sample" / "snapshots", out / "snapshots")
    index = _build_index(out)
    logs = parse_logs(logs_path)
    _write_package(
        out,
        package_version=2,
        v1_path=out / "v1" / "views.json",
        v2_root=out / "v2",
        logs_path=logs_path,
        snapshots_root=out / "snapshots",
        index=index,
        logical_clock_ms=190000,
    )


def generate_next(out: Path) -> None:
    out = out.resolve()
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(out.parent / "sample", out)
    _write_v2(out / "v2", "next")
    shutil.rmtree(out / "snapshots")
    (out / "snapshots").mkdir()
    index = _build_index(out)
    logs_path = out / "logs" / "manifest.json"
    logs = parse_logs(logs_path)
    _write_snapshots(out, index, logs)
    _write_package(
        out,
        package_version=2,
        v1_path=out / "v1" / "views.json",
        v2_root=out / "v2",
        logs_path=logs_path,
        snapshots_root=out / "snapshots",
        index=index,
        logical_clock_ms=180000,
    )


def generate_rollback(out: Path) -> None:
    out = out.resolve()
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(out.parent / "sample", out)
    manifest = json.loads((out / "package.json").read_text(encoding="utf-8"))
    manifest["package_version"] = 0
    _write_json(out / "package.json", manifest)


def generate_suite(root: Path) -> dict[str, str]:
    root = root.resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    generate_sample(root / "sample")
    generate_append(root / "sample_append")
    generate_next(root / "sample_next")
    generate_rollback(root / "sample_rollback")
    return {
        "append": str(root / "sample_append" / "package.json"),
        "base": str(root / "sample" / "package.json"),
        "next": str(root / "sample_next" / "package.json"),
        "rollback": str(root / "sample_rollback" / "package.json"),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic MV engine samples")
    parser.add_argument("--out", required=True, help="output root directory")
    args = parser.parse_args(list(argv) if argv is not None else None)
    paths = generate_suite(Path(args.out))
    print(canonical_json(paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

