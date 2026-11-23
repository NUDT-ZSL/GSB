from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .errors import ErrorCode, GraphStoreError, fail
from .models import (
    Edge,
    NodeVersion,
    PackageManifest,
    SnapshotV1,
    SnapshotV2,
)
from .util import (
    canonical_json,
    canonical_node_id,
    digest_file,
    digest_json,
    ensure_within,
    finite_number,
    is_int,
    read_json,
    validate_label,
)


V1_SCHEMA = "graphstore.v1.snapshot/1"
V2_MANIFEST_SCHEMA = "graphstore.v2.manifest/1"
V2_SHARD_SCHEMA = "graphstore.v2.shard/1"
PACKAGE_SCHEMA = "graphstore.package/1"


def _snapshot_node_id(value: Any, code: str) -> str:
    try:
        return canonical_node_id(value)
    except GraphStoreError as exc:
        raise fail(code, exc.message, value=repr(value))


def _snapshot_label(value: Any, code: str) -> str:
    try:
        return validate_label(value, code=code)
    except GraphStoreError as exc:
        raise fail(code, exc.message, value=repr(value))


def v2_node_digest(record: dict[str, Any]) -> str:
    return digest_json(
        {
            "canonical_id": record["canonical_id"],
            "label": record["label"],
            "weight": record["weight"],
            "write_ms": record["write_ms"],
            "tombstone": record["tombstone"],
            "out_edges": record["out_edges"],
        }
    )


def _parse_v1_edges(
    node_id: str,
    node: dict[str, Any],
    *,
    logical_clock: int,
) -> tuple[Edge, ...]:
    adjacency = node.get("adjacency")
    declarations = node.get("out_edges")
    if not isinstance(adjacency, dict) or not isinstance(declarations, list):
        raise fail(
            ErrorCode.V1_INVALID_RECORD,
            "node requires adjacency object and out_edges declarations",
            node_id=node_id,
        )
    targets = adjacency.get("targets")
    labels = adjacency.get("labels")
    weights = adjacency.get("weights")
    if not all(isinstance(item, list) for item in (targets, labels, weights)):
        raise fail(
            ErrorCode.V1_ADJACENCY_MISMATCH,
            "adjacency targets/labels/weights must be arrays",
            node_id=node_id,
        )
    if not (len(targets) == len(labels) == len(weights)):
        raise fail(
            ErrorCode.V1_ADJACENCY_MISMATCH,
            "adjacency arrays have different lengths",
            node_id=node_id,
            lengths=[len(targets), len(labels), len(weights)],
        )

    adjacency_edges: list[Edge] = []
    for index, (target_raw, label_raw, weight_raw) in enumerate(
        zip(targets, labels, weights)
    ):
        target = _snapshot_node_id(target_raw, ErrorCode.V1_ADJACENCY_MISMATCH)
        label = _snapshot_label(label_raw, ErrorCode.V1_INVALID_RECORD)
        if not finite_number(weight_raw):
            raise fail(
                ErrorCode.V1_INVALID_WEIGHT,
                "edge weight must be finite",
                node_id=node_id,
                edge_index=index,
            )
        adjacency_edges.append(
            Edge(
                target=target,
                label=label,
                weight=float(weight_raw),
                source="v1",
                logical_clock=logical_clock,
                generation=0,
                source_priority=1,
            )
        )

    declared_edges: list[Edge] = []
    for index, declaration in enumerate(declarations):
        if not isinstance(declaration, dict):
            raise fail(
                ErrorCode.V1_INVALID_RECORD,
                "out_edges entries must be objects",
                node_id=node_id,
                edge_index=index,
            )
        target = _snapshot_node_id(
            declaration.get("target"), ErrorCode.V1_ADJACENCY_MISMATCH
        )
        label = _snapshot_label(
            declaration.get("label"), ErrorCode.V1_INVALID_RECORD
        )
        weight = declaration.get("weight", 1.0)
        if not finite_number(weight):
            raise fail(
                ErrorCode.V1_INVALID_WEIGHT,
                "edge weight must be finite",
                node_id=node_id,
                edge_index=index,
            )
        declared_edges.append(
            Edge(
                target=target,
                label=label,
                weight=float(weight),
                source="v1",
                logical_clock=logical_clock,
                generation=0,
                source_priority=1,
            )
        )

    if [edge.to_dict() for edge in adjacency_edges] != [
        edge.to_dict() for edge in declared_edges
    ]:
        raise fail(
            ErrorCode.V1_ADJACENCY_MISMATCH,
            "adjacency arrays do not match out_edges declarations",
            node_id=node_id,
        )
    return tuple(adjacency_edges)


