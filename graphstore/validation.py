from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .checksums import (
    digest_edges,
    is_finite_number,
    is_int,
    load_json,
    sha256_file,
    sha256_object,
)
from .errors import (
    E_MISSING_FILE,
    E_SHARD_HASH_MISMATCH,
    E_V1_ADJACENCY_MISMATCH,
    E_V1_DUPLICATE_NODE_ID,
    E_V1_INVALID_RELATIVE_TIME,
    E_V1_INVALID_WEIGHT,
    E_V1_LINEAR_INDEX,
    E_V1_SCHEMA,
    E_V2_DUPLICATE_CANONICAL_ID,
    E_V2_EDGE_INVALID,
    E_V2_GENERATION,
    E_V2_METADATA,
    E_V2_SCHEMA,
    E_V2_TOMBSTONE,
    GraphStoreError,
    require,
)
from .model import EdgeInput, V1Node, V1Snapshot, V2Node, V2Shard, V2Snapshot


def _string(value: Any, code: str, field: str, **context: Any) -> str:
    require(
        isinstance(value, str) and bool(value.strip()),
        code,
        f"{field} must be a non-empty string",
        field=field,
        **context,
    )
    return value


def _nonnegative_int(value: Any, code: str, field: str, **context: Any) -> int:
    require(
        is_int(value) and value >= 0,
        code,
        f"{field} must be a non-negative integer",
        field=field,
        value=value,
        **context,
    )
    return value


def _validate_v1_node(record: Any, index: int) -> V1Node:
    require(
        isinstance(record, dict),
        E_V1_SCHEMA,
        "v1 node must be an object",
        node_index=index,
    )
    node_id = _string(record.get("id"), E_V1_SCHEMA, "node.id", node_index=index)
    label = _string(record.get("label"), E_V1_SCHEMA, "node.label", node_id=node_id)
    weight = record.get("weight")
    require(
        is_finite_number(weight),
        E_V1_INVALID_WEIGHT,
        "v1 node weight must be a finite number",
        node_id=node_id,
        value=weight,
    )
    relative_seconds = record.get("relative_write_seconds")
    require(
        is_int(relative_seconds) and relative_seconds >= 0,
        E_V1_INVALID_RELATIVE_TIME,
        "v1 relative_write_seconds must be a non-negative integer",
        node_id=node_id,
        value=relative_seconds,
    )

    adjacency = record.get("adjacency")
    require(
        isinstance(adjacency, list),
        E_V1_SCHEMA,
        "v1 adjacency must be a flat array",
        node_id=node_id,
    )
    require(
        len(adjacency) % 2 == 0,
        E_V1_SCHEMA,
        "v1 adjacency must contain target/label pairs",
        node_id=node_id,
        length=len(adjacency),
    )
    adjacency_pairs: list[tuple[str, str]] = []
    for offset in range(0, len(adjacency), 2):
        target = _string(
            adjacency[offset],
            E_V1_SCHEMA,
            "node.adjacency target",
            node_id=node_id,
            offset=offset,
        )
        edge_label = _string(
            adjacency[offset + 1],
            E_V1_SCHEMA,
            "node.adjacency label",
            node_id=node_id,
            offset=offset,
        )
        adjacency_pairs.append((target, edge_label))

    declared = record.get("out_edges")
    require(
        isinstance(declared, list),
        E_V1_SCHEMA,
        "v1 out_edges must be an array",
        node_id=node_id,
    )
    declared_pairs: list[tuple[str, str]] = []
    for edge_index, edge in enumerate(declared):
        require(
            isinstance(edge, dict),
            E_V1_SCHEMA,
            "v1 out_edges entries must be objects",
            node_id=node_id,
            edge_index=edge_index,
        )
        target = _string(
            edge.get("target"),
            E_V1_SCHEMA,
            "node.out_edges.target",
            node_id=node_id,
            edge_index=edge_index,
        )
        edge_label = _string(
            edge.get("label"),
            E_V1_SCHEMA,
            "node.out_edges.label",
            node_id=node_id,
            edge_index=edge_index,
        )
        declared_pairs.append((target, edge_label))
    require(
        adjacency_pairs == declared_pairs,
        E_V1_ADJACENCY_MISMATCH,
        "v1 adjacency does not match declared out_edges in order and multiplicity",
        node_id=node_id,
        adjacency=adjacency_pairs,
        out_edges=declared_pairs,
    )
    return V1Node(
        node_id=node_id,
        label=label,
        weight=float(weight),
        relative_write_seconds=relative_seconds,
        edges=tuple(
            EdgeInput(to_id=target, label=edge_label, weight=1.0)
            for target, edge_label in declared_pairs
        ),
    )


