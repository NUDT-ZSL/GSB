"""The per-page computation DAG and content-addressed incremental execution."""

from __future__ import annotations

from typing import Any, Callable

from .annotations import (
    AnnotationDocument,
    AnnotationRecord,
    default_role,
    render_annotations,
)
from .cache import ContentAddressedCache
from .canonical import round_float, sha256_obj
from .costs import (
    costed_edges_fingerprint,
    normalize_cost_config,
    score_edges,
    validate_cost_details,
)
from .errors import LayoutError
from .geometry import (
    build_candidate_edges,
    edge_set_fingerprint,
    geometry_fingerprint,
    normalize_geometry_config,
    normalize_input_fingerprint,
    normalize_limits,
    normalize_page,
    type_fingerprint,
    validate_candidate_coverage,
)
from .snapshot import LayoutSnapshot
from .solver import compute_metrics, solve_order, validate_order

STAGES = ("normalize", "edges", "costs", "solve", "metrics")


def build_pipeline_graph(page_ids: list[str]) -> dict[str, Any]:
    nodes: list[str] = []
    edges: list[dict[str, str]] = []
    for page_id in page_ids:
        for index, stage in enumerate(STAGES):
            node = f"{stage}:{page_id}"
            nodes.append(node)
            if index:
                edges.append(
                    {
                        "from": f"{STAGES[index - 1]}:{page_id}",
                        "to": node,
                    }
                )
    adjacency = {node: [] for node in nodes}
    indegree = {node: 0 for node in nodes}
    for edge in edges:
        adjacency[edge["from"]].append(edge["to"])
        indegree[edge["to"]] += 1
    ready = sorted(node for node, degree in indegree.items() if degree == 0)
    topological: list[str] = []
    while ready:
        node = ready.pop(0)
        topological.append(node)
        for target in sorted(adjacency[node]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()
    return {
        "nodes": sorted(nodes),
        "edges": edges,
        "topological_order": topological,
        "acyclic": len(topological) == len(nodes),
    }


class LayoutRunner:
    def __init__(
        self,
        cache: ContentAddressedCache | None = None,
        *,
        geometry_config: dict[str, Any] | None = None,
        limits: dict[str, Any] | None = None,
        verify: bool = True,
    ) -> None:
        self.cache = cache or ContentAddressedCache()
        self.geometry_config = normalize_geometry_config(geometry_config)
        self.limits = normalize_limits(limits)
        self.verify = verify

    @staticmethod
    def _snapshot_pages(snapshot: LayoutSnapshot) -> dict[str, dict[str, Any]]:
        pages = snapshot.section("block_annotations").content
        if not isinstance(pages, dict):
            raise LayoutError(
                "E_FORMAT_INVALID",
                "block_annotations snapshot content must map page ids to pages",
                field="block_annotations.content",
            )
        return {str(page_id): page for page_id, page in pages.items()}

    @staticmethod
    def _cost_config(snapshot: LayoutSnapshot) -> dict[str, Any]:
        type_order = snapshot.section("type_order").content
        edge_weights = snapshot.section("edge_weights").content
        if not isinstance(type_order, dict):
            raise LayoutError(
                "E_CONFIG_INVALID",
                "type_order snapshot content must be an object",
                field="type_order.content",
            )
        if not isinstance(edge_weights, dict):
            raise LayoutError(
                "E_CONFIG_INVALID",
                "edge_weights snapshot content must be an object",
                field="edge_weights.content",
            )
        config = dict(edge_weights)
        config["type_order"] = type_order
        return normalize_cost_config(config)

    def _cached(
        self,
        *,
        node: str,
        page_id: str,
        dependencies: dict[str, Any],
        config: dict[str, Any],
        compute: Callable[[], Any],
    ) -> tuple[Any, str, str]:
        key = self.cache.make_key(node, page_id, dependencies, config)
        cached = self.cache.get(key)
        if cached is not None:
            return cached, "reused", key
        value = compute()
        self.cache.put(
            key,
            value,
            metadata={"node": node, "page_id": page_id},
        )
        return value, "recomputed", key

    def _annotation_views(
        self,
        page_id: str,
        normalized: dict[str, Any],
        sequence: list[int],
    ) -> dict[str, str]:
        by_id = {block["block_id"]: block for block in normalized["blocks"]}
        records: list[AnnotationRecord] = []
        for position, block_id in enumerate(sequence):
            block = by_id[block_id]
            records.append(
                AnnotationRecord(
                    block_id=block_id,
                    reading_order=position,
                    block_type=block["block_type"],
                    text_len=block["text_len"],
                    confidence_low=0.0,
                    confidence_high=1.0,
                    paragraph_role=default_role(block["block_type"]),
                    order_weight=1.0,
                    column_index=block["column_index"],
                    zone_id=block["zone_id"],
                    nesting_path=block["nesting_path"],
                )
            )
        document = AnnotationDocument(
            version="v3",
            records=records,
            source=f"predicted:{page_id}",
        )
        return {
            "v1": render_annotations(document, "v1"),
            "v2": render_annotations(document, "v2"),
            "v3": render_annotations(document, "v3"),
        }

    def run(self, snapshot: LayoutSnapshot, *, mode: str = "baseline") -> dict[str, Any]:
        pages = self._snapshot_pages(snapshot)
        cost_config = self._cost_config(snapshot)
        page_ids = sorted(pages)
        graph = build_pipeline_graph(page_ids)
        if not graph["acyclic"]:
            raise LayoutError(
                "E_ORDER_INCONSISTENT",
                "computation graph contains a cycle",
            )
        node_status: dict[str, dict[str, Any]] = {}
        page_reports: list[dict[str, Any]] = []
        degradations: list[dict[str, Any]] = []
        for page_id in page_ids:
            page = pages[page_id]
            page_with_id = dict(page)
            page_with_id.setdefault("page_id", page_id)
            normalized, status, key = self._cached(
                node=f"normalize:{page_id}",
                page_id=page_id,
                dependencies={"input": normalize_input_fingerprint(page_with_id)},
                config={"geometry": self.geometry_config},
                compute=lambda page_with_id=page_with_id: normalize_page(
                    page_with_id, self.geometry_config
                ),
            )
            node_status[f"normalize:{page_id}"] = {
                "status": status,
                "key": key,
                "page_id": page_id,
                "stage": "normalize",
            }
            geom_fp = geometry_fingerprint(normalized)
            edge_cfg = {
                "geometry": self.geometry_config,
                "limits": self.limits,
            }
            edge_artifact, status, key = self._cached(
                node=f"edges:{page_id}",
                page_id=page_id,
                dependencies={"geometry": geom_fp},
                config=edge_cfg,
                compute=lambda normalized=normalized: build_candidate_edges(
                    normalized, self.geometry_config, self.limits
                ),
            )
            node_status[f"edges:{page_id}"] = {
                "status": status,
                "key": key,
                "page_id": page_id,
                "stage": "edges",
            }
            candidate_edges = edge_artifact["edges"]
            if self.verify:
                validate_candidate_coverage(normalized, candidate_edges)
            if edge_artifact.get("degraded", {}).get("active"):
                degradation = dict(edge_artifact["degraded"])
                degradation["page_id"] = page_id
                degradation["node"] = f"edges:{page_id}"
                degradations.append(degradation)
            edge_fp = edge_set_fingerprint(candidate_edges)
            type_fp = type_fingerprint(normalized)
            scored_edges, status, key = self._cached(
                node=f"costs:{page_id}",
                page_id=page_id,
                dependencies={"edges": edge_fp, "types": type_fp},
                config={"cost": cost_config},
                compute=lambda normalized=normalized, candidate_edges=candidate_edges: score_edges(
                    normalized, candidate_edges, cost_config
                ),
            )
            node_status[f"costs:{page_id}"] = {
                "status": status,
                "key": key,
                "page_id": page_id,
                "stage": "costs",
            }
            if self.verify:
                validate_cost_details(
                    normalized,
                    candidate_edges,
                    scored_edges,
                    cost_config,
                )
            scored_fp = costed_edges_fingerprint(scored_edges)
            order_result, status, key = self._cached(
                node=f"solve:{page_id}",
                page_id=page_id,
                dependencies={"costed_edges": scored_fp},
                config={"max_blocks_per_page": self.limits["max_blocks_per_page"]},
                compute=lambda normalized=normalized, scored_edges=scored_edges: solve_order(
                    normalized, scored_edges, self.limits
                ),
            )
            node_status[f"solve:{page_id}"] = {
                "status": status,
                "key": key,
                "page_id": page_id,
                "stage": "solve",
            }
            if self.verify:
                validate_order(
                    normalized,
                    order_result["sequence"],
                    scored_edges,
                    expected_total=order_result["total_cost"],
                    recovered_edges=order_result["edges"],
                )
            from .geometry import true_order_fingerprint

            true_fp = true_order_fingerprint(page_with_id)
            metrics, status, key = self._cached(
                node=f"metrics:{page_id}",
                page_id=page_id,
                dependencies={"solve": sha256_obj(order_result), "true_order": true_fp},
                config={"precision": 8},
                compute=lambda normalized=normalized,
                page_with_id=page_with_id,
                order_result=order_result: compute_metrics(
                    normalized,
                    page_with_id,
                    order_result["sequence"],
                    order_result["total_cost"],
                    order_result["edges"],
                ),
            )
            node_status[f"metrics:{page_id}"] = {
                "status": status,
                "key": key,
                "page_id": page_id,
                "stage": "metrics",
            }
            page_reports.append(
                {
                    "page_id": page_id,
                    "canvas_shape": normalized["canvas_shape"],
                    "blocks": normalized["blocks"],
                    "candidate_edges": candidate_edges,
                    "costed_edges": scored_edges,
                    "reading_order": order_result["sequence"],
                    "reading_order_true": metrics["reading_order_true"],
                    "total_cost": order_result["total_cost"],
                    "edge_cost_chain": [
                        {
                            "source_block_id": edge["source_block_id"],
                            "target_block_id": edge["target_block_id"],
                            "total_cost": edge["total_cost"],
                            "components": edge["components"],
                        }
                        for edge in order_result["edges"]
                    ],
                    "rule_hits": metrics["rule_hits"],
                    "metrics": metrics,
                    "annotation_views": self._annotation_views(
                        page_id, normalized, order_result["sequence"]
                    ),
                    "degraded": edge_artifact.get("degraded", {"active": False}),
                }
            )
        recomputed = sorted(
            node for node, item in node_status.items() if item["status"] == "recomputed"
        )
        reused = sorted(
            node for node, item in node_status.items() if item["status"] == "reused"
        )
        recomputed_pages = sorted(
            {node_status[node]["page_id"] for node in recomputed}
        )
        reused_pages = sorted({node_status[node]["page_id"] for node in reused})
        geometry_affected_pages = sorted(
            {
                node_status[node]["page_id"]
                for node in recomputed
                if node_status[node]["stage"] in {"normalize", "edges"}
            }
        )
        suffix_ok = True
        for page_id in recomputed_pages:
            statuses = {
                stage: node_status[f"{stage}:{page_id}"]["status"] for stage in STAGES
            }
            for upper, lower in (("costs", "solve"), ("solve", "metrics")):
                if statuses[upper] == "recomputed" and statuses[lower] != "recomputed":
                    suffix_ok = False
        execution = {
            "mode": mode,
            "recomputed_nodes": recomputed,
            "reused_nodes": reused,
            "node_status": {node: node_status[node] for node in sorted(node_status)},
            "recomputed_pages": recomputed_pages,
            "reused_pages": reused_pages,
            "affected_pages": recomputed_pages,
            "geometry_affected_pages": geometry_affected_pages,
            "downstream_only_pages": sorted(
                set(recomputed_pages) - set(geometry_affected_pages)
            ),
            "unaffected_pages": sorted(set(page_ids) - set(recomputed_pages)),
            "recompute_reuse_disjoint": not (set(recomputed) & set(reused)),
            "node_partition_complete": sorted(set(recomputed) | set(reused))
            == sorted(graph["nodes"]),
            "suffix_recomputed": suffix_ok,
        }
        total_cost = round_float(
            sum(float(page["total_cost"]) for page in page_reports), 8
        )
        exact_matches = sum(
            1 for page in page_reports if page["metrics"]["exact_order_match"] is True
        )
        summary = {
            "page_count": len(page_reports),
            "block_count": sum(len(page["blocks"]) for page in page_reports),
            "candidate_edge_count": sum(
                len(page["candidate_edges"]) for page in page_reports
            ),
            "total_order_cost": total_cost,
            "exact_order_match_pages": exact_matches,
            "all_orders_verified": all(
                page["metrics"]["total_cost"]
                == page["metrics"]["total_cost_recomputed"]
                for page in page_reports
            ),
            "degraded": bool(degradations),
        }
        return {
            "schema_version": 1,
            "mode": mode,
            "snapshot": snapshot.metadata(),
            "graph": graph,
            "summary": summary,
            "pages": page_reports,
            "execution": execution,
            "degradations": degradations,
            "business_codes": ["OK"] if not degradations else ["OK", "DEGRADED"],
        }


def report_content_fingerprint(report: dict[str, Any]) -> str:
    """Fingerprint algorithmic outputs while excluding cache hit/miss accounting."""

    return sha256_obj(
        {
            "snapshot": report["snapshot"],
            "summary": report["summary"],
            "pages": report["pages"],
            "degradations": report["degradations"],
        }
    )