def load_v1(
    path: str | Path,
    *,
    logical_clock_ms: int,
    clock_scale_ms: int = 1000,
    rounding: str = "half_up",
) -> SnapshotV1:
    if not is_int(logical_clock_ms) or logical_clock_ms < 0:
        raise fail(
            ErrorCode.INVALID_ARGUMENT,
            "logical_clock_ms must be a non-negative integer",
        )
    if not is_int(clock_scale_ms) or clock_scale_ms <= 0:
        raise fail(
            ErrorCode.INVALID_ARGUMENT,
            "v1 clock scale must be a positive integer",
        )
    if rounding not in {"half_up", "floor", "ceil"}:
        raise fail(
            ErrorCode.INVALID_ARGUMENT,
            "rounding must be half_up, floor or ceil",
        )

    source = Path(path)
    raw = read_json(source, code=ErrorCode.V1_INVALID_JSON)
    if not isinstance(raw, dict):
        raise fail(ErrorCode.V1_INVALID_JSON, "v1 snapshot root must be an object")
    if raw.get("schema") != V1_SCHEMA:
        raise fail(
            ErrorCode.V1_INVALID_JSON,
            f"v1 schema must be {V1_SCHEMA}",
            schema=raw.get("schema"),
        )
    snapshot_id = raw.get("snapshot_id")
    version = raw.get("version")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise fail(ErrorCode.V1_INVALID_JSON, "snapshot_id must be a non-empty string")
    if not is_int(version) or version <= 0:
        raise fail(ErrorCode.V1_INVALID_JSON, "version must be a positive integer")
    records = raw.get("nodes")
    linear_index = raw.get("linear_index")
    if not isinstance(records, list):
        raise fail(ErrorCode.V1_INVALID_JSON, "nodes must be an array")
    if not isinstance(linear_index, list):
        raise fail(ErrorCode.V1_INDEX_MISMATCH, "linear_index must be an array")

    nodes: dict[str, NodeVersion] = {}
    for position, record in enumerate(records):
        if not isinstance(record, dict):
            raise fail(
                ErrorCode.V1_INVALID_RECORD,
                "node record must be an object",
                position=position,
            )
        node_id = _snapshot_node_id(
            record.get("id"), ErrorCode.V1_INVALID_RECORD
        )
        if node_id in nodes:
            raise fail(
                ErrorCode.V1_DUPLICATE_NODE_ID,
                "duplicate node id",
                node_id=node_id,
            )
        label = _snapshot_label(
            record.get("label"), ErrorCode.V1_INVALID_RECORD
        )
        weight = record.get("weight")
        if not finite_number(weight):
            raise fail(
                ErrorCode.V1_INVALID_WEIGHT,
                "node weight must be finite",
                node_id=node_id,
            )
        written_seconds = record.get("written_seconds")
        if not is_int(written_seconds) or written_seconds < 0:
            raise fail(
                ErrorCode.V1_INVALID_WRITTEN_SECONDS,
                "written_seconds must be a non-negative integer",
                node_id=node_id,
            )
        write_ms = logical_clock_ms + written_seconds * clock_scale_ms
        if rounding == "half_up":
            rounded_ms = write_ms
        elif rounding == "floor":
            rounded_ms = write_ms
        else:
            rounded_ms = write_ms
        conversion = {
            "source": "v1",
            "raw_written_seconds": written_seconds,
            "logical_clock_epoch_ms": logical_clock_ms,
            "scale_ms_per_second": clock_scale_ms,
            "rounding": rounding,
            "pre_round_ms": write_ms,
            "write_ms": rounded_ms,
        }
        edges = _parse_v1_edges(node_id, record, logical_clock=rounded_ms)
        nodes[node_id] = NodeVersion(
            node_id=node_id,
            label=label,
            weight=float(weight),
            logical_clock=rounded_ms,
            generation=0,
            source="v1",
            source_priority=1,
            tombstone=False,
            edges=edges,
            conversion=conversion,
        )

    indexed_ids: list[str] = []
    for position, item in enumerate(linear_index):
        indexed_ids.append(
            _snapshot_node_id(item, ErrorCode.V1_INDEX_MISMATCH)
        )
    if indexed_ids != sorted(indexed_ids):
        raise fail(
            ErrorCode.V1_INDEX_MISMATCH,
            "linear_index is not sorted",
        )
    if len(set(indexed_ids)) != len(indexed_ids):
        raise fail(
            ErrorCode.V1_INDEX_MISMATCH,
            "linear_index contains duplicates",
        )
    if set(indexed_ids) != set(nodes):
        raise fail(
            ErrorCode.V1_INDEX_MISMATCH,
            "linear_index does not cover exactly all node IDs",
            missing=sorted(set(nodes) - set(indexed_ids)),
            extra=sorted(set(indexed_ids) - set(nodes)),
        )

    return SnapshotV1(
        path=str(source),
        schema=V1_SCHEMA,
        snapshot_id=snapshot_id,
        version=version,
        nodes=nodes,
        linear_index=tuple(indexed_ids),
        raw_digest=digest_file(source),
        injected_clock_ms=logical_clock_ms,
        clock_scale_ms=clock_scale_ms,
        rounding=rounding,
    )


