from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import ErrorCode, fail
from .index import build_index
from .merge import merge_snapshots
from .models import (
    INDEX_SCHEMA,
    MERGE_SCHEMA,
    MergeResult,
    SnapshotV1,
    SnapshotV2,
    TraversalRequest,
    UnifiedIndex,
)
from .scheduler import execute_run, make_run_id, plan_batches
from .snapshot import load_snapshots
from .store import StateStore
from .util import canonical_json, digest_json


def _index_version_for(manifest: Any) -> str:
    value = digest_json(
        {
            "index_identity": manifest.index_identity,
            "merge_schema": MERGE_SCHEMA,
            "index_schema": INDEX_SCHEMA,
        }
    )
    return f"idx-{value[:32]}"


class GraphStore:
    def __init__(
        self,
        state_db: str | Path,
        *,
        lock_timeout: float = 30.0,
        clock_rollback_tolerance_ms: int = 0,
    ) -> None:
        self.state = StateStore(state_db, lock_timeout=lock_timeout)
        self.clock_rollback_tolerance_ms = clock_rollback_tolerance_ms

    def environment(self) -> dict[str, Any]:
        return self.state.validate_environment()

    def prepare(
        self,
        *,
        v1_path: str | Path,
        v2_path: str | Path,
        logical_clock_ms: int,
        v1_clock_scale_ms: int = 1000,
        v1_rounding: str = "half_up",
    ) -> tuple[SnapshotV1, SnapshotV2, MergeResult, UnifiedIndex]:
        v1, v2, manifest = load_snapshots(
            v1_path,
            v2_path,
            logical_clock_ms=logical_clock_ms,
            v1_clock_scale_ms=v1_clock_scale_ms,
            v1_rounding=v1_rounding,
        )
        merged = merge_snapshots(v1, v2, manifest)
        index = build_index(
            merged, index_version=_index_version_for(manifest)
        )
        return v1, v2, merged, index

    @staticmethod
    def _edge_conflicts(merged: MergeResult) -> list[dict[str, Any]]:
        return [
            {
                "node_id": node.node_id,
                "target": edge.target,
                "label": edge.label,
                "winner_source": edge.winner_source,
                "winner_weight": edge.weight,
                "sources": list(edge.sources),
                "candidates": list(edge.candidates),
            }
            for node_id in sorted(merged.nodes)
            for node in [merged.nodes[node_id]]
            for edge in node.edges
            if edge.conflict
        ]

    def load(
        self,
        *,
        v1_path: str | Path,
        v2_path: str | Path,
        logical_clock_ms: int,
        v1_clock_scale_ms: int = 1000,
        v1_rounding: str = "half_up",
    ) -> dict[str, Any]:
        v1, v2, merged, index = self.prepare(
            v1_path=v1_path,
            v2_path=v2_path,
            logical_clock_ms=logical_clock_ms,
            v1_clock_scale_ms=v1_clock_scale_ms,
            v1_rounding=v1_rounding,
        )
        return {
            "ok": True,
            "persisted": False,
            "v1": v1.summary(),
            "v2": v2.summary(),
            "package": merged.package_manifest.to_dict(),
            "merge": merged.summary(),
            "edge_conflicts": self._edge_conflicts(merged),
            "index": {
                "index_version": index.index_version,
                "index_digest": index.digest,
                "merge_digest": index.merge_digest,
                "node_count": index.summary["node_count"],
                "visible_node_count": index.summary["visible_node_count"],
                "tombstone_count": index.summary["tombstone_count"],
                "edge_count": index.summary["edge_count"],
                "label_prefixes": index.summary["label_prefixes"],
            },
        }

    def merge(
        self,
        *,
        v1_path: str | Path,
        v2_path: str | Path,
        logical_clock_ms: int,
        v1_clock_scale_ms: int = 1000,
        v1_rounding: str = "half_up",
    ) -> dict[str, Any]:
        _v1, _v2, merged, index = self.prepare(
            v1_path=v1_path,
            v2_path=v2_path,
            logical_clock_ms=logical_clock_ms,
            v1_clock_scale_ms=v1_clock_scale_ms,
            v1_rounding=v1_rounding,
        )
        migration = self.state.commit_index(
            index,
            node_decisions=merged.node_decisions,
            edge_decisions=merged.edge_decisions,
            clock_rollback_tolerance_ms=self.clock_rollback_tolerance_ms,
        )
        return {
            "ok": True,
            "migration": migration,
            "index": {
                "index_version": migration.get(
                    "index_version", index.index_version
                ),
                "index_digest": migration.get("index_digest", index.digest),
                "node_count": index.summary["node_count"],
                "visible_node_count": index.summary["visible_node_count"],
                "tombstone_count": index.summary["tombstone_count"],
                "edge_count": index.summary["edge_count"],
                "label_prefixes": index.summary["label_prefixes"],
            },
            "merge": merged.summary(),
            "edge_conflicts": self._edge_conflicts(merged),
        }

    def _validate_request(self, index: UnifiedIndex, request: TraversalRequest) -> None:
        if request.mode == "exact":
            index.exact(request.exact_node or "")
        elif request.mode == "reach":
            for start in request.starts:
                index.exact(start)

    def traverse(self, request: TraversalRequest) -> dict[str, Any]:
        index, _meta = self.state.load_index()
        self._validate_request(index, request)
        run_id = make_run_id(index, request)
        status = execute_run(self.state, index, request, run_id)
        run = self.state.get_run(run_id)
        return {
            "ok": True,
            "run_id": run_id,
            "status": status,
            "request": request.to_dict(),
            "result": run["result"],
        }

    def _request_from_run(self, run: dict[str, Any]) -> TraversalRequest:
        values = dict(run["request"])
        return TraversalRequest.create(
            mode=values["mode"],
            starts=values.get("starts", ()),
            exact_node=values.get("exact_node"),
            prefix=values.get("prefix"),
            max_depth=values["max_depth"],
            edge_labels=values.get("edge_labels", ()),
            min_edge_weight=values.get("min_edge_weight"),
            max_edge_weight=values.get("max_edge_weight"),
            max_parallel_tasks=values["max_parallel_tasks"],
            memory_budget_entries=values["memory_budget_entries"],
            batch_size=values["batch_size"],
        )

    def resume(self, run_id: str | None = None) -> dict[str, Any]:
        if run_id is None:
            latest = self.state.get_latest_run()
            if latest is None:
                raise fail(
                    ErrorCode.RUN_NOT_FOUND,
                    "there is no traversal run for the current index",
                )
            run_id = latest["run_id"]
        run = self.state.get_run(run_id)
        index, _meta = self.state.load_index(run["index_version"])
        request = self._request_from_run(run)
        status = execute_run(self.state, index, request, run_id)
        refreshed = self.state.get_run(run_id)
        return {
            "ok": True,
            "run_id": run_id,
            "resumed": True,
            "status": status,
            "request": request.to_dict(),
            "result": refreshed["result"],
        }

    def status(self, run_id: str | None = None) -> dict[str, Any]:
        return self.state.status(run_id)

    def decisions(self, index_version: str | None = None) -> dict[str, Any]:
        if index_version is None:
            meta = self.state.status()["index"]
            if meta is None:
                raise fail(
                    ErrorCode.INDEX_NOT_FOUND,
                    "no committed index exists",
                )
            index_version = meta["version"]
        return {
            "index_version": index_version,
            "decisions": self.state.list_merge_decisions(index_version),
        }

    def verify(
        self,
        *,
        run_id: str | None = None,
        v1_path: str | Path | None = None,
        v2_path: str | Path | None = None,
        logical_clock_ms: int | None = None,
        v1_clock_scale_ms: int = 1000,
        v1_rounding: str = "half_up",
    ) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        integrity = self.state.integrity_check()
        checks.append({"check": "sqlite_integrity", **integrity})
        if (
            integrity["integrity_check"] != "ok"
            or integrity["foreign_key_violations"] != 0
        ):
            raise fail(
                ErrorCode.INDEX_VERIFY_FAILED,
                "SQLite integrity check failed",
                **integrity,
            )

        if run_id is None:
            latest = self.state.get_latest_run()
            if latest is not None:
                run_id = latest["run_id"]
        run = self.state.get_run(run_id) if run_id is not None else None
        index_version = run["index_version"] if run is not None else None
        index, meta = self.state.load_index(index_version)
        checks.append(
            {
                "check": "index_rehydrate",
                "ok": True,
                "index_version": index.index_version,
                "index_digest": index.digest,
            }
        )

        if v1_path is not None or v2_path is not None or logical_clock_ms is not None:
            if (
                v1_path is None
                or v2_path is None
                or logical_clock_ms is None
            ):
                raise fail(
                    ErrorCode.INVALID_ARGUMENT,
                    "source verification requires v1_path, v2_path and logical_clock_ms",
                )
            _v1, _v2, _merged, rebuilt = self.prepare(
                v1_path=v1_path,
                v2_path=v2_path,
                logical_clock_ms=logical_clock_ms,
                v1_clock_scale_ms=v1_clock_scale_ms,
                v1_rounding=v1_rounding,
            )
            if rebuilt.digest != index.digest:
                raise fail(
                    ErrorCode.INDEX_VERIFY_FAILED,
                    "rebuilt index digest differs from persisted index",
                    expected=rebuilt.digest,
                    actual=index.digest,
                )
            checks.append(
                {
                    "check": "source_rebuild",
                    "ok": True,
                    "index_digest": rebuilt.digest,
                }
            )

        if run is not None:
            request = self._request_from_run(run)
            self._validate_request(index, request)
            plans = plan_batches(index, request)
            self.state.validate_batch_plans(run_id or "", plans)
            checks.append({"check": "batch_content_digests", "ok": True})
            batches = self.state.list_batches(run_id or "")
            verified = 0
            for batch in batches:
                if batch["state"] != "succeeded" or batch["result"] is None:
                    continue
                stored = dict(batch["result"])
                stored_digest = stored.pop("result_digest", None)
                if digest_json(stored) != stored_digest:
                    raise fail(
                        ErrorCode.CONTENT_DIGEST_MISMATCH,
                        "stored batch result digest does not match its payload",
                        batch_key=batch["batch_key"],
                    )
                if stored_digest != batch["result_digest"]:
                    raise fail(
                        ErrorCode.CONTENT_DIGEST_MISMATCH,
                        "stored batch result digest differs from committed digest",
                        batch_key=batch["batch_key"],
                        expected=stored_digest,
                        actual=batch["result_digest"],
                    )
                if request.mode == "reach":
                    from .traversal import execute_batch_traversals

                    replayed = execute_batch_traversals(
                        index, request, batch["starts"]
                    )
                else:
                    from .traversal import execute_query

                    replayed = execute_query(index, request)
                replayed = {
                    "batch_key": batch["batch_key"],
                    "batch_index": batch["batch_index"],
                    "content_digest": batch["content_digest"],
                    **replayed,
                }
                replayed["result_digest"] = digest_json(replayed)
                if canonical_json(replayed) != canonical_json(batch["result"]):
                    raise fail(
                        ErrorCode.TRAVERSAL_MISMATCH,
                        "replayed batch result differs from committed result",
                        batch_key=batch["batch_key"],
                    )
                verified += 1
            checks.append(
                {
                    "check": "committed_batch_replay",
                    "ok": True,
                    "verified_batches": verified,
                }
            )
            if run["state"] in {"completed", "completed_with_failures"}:
                outcomes = {
                    "started_succeeded": 0,
                    "started_failed": 0,
                    "abandoned": 0,
                    "not_started": 0,
                    "started_in_progress": 0,
                }
                results: list[dict[str, Any]] = []
                failures: list[dict[str, Any]] = []
                abandoned: list[dict[str, Any]] = []
                batch_digests: list[dict[str, Any]] = []
                for batch in batches:
                    if batch["state"] == "succeeded":
                        outcomes["started_succeeded"] += 1
                        results.append(batch["result"])
                    elif batch["state"] == "failed":
                        outcomes["started_failed"] += 1
                        failures.append(
                            {
                                "batch_key": batch["batch_key"],
                                "reason": batch["reason"],
                                "result": batch["result"],
                            }
                        )
                    elif batch["state"] == "abandoned":
                        outcomes["abandoned"] += 1
                        abandoned.append(
                            {
                                "batch_key": batch["batch_key"],
                                "reason": batch["reason"],
                            }
                        )
                    elif batch["state"] == "running":
                        outcomes["started_in_progress"] += 1
                    else:
                        outcomes["not_started"] += 1
                    batch_digests.append(
                        {
                            "batch_key": batch["batch_key"],
                            "content_digest": batch["content_digest"],
                            "result_digest": batch["result_digest"],
                            "state": batch["state"],
                        }
                    )
                expected_result = {
                    "run_id": run_id,
                    "index_version": run["index_version"],
                    "request_digest": run["request_digest"],
                    "outcomes": outcomes,
                    "failures": failures,
                    "abandoned": abandoned,
                    "batch_results": results,
                    "batch_digests": batch_digests,
                }
                expected_truth_digest = digest_json(
                    {
                        "run_id": run_id,
                        "index_version": run["index_version"],
                        "request_digest": run["request_digest"],
                        "batch_digests": batch_digests,
                    }
                )
                if canonical_json(expected_result) != canonical_json(run["result"]):
                    raise fail(
                        ErrorCode.CONTENT_DIGEST_MISMATCH,
                        "final run result differs from committed batch rows",
                        run_id=run_id,
                    )
                if run["truth_digest"] != expected_truth_digest:
                    raise fail(
                        ErrorCode.CONTENT_DIGEST_MISMATCH,
                        "final truth digest differs from committed batch digests",
                        run_id=run_id,
                    )
                checks.append(
                    {
                        "check": "final_run_result",
                        "ok": True,
                        "result_digest": digest_json(expected_result),
                        "truth_digest": expected_truth_digest,
                    }
                )

        return {
            "ok": True,
            "checks": checks,
            "index": {
                "index_version": index.index_version,
                "index_digest": index.digest,
                "node_count": index.summary["node_count"],
                "visible_node_count": index.summary["visible_node_count"],
                "edge_count": index.summary["edge_count"],
            },
            "run_id": run_id,
            "run_state": run["state"] if run is not None else None,
            "run_result_digest": run["result_digest"] if run is not None else None,
        }

