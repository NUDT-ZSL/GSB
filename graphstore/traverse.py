from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .checksums import canonical_json, is_finite_number, is_int, sha256_object
from .errors import (
    E_BATCH_CONFLICT,
    E_BATCH_NOT_FOUND,
    E_INVALID_DEPTH,
    E_INVALID_EDGE_LABEL,
    E_INVALID_QUERY,
    E_NODE_NOT_FOUND,
    E_STATE_CORRUPT,
    GraphStoreError,
    require,
)
from .model import GraphIndex, UnifiedEdge, UnifiedNode
from .storage import FileLock, GraphStore, maybe_crash


MAX_DEPTH = 16


@dataclass(frozen=True)
class TraversalOutcome:
    result: dict[str, Any]
    idempotent: bool
    resumed_from_depth: int

    def to_dict(self) -> dict[str, Any]:
        return self.result


def _query_string(value: Any, field: str) -> str:
    require(
        isinstance(value, str) and bool(value.strip()),
        E_INVALID_QUERY,
        f"{field} must be a non-empty string",
        field=field,
        value=value,
    )
    return value


def _query_depth(value: Any, *, maximum: int = MAX_DEPTH) -> int:
    require(
        is_int(value) and value > 0,
        E_INVALID_DEPTH,
        "depth must be a positive integer",
        value=value,
    )
    require(
        value <= maximum,
        E_INVALID_DEPTH,
        "depth exceeds the supported maximum",
        value=value,
        maximum=maximum,
    )
    return value


def _edge_labels(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    require(
        isinstance(value, (list, tuple)),
        E_INVALID_EDGE_LABEL,
        "edge_labels must be an array of strings",
        value=value,
    )
    labels: list[str] = []
    for item in value:
        require(
            isinstance(item, str) and bool(item.strip()),
            E_INVALID_EDGE_LABEL,
            "edge label must be a non-empty string",
            value=item,
        )
        labels.append(item)
    return tuple(sorted(set(labels)))


def _optional_weight(value: Any, field: str) -> float | None:
    if value is None:
        return None
    require(
        is_finite_number(value),
        E_INVALID_QUERY,
        f"{field} must be a finite number",
        field=field,
        value=value,
    )
    return float(value)


def normalize_query(query: dict[str, Any]) -> dict[str, Any]:
    require(
        isinstance(query, dict),
        E_INVALID_QUERY,
        "query must be an object",
    )
    kind = query.get("kind")
    if kind == "one_hop":
        kind = "reachable"
        query = {**query, "depth": 1}
    elif kind == "two_hop":
        kind = "reachable"
        query = {**query, "depth": 2}
    require(
        kind in {"exact", "label_prefix", "reachable", "filtered"},
        E_INVALID_QUERY,
        "query kind is not supported",
        kind=kind,
    )
    if kind == "exact":
        return {
            "audit": True,
            "kind": "exact",
            "node_id": _query_string(query.get("node_id"), "node_id"),
        }
    if kind == "label_prefix":
        prefix = query.get("prefix")
        require(
            isinstance(prefix, str),
            E_INVALID_QUERY,
            "prefix must be a string",
            value=prefix,
        )
        return {
            "include_tombstones": bool(query.get("include_tombstones", False)),
            "kind": "label_prefix",
            "prefix": prefix,
        }
    if kind == "reachable":
        depth = _query_depth(query.get("depth"), maximum=2)
        return {
            "depth": depth,
            "kind": "reachable",
            "node_id": _query_string(query.get("node_id"), "node_id"),
        }
    depth = _query_depth(query.get("depth"))
    labels = _edge_labels(query.get("edge_labels"))
    minimum = _optional_weight(query.get("min_weight"), "min_weight")
    maximum = _optional_weight(query.get("max_weight"), "max_weight")
    require(
        minimum is None or maximum is None or minimum <= maximum,
        E_INVALID_QUERY,
        "min_weight must not exceed max_weight",
        min_weight=minimum,
        max_weight=maximum,
    )
    normalized: dict[str, Any] = {
        "depth": depth,
        "edge_labels": list(labels),
        "kind": "filtered",
        "node_id": _query_string(query.get("node_id"), "node_id"),
    }
    if minimum is not None:
        normalized["min_weight"] = minimum
    if maximum is not None:
        normalized["max_weight"] = maximum
    return normalized


def query_digest(query: dict[str, Any]) -> str:
    return sha256_object(query)


def _node_result(
    node: UnifiedNode,
    *,
    path: list[str],
    edge_labels: list[str],
    depth: int,
    audit: bool,
) -> dict[str, Any]:
    payload = node.public_dict(audit=audit)
    if audit:
        payload["audit"] = True
    payload["depth"] = depth
    payload["edge_labels"] = edge_labels
    payload["path"] = path
    return payload


def _edge_allowed(
    edge: UnifiedEdge,
    labels: tuple[str, ...],
    minimum: float | None,
    maximum: float | None,
) -> bool:
    if labels and edge.label not in labels:
        return False
    if minimum is not None and edge.weight < minimum:
        return False
    if maximum is not None and edge.weight > maximum:
        return False
    return True


def _sort_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        results,
        key=lambda item: (
            int(item.get("depth", 0)),
            item["node_id"],
            tuple(item.get("path", [])),
            tuple(item.get("edge_labels", [])),
        ),
    )