def _required_int(
    value: Any,
    *,
    name: str,
    code: str,
    minimum: int | None = 0,
) -> int:
    if not is_int(value) or (minimum is not None and value < minimum):
        comparator = f" >= {minimum}" if minimum is not None else ""
        raise fail(code, f"{name} must be an integer{comparator}", value=repr(value))
    return value


def _read_metadata(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    uri = f"file:{path.resolve()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.Error as exc:
        raise fail(
            ErrorCode.V2_METADATA_INVALID,
            "unable to open v2 metadata database",
            path=str(path),
            error=str(exc),
        )
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {"snapshot_meta", "shards", "nodes"}
        if not required.issubset(tables):
            raise fail(
                ErrorCode.V2_METADATA_INVALID,
                "metadata database is missing required tables",
                missing=sorted(required - tables),
            )
        meta_row = connection.execute(
            "SELECT * FROM snapshot_meta WHERE id = 1"
        ).fetchone()
        if meta_row is None:
            raise fail(
                ErrorCode.V2_METADATA_INVALID,
                "snapshot_meta row id=1 is missing",
            )
        snapshot_meta = dict(meta_row)
        shards = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM shards ORDER BY ordinal, shard_id"
            )
        ]
        nodes: dict[str, dict[str, Any]] = {}
        for row in connection.execute("SELECT * FROM nodes ORDER BY canonical_id"):
            value = dict(row)
            node_id = _snapshot_node_id(
                value.get("canonical_id"), ErrorCode.V2_DUPLICATE_NODE_ID
            )
            if node_id in nodes:
                raise fail(
                    ErrorCode.V2_DUPLICATE_NODE_ID,
                    "duplicate canonical id in metadata",
                    node_id=node_id,
                )
            nodes[node_id] = value
        return snapshot_meta, shards, nodes
    except GraphStoreError:
        raise
    except sqlite3.Error as exc:
        raise fail(
            ErrorCode.V2_METADATA_INVALID,
            "unable to read metadata",
            error=str(exc),
        )
    finally:
        connection.close()


