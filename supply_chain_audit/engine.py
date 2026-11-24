"""Merged integrity view and scoped incremental recomputation."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .analysis import analyze_key, derive_status
from .codes import (
    ALGORITHM_WHITELIST_VERSION,
    CHECKPOINT_SCHEMA_VERSION,
    E_RECOMPUTE_DIVERGE,
    MERGE_POLICY_VERSION,
)
from .graph import DependencyGraph, build_invalidation_index
from .io_utils import canonical_json


def stable_sort_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=canonical_json)


class AuditEngine:
    """Stateful ledger with full and scoped incremental view construction."""

    def __init__(self, clock: dict[str, Any] | None = None, dependencies: dict[str, Any] | None = None):
        self.clock = clock or {}
        self.dependencies = dependencies or {"edges": [], "rejections": []}
        self.graph = DependencyGraph.from_edges(self.dependencies.get("edges", []))
        self.claims_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.rejections_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.revocations_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.rejections: list[dict[str, Any]] = list(self.dependencies.get("rejections", []))
        self.fatal_errors: list[dict[str, Any]] = []
        self.applied_batches: list[dict[str, Any]] = []
        self.watermark = 0
        self.analyses: dict[str, dict[str, Any]] = {}
        self.source_reasons: dict[str, str] = {}
        self.invalidation_index: dict[str, list[dict[str, Any]]] = {}
        self.closure_sets: dict[str, list[str]] = {}
        self.cycles = self.graph.find_cycles()
        self.cycle_keys = {member for cycle in self.cycles for member in cycle.get("members", [])}
        self.view = self.build_full_view()

    def _all_keys(self) -> set[str]:
        return (
            set(self.claims_by_key)
            | set(self.rejections_by_key)
            | set(self.revocations_by_key)
            | self.graph.nodes
        )

    def _add_rejections(self, rejections: list[dict[str, Any]]) -> set[str]:
        changed = set()
        for record in rejections:
            self.rejections.append(record)
            key = record.get("artifact_key")
            if isinstance(key, str) and key and key != "<missing>@<missing>":
                self.rejections_by_key[key].append(record)
                changed.add(key)
        return changed

    def ingest_parse_result(self, result: Any, batch_id: str | None = None) -> set[str]:
        changed = set()
        effective_batch = batch_id or getattr(result, "batch_id", None)
        if getattr(result, "fatal_error", None):
            fatal = dict(result.fatal_error)
            fatal["snapshot_id"] = getattr(result, "snapshot_id", "<unknown>")
            fatal["batch_id"] = effective_batch
            self.fatal_errors.append(fatal)
        changed.update(self._add_rejections(getattr(result, "rejections", [])))
        for claim in getattr(result, "claims", []):
            if effective_batch:
                claim["batch_id"] = effective_batch
            key = claim["artifact_key"]
            self.claims_by_key[key].append(claim)
            changed.add(key)
        return changed

    def ingest_revocation_batch(self, batch: dict[str, Any]) -> set[str]:
        changed = self._add_rejections(batch.get("rejections", []))
        batch_id = batch.get("batch_id")
        for entry in batch.get("revocations", []):
            entry = dict(entry)
            entry.setdefault("batch_id", batch_id)
            key = entry["artifact_key"]
            self.revocations_by_key[key].append(entry)
            changed.add(key)
        return changed

    def _analysis_for(self, key: str) -> dict[str, Any]:
        return analyze_key(
            key,
            self.claims_by_key.get(key, []),
            self.rejections_by_key.get(key, []),
            self.revocations_by_key.get(key, []),
        )

    def _build_artifact(
        self,
        key: str,
        analysis: dict[str, Any],
        invalidation_index: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        claims = self.claims_by_key.get(key, [])
        rejections = self.rejections_by_key.get(key, [])
        invalidations = invalidation_index.get(key, [])
        status, codes = derive_status(
            key,
            claims,
            rejections,
            analysis,
            invalidations,
            self.cycle_keys,
            self.graph.nodes,
        )
        current = analysis.get("current_claim") or {}
        name, version = (key.rsplit("@", 1) + [""])[:2] if "@" in key else (key, "")
        relevant_cycles = [
            cycle for cycle in self.cycles if key in cycle.get("members", [])
        ]
        snapshot_ids = sorted({claim["snapshot_id"] for claim in claims if claim.get("snapshot_id")})
        batch_ids = sorted({claim["batch_id"] for claim in claims if claim.get("batch_id")})
        signature_statuses = sorted(
            {claim["signature_status"] for claim in claims if claim.get("signature_status")}
        )
        artifact = {
            "artifact_key": key,
            "name": current.get("name", name),
            "version": current.get("version", version),
            "status": status,
            "business_codes": sorted(codes),
            "snapshot_ids": snapshot_ids,
            "batch_ids": batch_ids,
            "source_formats": sorted({claim["source_format"] for claim in claims if claim.get("source_format")}),
            "signature_statuses": signature_statuses,
            "published_at_ms": analysis["published_at_ms"],
            "error_bound_ms": analysis["error_bound_ms"],
            "time_conversion": analysis["time_conversion"],
            "digest_generation": analysis["current_generation"],
            "digest_generations": analysis["generations"],
            "algorithm_normalized": current.get("algorithm_normalized"),
            "algorithm_values": analysis["algorithms"],
            "digest_normalized": current.get("digest_normalized"),
            "digest_prefix": (current.get("digest_normalized") or "")[:12] or None,
            "tombstoned": analysis["tombstoned"],
            "revocations": stable_sort_records(analysis["revocations"]),
            "rejection_records": stable_sort_records(rejections),
            "conflicts": stable_sort_records(analysis["conflicts"]),
            "digest_comparison": analysis["digest_comparison"],
            "invalidations": invalidations,
            "closure_chains": [entry["chain"] for entry in invalidations],
            "direct_dependencies": self.graph.outgoing.get(key, []),
            "direct_dependents": self.graph.incoming.get(key, []),
            "dependency_cycles": relevant_cycles,
        }
        return artifact

    def _policy_versions(self) -> dict[str, Any]:
        return {
            "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "algorithm_whitelist_version": ALGORITHM_WHITELIST_VERSION,
            "merge_policy_version": MERGE_POLICY_VERSION,
        }

    def build_full_view(self) -> dict[str, Any]:
        keys = self._all_keys()
        analyses = {key: self._analysis_for(key) for key in keys}
        source_reasons = {
            key: analysis["source_reason"]
            for key, analysis in analyses.items()
            if analysis.get("source_reason")
        }
        invalidation_index, closure_sets = build_invalidation_index(source_reasons, self.graph)
        self.analyses = analyses
        self.source_reasons = source_reasons
        self.invalidation_index = invalidation_index
        self.closure_sets = closure_sets
        artifacts = {
            key: self._build_artifact(key, analyses[key], invalidation_index)
            for key in sorted(keys)
        }
        conflicts = stable_sort_records(
            [conflict for analysis in analyses.values() for conflict in analysis["conflicts"]]
        )
        rejections = stable_sort_records(self.rejections)
        status_counts: dict[str, int] = defaultdict(int)
        for artifact in artifacts.values():
            status_counts[artifact["status"]] += 1
        view = {
            "schema": "supply-chain-audit/integrity-view/v1",
            "policy_versions": self._policy_versions(),
            "clock_id": self.clock.get("clock_id"),
            "artifacts": artifacts,
            "conflicts": conflicts,
            "rejections": rejections,
            "fatal_errors": stable_sort_records(self.fatal_errors),
            "cycles": self.cycles,
            "closure_sets": closure_sets,
            "invalidation_index": invalidation_index,
            "stats": {
                "artifact_count": len(artifacts),
                "status_counts": dict(sorted(status_counts.items())),
                "conflict_count": len(conflicts),
                "rejection_count": len(rejections),
                "cycle_count": len(self.cycles),
                "closure_source_count": len(closure_sets),
                "invalidated_artifact_count": len(invalidation_index),
                "applied_batch_count": len(self.applied_batches),
                "watermark": self.watermark,
            },
        }
        return view

    def apply_parse_result(
        self,
        result: Any,
        batch_id: str | None = None,
        *,
        verify_equivalence: bool = True,
    ) -> dict[str, Any]:
        effective_batch = batch_id or getattr(result, "batch_id", None) or f"batch-{self.watermark + 1}"
        old_closure_sets = dict(self.closure_sets)
        changed_keys = self.ingest_parse_result(result, effective_batch)
        return self._refresh_incremental(effective_batch, changed_keys, old_closure_sets, verify_equivalence)

    def apply_revocation_batch(
        self,
        batch: dict[str, Any],
        *,
        verify_equivalence: bool = True,
    ) -> dict[str, Any]:
        effective_batch = batch.get("batch_id") or f"revocation-{self.watermark + 1}"
        old_closure_sets = dict(self.closure_sets)
        changed_keys = self.ingest_revocation_batch(batch)
        return self._refresh_incremental(effective_batch, changed_keys, old_closure_sets, verify_equivalence)

    def _refresh_incremental(
        self,
        batch_id: str,
        changed_keys: set[str],
        old_closure_sets: dict[str, list[str]],
        verify_equivalence: bool,
    ) -> dict[str, Any]:
        for key in changed_keys:
            self.analyses[key] = self._analysis_for(key)
            if self.analyses[key]["source_reason"]:
                self.source_reasons[key] = self.analyses[key]["source_reason"]
            else:
                self.source_reasons.pop(key, None)

        new_index, new_closure_sets = build_invalidation_index(self.source_reasons, self.graph)
        affected_keys = set(changed_keys)
        for key in changed_keys:
            affected_keys.update(old_closure_sets.get(key, []))
            affected_keys.update(new_closure_sets.get(key, []))

        # Re-analyze closure members too, because their incoming-edge context
        # can change even when their own claim is untouched.
        for key in list(affected_keys):
            if key not in changed_keys:
                self.analyses[key] = self._analysis_for(key)

        rejudged_edge_ids = set()
        rejudged_incoming_edges = []
        for key in affected_keys:
            for dependent in self.graph.incoming.get(key, []):
                edge_id = f"{dependent}->{key}"
                if edge_id not in rejudged_edge_ids:
                    rejudged_edge_ids.add(edge_id)
                    rejudged_incoming_edges.append(edge_id)
            for dependency in self.graph.outgoing.get(key, []):
                edge_id = f"{key}->{dependency}"
                if edge_id not in rejudged_edge_ids:
                    rejudged_edge_ids.add(edge_id)

        if not hasattr(self, "view") or not self.view:
            self.view = self.build_full_view()
        artifacts = dict(self.view.get("artifacts", {}))
        for key in affected_keys:
            analysis = self.analyses.get(key) or self._analysis_for(key)
            self.analyses[key] = analysis
            artifacts[key] = self._build_artifact(key, analysis, new_index)

        conflicts = stable_sort_records(
            [conflict for analysis in self.analyses.values() for conflict in analysis.get("conflicts", [])]
        )
        status_counts: dict[str, int] = defaultdict(int)
        for artifact in artifacts.values():
            status_counts[artifact["status"]] += 1

        self.invalidation_index = new_index
        self.closure_sets = new_closure_sets
        self.watermark += 1
        batch_record = {"batch_id": batch_id, "watermark": self.watermark}
        self.applied_batches.append(batch_record)

        self.view = {
            "schema": "supply-chain-audit/integrity-view/v1",
            "policy_versions": self._policy_versions(),
            "clock_id": self.clock.get("clock_id"),
            "artifacts": artifacts,
            "conflicts": conflicts,
            "rejections": stable_sort_records(self.rejections),
            "fatal_errors": stable_sort_records(self.fatal_errors),
            "cycles": self.cycles,
            "closure_sets": self.closure_sets,
            "invalidation_index": self.invalidation_index,
            "stats": {
                "artifact_count": len(artifacts),
                "status_counts": dict(sorted(status_counts.items())),
                "conflict_count": len(conflicts),
                "rejection_count": len(stable_sort_records(self.rejections)),
                "cycle_count": len(self.cycles),
                "closure_source_count": len(self.closure_sets),
                "invalidated_artifact_count": len(self.invalidation_index),
                "applied_batch_count": len(self.applied_batches),
                "watermark": self.watermark,
            },
        }

        outcome = {
            "batch_id": batch_id,
            "watermark": self.watermark,
            "changed_keys": sorted(changed_keys),
            "rejudged_keys": sorted(affected_keys),
            "rejudged_key_count": len(affected_keys),
            "rejudged_edges": sorted(rejudged_edge_ids),
            "rejudged_incoming_edge_count": len(rejudged_incoming_edges),
            "status": "applied",
        }
        if verify_equivalence:
            full_view = self.build_full_view()
            divergence = compare_views(self.view, full_view)
            if divergence:
                outcome["status"] = E_RECOMPUTE_DIVERGE
                outcome["divergence"] = divergence
                self.last_divergence = divergence
            else:
                outcome["equivalent_to_full_recompute"] = True
        return outcome


def compare_views(incremental: dict[str, Any], full: dict[str, Any]) -> dict[str, Any] | None:
    """Compare integrity artifacts, conflict entries and closure sets field by field."""

    inc_artifacts = incremental.get("artifacts", {})
    full_artifacts = full.get("artifacts", {})
    for key in sorted(set(inc_artifacts) | set(full_artifacts)):
        if key not in inc_artifacts:
            return _divergence(key, "__artifact__", None, full_artifacts[key], "artifact missing from incremental view")
        if key not in full_artifacts:
            return _divergence(key, "__artifact__", inc_artifacts[key], None, "unexpected incremental artifact")
        left = inc_artifacts[key]
        right = full_artifacts[key]
        for field_name in sorted(set(left) | set(right)):
            if left.get(field_name) != right.get(field_name):
                return _divergence(
                    key,
                    field_name,
                    left.get(field_name),
                    right.get(field_name),
                    "artifact field differs",
                )

    for section, key_field in (
        ("invalidation_index", "artifact_key"),
        ("closure_sets", "source_key"),
    ):
        left_section = incremental.get(section, {})
        right_section = full.get(section, {})
        for key in sorted(set(left_section) | set(right_section)):
            if left_section.get(key) != right_section.get(key):
                field_name = "entries" if section == "invalidation_index" else "closure_set"
                return _divergence(
                    key if isinstance(key, str) else "<unknown>",
                    field_name,
                    left_section.get(key),
                    right_section.get(key),
                    f"{section} differs",
                )

    for section in ("conflicts", "cycles"):
        left_records = incremental.get(section, [])
        right_records = full.get(section, [])
        if left_records != right_records:
            left_by_key = {str(item.get("artifact_key")): item for item in left_records if isinstance(item, dict)}
            right_by_key = {str(item.get("artifact_key")): item for item in right_records if isinstance(item, dict)}
            for key in sorted(set(left_by_key) | set(right_by_key)):
                left_item = left_by_key.get(key, {})
                right_item = right_by_key.get(key, {})
                for field_name in sorted(set(left_item) | set(right_item)):
                    if left_item.get(field_name) != right_item.get(field_name):
                        return _divergence(
                            key,
                            field_name,
                            left_item.get(field_name),
                            right_item.get(field_name),
                            f"{section} field differs",
                        )
            return _divergence("<unknown>", section, left_records[:1], right_records[:1], f"{section} differs")

    return None


def _divergence(key: str, field_name: str, incremental_value: Any, full_value: Any, message: str) -> dict[str, Any]:
    return {
        "code": E_RECOMPUTE_DIVERGE,
        "artifact_key": key,
        "field_name": field_name,
        "message": message,
        "incremental_value": incremental_value,
        "full_value": full_value,
    }