def validate_v1_snapshot(path: Path) -> V1Snapshot:
    document = load_json(path, label="v1 snapshot")
    require(
        isinstance(document, dict),
        E_V1_SCHEMA,
        "v1 snapshot root must be an object",
        path=str(path),
    )
    require(
        document.get("format") == "v1",
        E_V1_SCHEMA,
        "v1 snapshot format must be 'v1'",
        path=str(path),
    )
    snapshot_version = _nonnegative_int(
        document.get("snapshot_version"), E_V1_SCHEMA, "snapshot_version"
    )
    logical_clock_ms = _nonnegative_int(
        document.get("logical_clock_ms"), E_V1_SCHEMA, "logical_clock_ms"
    )
    records = document.get("nodes")
    require(
        isinstance(records, list),
        E_V1_SCHEMA,
        "v1 nodes must be an array",
        path=str(path),
    )
    nodes = tuple(_validate_v1_node(record, index) for index, record in enumerate(records))

    node_ids = [node.node_id for node in nodes]
    require(
        len(node_ids) == len(set(node_ids)),
        E_V1_DUPLICATE_NODE_ID,
        "v1 contains duplicate node IDs",
        duplicates=sorted(
            {node_id for node_id in node_ids if node_ids.count(node_id) > 1}
        ),
    )
    linear_index = document.get("linear_index")
    require(
        isinstance(linear_index, list)
        and all(isinstance(item, str) for item in linear_index),
        E_V1_LINEAR_INDEX,
        "v1 linear_index must be an array of node IDs",
    )
    require(
        linear_index == sorted(linear_index) and len(linear_index) == len(set(linear_index)),
        E_V1_LINEAR_INDEX,
        "v1 linear_index must be strictly ordered",
        linear_index=linear_index,
    )
    require(
        set(linear_index) == set(node_ids),
        E_V1_LINEAR_INDEX,
        "v1 linear_index must cover every node exactly once",
        missing=sorted(set(node_ids) - set(linear_index)),
        extra=sorted(set(linear_index) - set(node_ids)),
    )
    return V1Snapshot(
        snapshot_version=snapshot_version,
        logical_clock_ms=logical_clock_ms,
        path=str(path),
        sha256=sha256_file(path),
        nodes=nodes,
    )