def _parse_v2_edges(
    node_id: str,
    raw_edges: Any,
    *,
    source: str,
    generation: int,
    logical_clock: int,
) -> tuple[Edge, ...]:
    if not isinstance(raw_edges, list):
        raise fail(
            ErrorCode.V2_METADATA_INVALID,
            "out_edges must be an array",
            node_id=node_id,
        )
    edges: list[Edge] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(raw_edges):
        if not isinstance(item, dict):
            raise fail(
                ErrorCode.V2_METADATA_INVALID,
                "out_edges entries must be objects",
                node_id=node_id,
                edge_index=index,
            )
        target = _snapshot_node_id(
            item.get("target"), ErrorCode.V2_METADATA_INVALID
        )
        label = _snapshot_label(
            item.get("label"), ErrorCode.INVALID_EDGE_LABEL
        )
        weight = item.get("weight", 1.0)
        if not finite_number(weight):
            raise fail(
                ErrorCode.V2_METADATA_INVALID,
                "edge weight must be finite",
                node_id=node_id,
                edge_index=index,
            )
        key = (target, label)
        if key in seen:
            raise fail(
                ErrorCode.V2_METADATA_INVALID,
                "out_edges must be a set (duplicate target/label)",
                node_id=node_id,
                edge=key,
            )
        seen.add(key)
        edges.append(
            Edge(
                target=target,
                label=label,
                weight=float(weight),
                source=source,
                logical_clock=logical_clock,
                generation=generation,
                source_priority=2,
            )
        )
    if edges != sorted(edges, key=lambda edge: (edge.target, edge.label)):
        raise fail(
            ErrorCode.V2_METADATA_INVALID,
            "out_edges must be sorted by target then label",
            node_id=node_id,
        )
    return tuple(edges)


def _node_record_from_parts(
    *,
    node_id: str,
    label: Any,
    weight: Any,
    write_ms: Any,
    tombstone: Any,
    out_edges: Any,
    source: str,
    generation: int,
    shard_id: str,
) -> tuple[dict[str, Any], NodeVersion]:
    timestamp = _required_int(
        write_ms,
        name="write_ms",
        code=ErrorCode.V2_INVALID_TIMESTAMP,
    )
    if not isinstance(tombstone, bool):
        raise fail(
            ErrorCode.V2_METADATA_INVALID,
            "tombstone must be boolean",
            node_id=node_id,
        )
    if tombstone:
        if label is not None:
            raise fail(
                ErrorCode.V2_METADATA_INVALID,
                "tombstone label must be null",
                node_id=node_id,
            )
        if weight is not None:
            raise fail(
                ErrorCode.V2_METADATA_INVALID,
                "tombstone weight must be null",
                node_id=node_id,
            )
        if out_edges != []:
            raise fail(
                ErrorCode.V2_METADATA_INVALID,
                "tombstone out_edges must be empty",
                node_id=node_id,
            )
        parsed_label = ""
        parsed_weight = None
        edges: tuple[Edge, ...] = ()
    else:
        parsed_label = _snapshot_label(label, ErrorCode.V2_METADATA_INVALID)
        if not finite_number(weight):
            raise fail(
                ErrorCode.V2_METADATA_INVALID,
                "node weight must be finite",
                node_id=node_id,
            )
        parsed_weight = float(weight)
        edges = _parse_v2_edges(
            node_id,
            out_edges,
            source=source,
            generation=generation,
            logical_clock=timestamp,
        )
    record = {
        "canonical_id": node_id,
        "label": label,
        "weight": weight,
        "write_ms": timestamp,
        "tombstone": tombstone,
        "out_edges": out_edges,
    }
    node = NodeVersion(
        node_id=node_id,
        label=parsed_label,
        weight=parsed_weight,
        logical_clock=timestamp,
        generation=generation,
        source=source,
        source_priority=2,
        tombstone=tombstone,
        edges=edges,
        shard_id=shard_id,
    )
    return record, node