def _sort_truncations(truncations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        truncations,
        key=lambda item: (
            len(item.get("path", [])),
            tuple(item.get("path", [])),
            item["reason"],
            tuple(item.get("edge_labels", [])),
        ),
    )


class TraversalRunner:
    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def _batch_lock(self, batch_id: str) -> FileLock:
        suffix = sha256_object({"batch_id": batch_id})[:20]
        return FileLock(
            self.store.db_path.with_name(
                f"{self.store.db_path.name}.batch-{suffix}.lock"
            )
        )

    def execute(
        self,
        *,
        batch_id: str,
        query: dict[str, Any] | None = None,
        resume_only: bool = False,
    ) -> TraversalOutcome:
        require(
            self.store.exists,
            E_INVALID_QUERY,
            "graph store does not exist; run load first",
        )
        require(
            isinstance(batch_id, str) and bool(batch_id),
            E_INVALID_QUERY,
            "batch_id must be a non-empty string",
        )
        self.store.assert_clock_compatible()
        supplied_query = normalize_query(query) if query is not None else None
        with self._batch_lock(batch_id):
            with self.store.connection() as connection:
                row = connection.execute(
                    "SELECT * FROM traverse_batches WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()
                resume_depth = -1
                if row is None:
                    require(
                        not resume_only,
                        E_BATCH_NOT_FOUND,
                        "traversal batch does not exist",
                        batch_id=batch_id,
                    )
                    require(
                        query is not None,
                        E_INVALID_QUERY,
                        "query is required for a new batch",
                    )
                    assert supplied_query is not None
                    normalized = supplied_query
                    digest = query_digest(normalized)
                    index = self.store.load_index()
                    index_version = index.index_version
                    progress = self._initial_progress(normalized, index)
                    content_digest = self.store.batch_content_digest(
                        index_version, digest
                    )
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        now = self.store._advance_clock(connection)
                        connection.execute(
                            """
                            INSERT INTO traverse_batches(
                                batch_id, index_version, query_digest,
                                batch_content_digest, query_json, status,
                                next_depth, progress_json, created_clock_ms,
                                updated_clock_ms
                            ) VALUES (?, ?, ?, ?, ?, 'prepared', 0, ?, ?, ?)
                            """,
                            (
                                batch_id,
                                index_version,
                                digest,
                                content_digest,
                                canonical_json(normalized),
                                canonical_json(progress),
                                now,
                                now,
                            ),
                        )
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    maybe_crash("traverse_after_prepare")
                    stored_query = normalized
                else:
                    stored_query = normalize_query(json.loads(row["query_json"]))
                    if supplied_query is not None:
                        require(
                            supplied_query == stored_query,
                            E_BATCH_CONFLICT,
                            "batch ID is already bound to a different query",
                            batch_id=batch_id,
                        )
                    if row["status"] == "committed":
                        result = json.loads(row["result_json"])
                        require(
                            sha256_object(result) == row["result_digest"],
                            E_STATE_CORRUPT,
                            "committed batch result digest is invalid",
                            batch_id=batch_id,
                        )
                        return TraversalOutcome(
                            result=result,
                            idempotent=True,
                            resumed_from_depth=-1,
                        )
                    index_version = int(row["index_version"])
                    index = self.store.load_index(index_version)
                    progress = json.loads(row["progress_json"])
                    resume_depth = int(row["next_depth"])

                require(
                    query_digest(stored_query) == (
                        row["query_digest"] if row is not None else digest
                    ),
                    E_BATCH_CONFLICT,
                    "stored query digest does not match",
                    batch_id=batch_id,
                )
                progress = self._complete_unfinished_steps(
                    connection, batch_id, stored_query, index, progress
                )
                maybe_crash("traverse_before_commit")
                result = self._build_result(batch_id, stored_query, index, progress)
                result_digest = sha256_object(result)
                stats = result["stats"]
                connection.execute("BEGIN IMMEDIATE")
                try:
                    current = connection.execute(
                        "SELECT status FROM traverse_batches WHERE batch_id = ?",
                        (batch_id,),
                    ).fetchone()
                    require(
                        current is not None and current["status"] != "committed",
                        E_STATE_CORRUPT,
                        "batch was committed by an unexpected writer",
                        batch_id=batch_id,
                    )
                    now = self.store._advance_clock(connection)
                    connection.execute(
                        """
                        UPDATE traverse_batches
                        SET status = 'committed', result_json = ?,
                            result_digest = ?, stats_json = ?,
                            updated_clock_ms = ?, committed_clock_ms = ?
                        WHERE batch_id = ?
                        """,
                        (
                            canonical_json(result),
                            result_digest,
                            canonical_json(stats),
                            now,
                            now,
                            batch_id,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO traverse_watermarks(
                            batch_id, index_version, batch_content_digest,
                            result_digest, committed_clock_ms
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            batch_id,
                            index_version,
                            self.store.batch_content_digest(
                                index_version, query_digest(stored_query)
                            ),
                            result_digest,
                            now,
                        ),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                maybe_crash("traverse_after_commit")
                return TraversalOutcome(
                    result=result,
                    idempotent=False,
                    resumed_from_depth=resume_depth,
                )

    def _initial_progress(
        self, query: dict[str, Any], index: GraphIndex
    ) -> dict[str, Any]:
        kind = query["kind"]
        if kind in {"exact", "label_prefix"}:
            if kind == "exact":
                require(
                    query["node_id"] in index.nodes,
                    E_NODE_NOT_FOUND,
                    "node does not exist",
                    node_id=query["node_id"],
                )
            return {
                "frontier": [],
                "results": [],
                "selection_done": False,
                "truncations": [],
                "visited": [],
            }
        start_id = query["node_id"]
        require(
            start_id in index.nodes,
            E_NODE_NOT_FOUND,
            "start node does not exist",
            node_id=start_id,
        )
        start = index.nodes[start_id]
        progress: dict[str, Any] = {
            "frontier": [],
            "results": [],
            "selection_done": True,
            "truncations": [],
            "visited": [start_id],
        }
        if start.tombstone:
            progress["truncations"].append(
                {
                    "edge_labels": [],
                    "node_id": start_id,
                    "path": [start_id],
                    "reason": "tombstone",
                    "source": start.source.to_dict(),
                    "truncated": True,
                }
            )
        else:
            progress["frontier"] = [
                {"edge_labels": [], "node_id": start_id, "path": [start_id]}
            ]
        return progress

    def _complete_unfinished_steps(
        self,
        connection: sqlite3.Connection,
        batch_id: str,
        query: dict[str, Any],
        index: GraphIndex,
        progress: dict[str, Any],
    ) -> dict[str, Any]:
        if not progress.get("selection_done", False):
            progress = self._select_nodes(query, index, progress)
            progress["selection_done"] = True
            progress = self._checkpoint(
                connection, batch_id, 0, progress, "traverse_after_selection"
            )
        if query["kind"] in {"exact", "label_prefix"}:
            return progress

        target_depth = int(query["depth"])
        row = connection.execute(
            "SELECT next_depth FROM traverse_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        next_depth = int(row["next_depth"])
        executed_layers = 0
        while next_depth < target_depth and progress["frontier"]:
            progress = self._expand_layer(query, index, progress, next_depth)
            next_depth += 1
            executed_layers += 1
            progress = self._checkpoint(
                connection,
                batch_id,
                next_depth,
                progress,
                f"traverse_after_layer_{executed_layers}",
            )
        return progress

    def _checkpoint(
        self,
        connection: sqlite3.Connection,
        batch_id: str,
        next_depth: int,
        progress: dict[str, Any],
        crash_point: str,
    ) -> dict[str, Any]:
        progress = {
            **progress,
            "frontier": sorted(
                progress["frontier"],
                key=lambda item: (
                    item["node_id"],
                    tuple(item["path"]),
                    tuple(item["edge_labels"]),
                ),
            ),
            "results": _sort_results(progress["results"]),
            "truncations": _sort_truncations(progress["truncations"]),
            "visited": sorted(set(progress["visited"])),
        }
        connection.execute("BEGIN IMMEDIATE")
        try:
            now = self.store._advance_clock(connection)
            connection.execute(
                """
                UPDATE traverse_batches
                SET status = 'running', next_depth = ?, progress_json = ?,
                    updated_clock_ms = ?
                WHERE batch_id = ? AND status != 'committed'
                """,
                (next_depth, canonical_json(progress), now, batch_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        maybe_crash(crash_point)
        return progress

    def _select_nodes(
        self,
        query: dict[str, Any],
        index: GraphIndex,
        progress: dict[str, Any],
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        if query["kind"] == "exact":
            node = index.exact(query["node_id"])
            require(
                node is not None,
                E_NODE_NOT_FOUND,
                "node does not exist",
                node_id=query["node_id"],
            )
            results.append(
                _node_result(
                    node,
                    path=[node.node_id],
                    edge_labels=[],
                    depth=0,
                    audit=True,
                )
            )
            visited = [node.node_id]
        else:
            include_tombstones = bool(query["include_tombstones"])
            node_ids = index.label_prefix(query["prefix"])
            visited = list(node_ids)
            for node_id in node_ids:
                node = index.nodes[node_id]
                if node.tombstone and not include_tombstones:
                    continue
                results.append(
                    _node_result(
                        node,
                        path=[node.node_id],
                        edge_labels=[],
                        depth=0,
                        audit=node.tombstone,
                    )
                )
        return {
            **progress,
            "results": _sort_results(results),
            "truncations": [],
            "visited": sorted(set(visited)),
        }

    def _expand_layer(
        self,
        query: dict[str, Any],
        index: GraphIndex,
        progress: dict[str, Any],
        current_depth: int,
    ) -> dict[str, Any]:
        labels = tuple(query.get("edge_labels", []))
        minimum = query.get("min_weight")
        maximum = query.get("max_weight")
        visited = set(progress["visited"])
        results = list(progress["results"])
        truncations = list(progress["truncations"])
        next_frontier: list[dict[str, Any]] = []
        for state in progress["frontier"]:
            node_id = state["node_id"]
            node = index.nodes.get(node_id)
            if node is None or node.tombstone:
                continue
            for edge in index.neighbors(node_id):
                if not _edge_allowed(edge, labels, minimum, maximum):
                    continue
                target = index.nodes.get(edge.to_id)
                target_path = list(state["path"]) + [edge.to_id]
                target_labels = list(state["edge_labels"]) + [edge.label]
                if target is None:
                    truncations.append(
                        {
                            "edge_labels": target_labels,
                            "edge_source": edge.source.to_dict(),
                            "node_id": edge.to_id,
                            "path": target_path,
                            "reason": "missing_target",
                            "truncated": True,
                        }
                    )
                    continue
                if target.tombstone:
                    truncations.append(
                        {
                            "edge_labels": target_labels,
                            "edge_source": edge.source.to_dict(),
                            "node_id": target.node_id,
                            "path": target_path,
                            "reason": "tombstone",
                            "source": target.source.to_dict(),
                            "truncated": True,
                        }
                    )
                    continue
                if target.node_id in visited:
                    continue
                visited.add(target.node_id)
                next_depth = current_depth + 1
                results.append(
                    _node_result(
                        target,
                        path=target_path,
                        edge_labels=target_labels,
                        depth=next_depth,
                        audit=False,
                    )
                )
                next_frontier.append(
                    {
                        "edge_labels": target_labels,
                        "node_id": target.node_id,
                        "path": target_path,
                    }
                )
        return {
            **progress,
            "frontier": next_frontier,
            "results": results,
            "truncations": truncations,
            "visited": sorted(visited),
        }

    def _build_result(
        self,
        batch_id: str,
        query: dict[str, Any],
        index: GraphIndex,
        progress: dict[str, Any],
    ) -> dict[str, Any]:
        results = _sort_results(progress["results"])
        truncations = _sort_truncations(progress["truncations"])
        visited = sorted(set(progress["visited"]))
        result = {
            "batch_id": batch_id,
            "index_version": index.index_version,
            "query": query,
            "query_digest": query_digest(query),
            "results": results,
            "stats": {
                "emitted": len(results),
                "truncated": len(truncations),
                "visited": len(visited),
            },
            "truncations": truncations,
        }
        return result


def run_traversal(
    store: GraphStore,
    *,
    batch_id: str,
    query: dict[str, Any] | None = None,
    resume_only: bool = False,
) -> TraversalOutcome:
    return TraversalRunner(store).execute(
        batch_id=batch_id, query=query, resume_only=resume_only
    )