def _read_v2_metadata(path: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
    require(path.is_file(), E_MISSING_FILE, "v2 metadata database is missing", path=str(path))
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        meta = {
            row["key"]: row["value"]
            for row in connection.execute("SELECT key, value FROM meta ORDER BY key")
        }
        shards = [
            dict(row)
            for row in connection.execute(
                """
                SELECT shard_id, generation, content_hash, node_count, edge_count,
                       adjacency_digest
                FROM shards
                ORDER BY ordinal
                """
            )
        ]
        connection.close()
    except sqlite3.Error as exc:
        raise GraphStoreError(
            E_V2_METADATA,
            "cannot read v2 metadata database",
            path=str(path),
            reason=str(exc),
        ) from exc
    return meta, shards


def _validate_v2_edge(edge: Any, node_id: str, edge_index: int) -> EdgeInput:
    require(
        isinstance(edge, dict),
        E_V2_EDGE_INVALID,
        "v2 edge must be an object",
        node_id=node_id,
        edge_index=edge_index,
    )
    target = _string(
        edge.get("target"),
        E_V2_EDGE_INVALID,
        "edge.target",
        node_id=node_id,
        edge_index=edge_index,
    )
    label = _string(
        edge.get("label"),
        E_V2_EDGE_INVALID,
        "edge.label",
        node_id=node_id,
        edge_index=edge_index,
    )
    weight = edge.get("weight")
    require(
        is_finite_number(weight) and float(weight) >= 0.0,
        E_V2_EDGE_INVALID,
        "v2 edge weight must be a finite non-negative number",
        node_id=node_id,
        edge_index=edge_index,
        value=weight,
    )
    return EdgeInput(to_id=target, label=label, weight=float(weight))


def _validate_v2_node(
    record: Any, shard_id: str, shard_generation: int, index: int
) -> V2Node:
    require(
        isinstance(record, dict),
        E_V2_SCHEMA,
        "v2 node must be an object",
        shard_id=shard_id,
        node_index=index,
    )
    node_id = _string(
        record.get("canonical_id"),
        E_V2_SCHEMA,
        "node.canonical_id",
        shard_id=shard_id,
    )
    label = _string(
        record.get("label"), E_V2_SCHEMA, "node.label", node_id=node_id
    )
    written_at_ms = _nonnegative_int(
        record.get("written_at_ms"),
        E_V2_SCHEMA,
        "node.written_at_ms",
        node_id=node_id,
    )
    generation = _nonnegative_int(
        record.get("generation"), E_V2_SCHEMA, "node.generation", node_id=node_id
    )
    require(
        generation == shard_generation,
        E_V2_GENERATION,
        "v2 node generation must match its shard generation",
        node_id=node_id,
        node_generation=generation,
        shard_generation=shard_generation,
    )
    tombstone = record.get("tombstone")
    require(
        isinstance(tombstone, bool),
        E_V2_TOMBSTONE,
        "v2 tombstone must be boolean",
        node_id=node_id,
        value=tombstone,
    )
    state = record.get("state")
    require(
        state in {"active", "tombstone"},
        E_V2_TOMBSTONE,
        "v2 node state must be active or tombstone",
        node_id=node_id,
        value=state,
    )
    require(
        (state == "tombstone") == tombstone,
        E_V2_TOMBSTONE,
        "v2 tombstone flag and node state disagree",
        node_id=node_id,
        state=state,
        tombstone=tombstone,
    )
    raw_edges = record.get("out_edges")
    require(
        isinstance(raw_edges, list),
        E_V2_EDGE_INVALID,
        "v2 out_edges must be an array",
        node_id=node_id,
    )
    edges = tuple(
        _validate_v2_edge(edge, node_id, edge_index)
        for edge_index, edge in enumerate(raw_edges)
    )
    return V2Node(
        canonical_id=node_id,
        label=label,
        out_edges=edges,
        written_at_ms=written_at_ms,
        generation=generation,
        tombstone=tombstone,
        state=state,
        shard_id=shard_id,
    )


def validate_v2_snapshot(manifest_path: Path) -> V2Snapshot:
    manifest = load_json(manifest_path, label="v2 manifest")
    require(
        isinstance(manifest, dict),
        E_V2_SCHEMA,
        "v2 manifest root must be an object",
        path=str(manifest_path),
    )
    require(
        manifest.get("format") == "v2",
        E_V2_SCHEMA,
        "v2 manifest format must be 'v2'",
        path=str(manifest_path),
    )
    snapshot_version = _nonnegative_int(
        manifest.get("snapshot_version"), E_V2_SCHEMA, "snapshot_version"
    )
    logical_clock_ms = _nonnegative_int(
        manifest.get("logical_clock_ms"), E_V2_SCHEMA, "logical_clock_ms"
    )
    adjacency_digest = _string(
        manifest.get("adjacency_digest"), E_V2_SCHEMA, "adjacency_digest"
    )
    metadata_path = (manifest_path.parent / _string(
        manifest.get("metadata_db"), E_V2_SCHEMA, "metadata_db"
    )).resolve()
    meta, metadata_shards = _read_v2_metadata(metadata_path)
    require(
        meta.get("format") == "v2",
        E_V2_METADATA,
        "v2 metadata format does not match",
        path=str(metadata_path),
    )
    require(
        meta.get("snapshot_version") == str(snapshot_version),
        E_V2_METADATA,
        "v2 metadata snapshot version does not match manifest",
    )
    require(
        meta.get("logical_clock_ms") == str(logical_clock_ms),
        E_V2_METADATA,
        "v2 metadata logical clock does not match manifest",
    )
    try:
        raw_global_generation = int(meta.get("global_generation", "-1"))
    except (TypeError, ValueError) as exc:
        raise GraphStoreError(
            E_V2_METADATA,
            "v2 metadata global_generation is not an integer",
            value=meta.get("global_generation"),
        ) from exc
    global_generation = _nonnegative_int(
        raw_global_generation,
        E_V2_METADATA,
        "metadata.global_generation",
    )
    require(
        meta.get("adjacency_digest") == adjacency_digest,
        E_V2_METADATA,
        "v2 metadata adjacency digest does not match manifest",
        manifest=adjacency_digest,
        metadata=meta.get("adjacency_digest"),
    )

    manifest_shards = manifest.get("shards")
    require(
        isinstance(manifest_shards, list) and bool(manifest_shards),
        E_V2_SCHEMA,
        "v2 shards must be a non-empty array",
    )
    require(
        len(manifest_shards) == len(metadata_shards),
        E_V2_METADATA,
        "v2 metadata shard list has a different length",
        manifest=len(manifest_shards),
        metadata=len(metadata_shards),
    )

    shards: list[V2Shard] = []
    seen_ids: set[str] = set()
    seen_node_ids: set[str] = set()
    previous_generation = -1
    all_edges: list[tuple[str, str, str, float]] = []
    for ordinal, shard_entry in enumerate(manifest_shards):
        require(
            isinstance(shard_entry, dict),
            E_V2_SCHEMA,
            "v2 shard entry must be an object",
            ordinal=ordinal,
        )
        shard_id = _string(
            shard_entry.get("shard_id"),
            E_V2_SCHEMA,
            "shard.shard_id",
            ordinal=ordinal,
        )
        require(
            shard_id not in seen_ids,
            E_V2_SCHEMA,
            "v2 shard IDs must be unique",
            shard_id=shard_id,
        )
        seen_ids.add(shard_id)
        generation = _nonnegative_int(
            shard_entry.get("generation"),
            E_V2_GENERATION,
            "shard.generation",
            shard_id=shard_id,
        )
        require(
            generation >= previous_generation,
            E_V2_GENERATION,
            "v2 shard generations must be monotonic",
            shard_id=shard_id,
            previous_generation=previous_generation,
            generation=generation,
        )
        previous_generation = generation
        content_hash = _string(
            shard_entry.get("content_hash"),
            E_V2_SCHEMA,
            "shard.content_hash",
            shard_id=shard_id,
        )
        shard_path = (manifest_path.parent / _string(
            shard_entry.get("path"),
            E_V2_SCHEMA,
            "shard.path",
            shard_id=shard_id,
        )).resolve()
        require(
            shard_path.is_file(),
            E_MISSING_FILE,
            "v2 shard file is missing",
            shard_id=shard_id,
            path=str(shard_path),
        )
        shard_document = load_json(shard_path, label=f"v2 shard {shard_id}")
        require(
            isinstance(shard_document, dict)
            and shard_document.get("format") == "v2-shard",
            E_V2_SCHEMA,
            "v2 shard has an invalid format",
            shard_id=shard_id,
        )
        require(
            shard_document.get("shard_id") == shard_id,
            E_V2_SCHEMA,
            "v2 shard ID does not match its manifest",
            shard_id=shard_id,
        )
        require(
            shard_document.get("generation") == generation,
            E_V2_GENERATION,
            "v2 shard generation does not match its manifest",
            shard_id=shard_id,
        )
        observed_hash = sha256_object(
            {key: value for key, value in shard_document.items() if key != "content_hash"}
        )
        declared_hash = shard_document.get("content_hash")
        require(
            observed_hash == declared_hash == content_hash,
            E_SHARD_HASH_MISMATCH,
            "v2 shard content hash mismatch",
            shard_id=shard_id,
            observed=observed_hash,
            declared=declared_hash,
            manifest=content_hash,
        )
        require(
            ordinal < len(metadata_shards),
            E_V2_METADATA,
            "v2 metadata is missing a shard row",
            shard_id=shard_id,
        )
        metadata_row = metadata_shards[ordinal]
        require(
            metadata_row["shard_id"] == shard_id
            and metadata_row["generation"] == generation
            and metadata_row["content_hash"] == content_hash,
            E_V2_METADATA,
            "v2 metadata shard row does not match manifest",
            shard_id=shard_id,
        )

        records = shard_document.get("nodes")
        require(
            isinstance(records, list),
            E_V2_SCHEMA,
            "v2 shard nodes must be an array",
            shard_id=shard_id,
        )
        nodes = tuple(
            _validate_v2_node(record, shard_id, generation, index)
            for index, record in enumerate(records)
        )
        shard_edges = [
            (node.canonical_id, edge.to_id, edge.label, edge.weight)
            for node in nodes
            for edge in node.out_edges
        ]
        shard_digest = digest_edges(shard_edges)
        require(
            metadata_row["adjacency_digest"] == shard_digest,
            E_V2_METADATA,
            "v2 shard adjacency digest mismatch",
            shard_id=shard_id,
            expected=shard_digest,
            stored=metadata_row["adjacency_digest"],
        )
        require(
            metadata_row["node_count"] == len(nodes),
            E_V2_METADATA,
            "v2 shard node count mismatch",
            shard_id=shard_id,
        )
        require(
            metadata_row["edge_count"] == len(shard_edges),
            E_V2_METADATA,
            "v2 shard edge count mismatch",
            shard_id=shard_id,
        )
        for node in nodes:
            require(
                node.canonical_id not in seen_node_ids,
                E_V2_DUPLICATE_CANONICAL_ID,
                "v2 contains duplicate canonical node IDs",
                node_id=node.canonical_id,
                shard_id=shard_id,
            )
            seen_node_ids.add(node.canonical_id)
        all_edges.extend(shard_edges)
        shards.append(
            V2Shard(
                shard_id=shard_id,
                generation=generation,
                content_hash=content_hash,
                path=str(shard_path),
                file_sha256=sha256_file(shard_path),
                nodes=nodes,
            )
        )

    require(
        global_generation >= previous_generation,
        E_V2_GENERATION,
        "v2 global generation predates a shard generation",
        global_generation=global_generation,
        latest_shard_generation=previous_generation,
    )
    observed_adjacency = digest_edges(all_edges)
    require(
        observed_adjacency == adjacency_digest,
        E_V2_METADATA,
        "v2 global adjacency digest mismatch",
        expected=observed_adjacency,
        stored=adjacency_digest,
    )
    return V2Snapshot(
        snapshot_version=snapshot_version,
        logical_clock_ms=logical_clock_ms,
        global_generation=global_generation,
        adjacency_digest=adjacency_digest,
        manifest_path=str(manifest_path),
        metadata_path=str(metadata_path),
        manifest_sha256=sha256_file(manifest_path),
        metadata_sha256=sha256_file(metadata_path),
        shards=tuple(shards),
    )