def _parse_manifest(path: Path) -> dict[str, Any]:
    manifest = read_json(path, code=ErrorCode.V2_MANIFEST_INVALID)
    if not isinstance(manifest, dict):
        raise fail(ErrorCode.V2_MANIFEST_INVALID, "manifest root must be an object")
    if manifest.get("schema") != V2_MANIFEST_SCHEMA:
        raise fail(
            ErrorCode.V2_MANIFEST_INVALID,
            f"manifest schema must be {V2_MANIFEST_SCHEMA}",
        )
    required = {
        "snapshot_id",
        "version",
        "logical_clock_floor",
        "node_count_min",
        "node_count_max",
        "shards",
    }
    missing = sorted(required - set(manifest))
    if missing:
        raise fail(
            ErrorCode.V2_MANIFEST_INVALID,
            "manifest is missing fields",
            missing=missing,
        )
    if not isinstance(manifest["snapshot_id"], str) or not manifest["snapshot_id"]:
        raise fail(
            ErrorCode.V2_MANIFEST_INVALID,
            "snapshot_id must be a non-empty string",
        )
    _required_int(
        manifest["version"],
        name="version",
        code=ErrorCode.V2_MANIFEST_INVALID,
        minimum=1,
    )
    _required_int(
        manifest["logical_clock_floor"],
        name="logical_clock_floor",
        code=ErrorCode.V2_MANIFEST_INVALID,
    )
    low = _required_int(
        manifest["node_count_min"],
        name="node_count_min",
        code=ErrorCode.V2_MANIFEST_INVALID,
    )
    high = _required_int(
        manifest["node_count_max"],
        name="node_count_max",
        code=ErrorCode.V2_MANIFEST_INVALID,
    )
    if low > high:
        raise fail(
            ErrorCode.V2_MANIFEST_INVALID,
            "node_count_min exceeds node_count_max",
        )
    shards = manifest["shards"]
    if not isinstance(shards, list) or not shards:
        raise fail(
            ErrorCode.V2_MANIFEST_INVALID,
            "shards must be a non-empty array",
        )
    seen_shards: set[str] = set()
    for item in shards:
        if not isinstance(item, dict):
            raise fail(
                ErrorCode.V2_MANIFEST_INVALID,
                "shard entries must be objects",
            )
        shard_id = item.get("shard_id")
        file_name = item.get("file")
        if not isinstance(shard_id, str) or not shard_id:
            raise fail(
                ErrorCode.V2_MANIFEST_INVALID,
                "shard_id must be a non-empty string",
            )
        if shard_id in seen_shards:
            raise fail(
                ErrorCode.V2_MANIFEST_INVALID,
                "duplicate shard_id",
                shard_id=shard_id,
            )
        seen_shards.add(shard_id)
        if not isinstance(file_name, str) or not file_name:
            raise fail(
                ErrorCode.V2_MANIFEST_INVALID,
                "shard file must be a non-empty string",
                shard_id=shard_id,
            )
        _required_int(
            item.get("generation"),
            name="generation",
            code=ErrorCode.V2_MANIFEST_INVALID,
        )
        _required_int(
            item.get("node_count"),
            name="node_count",
            code=ErrorCode.V2_MANIFEST_INVALID,
        )
        digest = item.get("sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise fail(
                ErrorCode.V2_MANIFEST_INVALID,
                "sha256 must be 64 lowercase hex characters",
                shard_id=shard_id,
            )
    return manifest


def load_v2(
    directory: str | Path,
    *,
    known_existing_ids: Iterable[str] = (),
) -> SnapshotV2:
    root = Path(directory)
    manifest_path = root / "manifest.json"
    metadata_path = root / "metadata.sqlite"
    manifest = _parse_manifest(manifest_path)
    metadata, db_shards, db_nodes = _read_metadata(metadata_path)

    for field in (
        "snapshot_id",
        "version",
        "logical_clock_floor",
        "node_count_min",
        "node_count_max",
    ):
        if metadata.get(field) != manifest[field]:
            raise fail(
                ErrorCode.V2_METADATA_INVALID,
                "metadata does not match manifest",
                field=field,
                manifest=manifest[field],
                metadata=metadata.get(field),
            )

    manifest_shards = {
        item["shard_id"]: item for item in manifest["shards"]
    }
    metadata_shards = {item["shard_id"]: item for item in db_shards}
    if set(manifest_shards) != set(metadata_shards):
        raise fail(
            ErrorCode.V2_SHARD_MISMATCH,
            "manifest and metadata shard sets differ",
            missing=sorted(set(manifest_shards) - set(metadata_shards)),
            extra=sorted(set(metadata_shards) - set(manifest_shards)),
        )

    last_generation = -1
    parsed_nodes: dict[str, NodeVersion] = {}
    shard_summaries: list[dict[str, Any]] = []
    shard_node_ids: set[str] = set()
    for ordinal, manifest_shard in enumerate(manifest["shards"]):
        shard_id = manifest_shard["shard_id"]
        meta_shard = metadata_shards[shard_id]
        for field in ("file", "sha256", "generation", "node_count"):
            if meta_shard.get(field) != manifest_shard.get(field):
                raise fail(
                    ErrorCode.V2_SHARD_MISMATCH,
                    "shard metadata does not match manifest",
                    shard_id=shard_id,
                    field=field,
                )
        if meta_shard.get("ordinal") != ordinal:
            raise fail(
                ErrorCode.V2_SHARD_MISMATCH,
                "shard ordinal does not match manifest order",
                shard_id=shard_id,
                expected=ordinal,
                actual=meta_shard.get("ordinal"),
            )
        generation = manifest_shard["generation"]
        if generation < last_generation:
            raise fail(
                ErrorCode.V2_GENERATION_REGRESSION,
                "shard generations must be monotonic",
                shard_id=shard_id,
                previous=last_generation,
                generation=generation,
            )
        last_generation = generation

        shard_path = ensure_within(root, root / manifest_shard["file"])
        try:
            actual_hash = digest_file(shard_path)
        except OSError as exc:
            raise fail(
                ErrorCode.V2_SHARD_MISMATCH,
                "unable to read shard",
                shard_id=shard_id,
                path=str(shard_path),
                error=str(exc),
            )
        if actual_hash != manifest_shard["sha256"]:
            raise fail(
                ErrorCode.SHARD_HASH_MISMATCH,
                "shard hash does not match manifest",
                shard_id=shard_id,
                expected=manifest_shard["sha256"],
                actual=actual_hash,
            )
        if meta_shard["sha256"] != actual_hash:
            raise fail(
                ErrorCode.SHARD_HASH_MISMATCH,
                "shard hash does not match metadata",
                shard_id=shard_id,
                expected=meta_shard["sha256"],
                actual=actual_hash,
            )

        raw_shard = read_json(shard_path, code=ErrorCode.V2_INVALID_JSON)
        if not isinstance(raw_shard, dict):
            raise fail(
                ErrorCode.V2_INVALID_JSON,
                "shard root must be an object",
                shard_id=shard_id,
            )
        if raw_shard.get("schema") != V2_SHARD_SCHEMA:
            raise fail(
                ErrorCode.V2_INVALID_JSON,
                f"shard schema must be {V2_SHARD_SCHEMA}",
                shard_id=shard_id,
            )
        if raw_shard.get("shard_id") != shard_id:
            raise fail(
                ErrorCode.V2_SHARD_MISMATCH,
                "shard id mismatch",
                shard_id=shard_id,
                actual=raw_shard.get("shard_id"),
            )
        if raw_shard.get("generation") != generation:
            raise fail(
                ErrorCode.V2_GENERATION_REGRESSION,
                "shard generation mismatch",
                shard_id=shard_id,
                expected=generation,
                actual=raw_shard.get("generation"),
            )
        raw_records = raw_shard.get("nodes")
        if not isinstance(raw_records, list):
            raise fail(
                ErrorCode.V2_INVALID_JSON,
                "shard nodes must be an array",
                shard_id=shard_id,
            )
        if len(raw_records) != manifest_shard["node_count"]:
            raise fail(
                ErrorCode.V2_SHARD_MISMATCH,
                "shard node count mismatch",
                shard_id=shard_id,
                expected=manifest_shard["node_count"],
                actual=len(raw_records),
            )

        for record in raw_records:
            if not isinstance(record, dict):
                raise fail(
                    ErrorCode.V2_INVALID_JSON,
                    "node record must be an object",
                    shard_id=shard_id,
                )
            required = {
                "canonical_id",
                "label",
                "weight",
                "write_ms",
                "tombstone",
                "out_edges",
                "node_digest",
            }
            missing = sorted(required - set(record))
            if missing:
                raise fail(
                    ErrorCode.V2_INVALID_JSON,
                    "node record is missing fields",
                    shard_id=shard_id,
                    missing=missing,
                )
            node_id = _snapshot_node_id(
                record["canonical_id"], ErrorCode.V2_DUPLICATE_NODE_ID
            )
            if node_id in parsed_nodes or node_id in shard_node_ids:
                raise fail(
                    ErrorCode.V2_DUPLICATE_NODE_ID,
                    "duplicate canonical id across shards",
                    node_id=node_id,
                )
            source = f"v2:{shard_id}"
            normalized, node = _node_record_from_parts(
                node_id=node_id,
                label=record["label"],
                weight=record["weight"],
                write_ms=record["write_ms"],
                tombstone=record["tombstone"],
                out_edges=record["out_edges"],
                source=source,
                generation=generation,
                shard_id=shard_id,
            )
            expected_digest = v2_node_digest(normalized)
            if record["node_digest"] != expected_digest:
                raise fail(
                    ErrorCode.V2_NODE_DIGEST_MISMATCH,
                    "node digest does not match shard record",
                    node_id=node_id,
                    expected=expected_digest,
                    actual=record["node_digest"],
                )
            db_record = db_nodes.get(node_id)
            if db_record is None:
                raise fail(
                    ErrorCode.V2_METADATA_INVALID,
                    "shard node is absent from metadata",
                    node_id=node_id,
                )
            if (
                db_record.get("shard_id") != shard_id
                or db_record.get("shard_generation") != generation
            ):
                raise fail(
                    ErrorCode.V2_METADATA_INVALID,
                    "node shard metadata mismatch",
                    node_id=node_id,
                )
            try:
                db_edges = json.loads(db_record["out_edges_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise fail(
                    ErrorCode.V2_METADATA_INVALID,
                    "metadata out_edges_json is invalid",
                    node_id=node_id,
                    error=str(exc),
                )
            db_normalized = {
                "canonical_id": db_record.get("canonical_id"),
                "label": db_record.get("label"),
                "weight": db_record.get("weight"),
                "write_ms": db_record.get("write_ms"),
                "tombstone": bool(db_record.get("tombstone")),
                "out_edges": db_edges,
            }
            if canonical_json(db_normalized) != canonical_json(normalized):
                raise fail(
                    ErrorCode.V2_METADATA_INVALID,
                    "shard node does not match metadata row",
                    node_id=node_id,
                )
            if db_record.get("node_digest") != expected_digest:
                raise fail(
                    ErrorCode.V2_NODE_DIGEST_MISMATCH,
                    "metadata node digest mismatch",
                    node_id=node_id,
                    expected=expected_digest,
                    actual=db_record.get("node_digest"),
                )
            parsed_nodes[node_id] = node
            shard_node_ids.add(node_id)

        shard_summaries.append(
            {
                "shard_id": shard_id,
                "file": manifest_shard["file"],
                "sha256": actual_hash,
                "generation": generation,
                "node_count": len(raw_records),
                "ordinal": ordinal,
            }
        )

    if set(db_nodes) != shard_node_ids:
        raise fail(
            ErrorCode.V2_METADATA_INVALID,
            "metadata node set does not match shard node set",
            missing=sorted(set(db_nodes) - shard_node_ids),
            extra=sorted(shard_node_ids - set(db_nodes)),
        )

    node_count = len(parsed_nodes)
    if not manifest["node_count_min"] <= node_count <= manifest["node_count_max"]:
        raise fail(
            ErrorCode.V2_MANIFEST_INVALID,
            "actual node count is outside declared bounds",
            actual=node_count,
            minimum=manifest["node_count_min"],
            maximum=manifest["node_count_max"],
        )
    logical_clock_floor = manifest["logical_clock_floor"]
    for node in parsed_nodes.values():
        if node.logical_clock < logical_clock_floor:
            raise fail(
                ErrorCode.V2_INVALID_TIMESTAMP,
                "node timestamp is below logical clock floor",
                node_id=node.node_id,
                timestamp=node.logical_clock,
                floor=logical_clock_floor,
            )
        if node.edges and node.logical_clock != node.edges[0].logical_clock:
            raise fail(
                ErrorCode.V2_INVALID_TIMESTAMP,
                "edge timestamp mismatch",
                node_id=node.node_id,
            )

    existing_ids = set(known_existing_ids)
    existing_ids.update(
        node_id
        for node_id, node in parsed_nodes.items()
        if not node.tombstone
    )
    tombstones = tuple(
        sorted(
            node_id
            for node_id, node in parsed_nodes.items()
            if node.tombstone
        )
    )
    for node_id in tombstones:
        if node_id not in existing_ids:
            raise fail(
                ErrorCode.V2_TOMBSTONE_UNKNOWN_NODE,
                "tombstone refers to a node that does not exist",
                node_id=node_id,
            )

    metadata_payload = {
        "snapshot_meta": metadata,
        "shards": db_shards,
        "nodes": [db_nodes[node_id] for node_id in sorted(db_nodes)],
    }
    return SnapshotV2(
        path=str(root),
        schema=V2_MANIFEST_SCHEMA,
        snapshot_id=manifest["snapshot_id"],
        version=manifest["version"],
        logical_clock_floor=logical_clock_floor,
        node_count_min=manifest["node_count_min"],
        node_count_max=manifest["node_count_max"],
        nodes=parsed_nodes,
        tombstones=tombstones,
        shards=tuple(shard_summaries),
        manifest_digest=digest_file(manifest_path),
        metadata_digest=digest_json(metadata_payload),
    )


def load_snapshots(
    v1_path: str | Path,
    v2_path: str | Path,
    *,
    logical_clock_ms: int,
    v1_clock_scale_ms: int = 1000,
    v1_rounding: str = "half_up",
) -> tuple[SnapshotV1, SnapshotV2, PackageManifest]:
    v1 = load_v1(
        v1_path,
        logical_clock_ms=logical_clock_ms,
        clock_scale_ms=v1_clock_scale_ms,
        rounding=v1_rounding,
    )
    v2 = load_v2(v2_path, known_existing_ids=set(v1.nodes))
    package = PackageManifest(
        schema=PACKAGE_SCHEMA,
        snapshot_id=v2.snapshot_id,
        package_version=v2.version,
        logical_clock_floor=v2.logical_clock_floor,
        node_count_min=v2.node_count_min,
        node_count_max=v2.node_count_max,
        shards=v2.shards,
        v1_snapshot_id=v1.snapshot_id,
        v2_snapshot_id=v2.snapshot_id,
        v1_digest=v1.raw_digest,
        v2_manifest_digest=v2.manifest_digest,
        v2_metadata_digest=v2.metadata_digest,
        injected_clock_ms=logical_clock_ms,
        v1_clock_scale_ms=v1_clock_scale_ms,
        v1_rounding=v1_rounding,
    )
    return v1, v2, package

