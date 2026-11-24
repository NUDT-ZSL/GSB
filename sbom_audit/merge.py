"""Order-independent canonical merge, hash adjudication and report assembly."""

from __future__ import annotations

from typing import Any, Iterable

from .codes import (
    E_DEP_CYCLE,
    E_HASH_MISMATCH,
    E_HASH_NOT_COMPARABLE,
    E_MERGE_CONFLICT,
    E_MISSING_COMPONENT,
    E_REVOKED,
    E_TOMBSTONE,
    E_VERSION_CONSTRAINT,
)
from .graph import DependencyGraph
from .hashing import constant_time_equal, derive_hmac_over_digest, validate_digest
from .model import ComponentRecord, digest_object


def _source_basis(record: ComponentRecord) -> dict[str, Any]:
    return {
        "generation": record.generation,
        "snapshot_kind": record.snapshot_kind,
        "snapshot_id": record.snapshot_id,
        "record_index": record.record_index,
        "published_at_ms": record.published_at_ms,
        "version": record.version,
        "digest_algorithm": record.algorithm,
        "digest": record.digest,
        "signature_status": record.signature_status,
        "tombstone": record.tombstone,
        "revoked": record.revoked,
        "source_ref": record.source_ref,
    }


def _sort_key(record: ComponentRecord) -> tuple[int, int, str, int, str]:
    return (
        record.generation,
        record.published_at_ms if record.published_at_ms is not None else -1,
        record.snapshot_id,
        record.record_index,
        record.source_ref,
    )


def _evaluate_hash(key: str, generations: dict[int, ComponentRecord], sync_markers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    available = [r for r in generations.values() if r.digest and r.algorithm]
    if not available:
        return {"hash_status": "not_applicable", "hash_code": None, "comparable": False, "digest_changed": False, "sync_marker": None}
    if len(available) == 1:
        only = available[0]
        return {
            "hash_status": "single_source",
            "hash_code": None,
            "comparable": True,
            "digest_changed": False,
            "algorithm": only.algorithm,
            "digest": only.digest,
            "digest_bits": validate_digest(only.algorithm, only.digest).bits,
            "sync_marker": None,
        }

    oldest = min(available, key=lambda r: (r.generation, r.published_at_ms or -1))
    newest = max(available, key=lambda r: (r.generation, r.published_at_ms or -1))
    old_spec = validate_digest(oldest.algorithm, oldest.digest)
    new_spec = validate_digest(newest.algorithm, newest.digest)
    marker_decl = sync_markers.get(key) or {}
    marker = marker_decl.get("marker") if isinstance(marker_decl, dict) else None

    if oldest.algorithm == newest.algorithm:
        equal = constant_time_equal(oldest.digest or "", newest.digest or "")
        return {
            "hash_status": "match" if equal else "mismatch",
            "hash_code": None if equal else E_HASH_MISMATCH,
            "comparable": True,
            "digest_changed": not equal,
            "algorithm": newest.algorithm,
            "digest": newest.digest,
            "digest_bits": new_spec.bits,
            "sync_marker": marker,
        }

    if old_spec.bits != new_spec.bits or marker_decl.get("bits") not in (None, old_spec.bits):
        return {
            "hash_status": "algorithm_width_mismatch",
            "hash_code": "E_HASH_ALGO",
            "comparable": False,
            "digest_changed": True,
            "algorithms": [oldest.algorithm, newest.algorithm],
            "digest_bits": [old_spec.bits, new_spec.bits],
            "sync_marker": marker,
        }

    if marker in {"raw-equal", "normalized-equal", "synced", "direct"}:
        equal = constant_time_equal(oldest.digest or "", newest.digest or "")
        return {
            "hash_status": "synchronized_match" if equal else "mismatch",
            "hash_code": None if equal else E_HASH_MISMATCH,
            "comparable": True,
            "digest_changed": not equal,
            "normalization": "whitelist-bit-width+hex",
            "algorithms": [oldest.algorithm, newest.algorithm],
            "digest": newest.digest,
            "digest_bits": new_spec.bits,
            "sync_marker": marker,
        }

    if marker in {"hmac-over-digest", "hmac_sha256_over_digest"}:
        key_hex = marker_decl.get("key_hex")
        if not key_hex:
            return {"hash_status": "invalid_sync_declaration", "hash_code": E_HASH_NOT_COMPARABLE, "comparable": False, "digest_changed": None, "sync_marker": marker}
        secret = bytes.fromhex(str(key_hex))
        sha_record = oldest if oldest.algorithm == "sha256" else newest if newest.algorithm == "sha256" else None
        hmac_record = newest if newest.algorithm == "hmac-sha256" else oldest if oldest.algorithm == "hmac-sha256" else None
        if sha_record is None or hmac_record is None:
            expected = derive_hmac_over_digest(oldest.digest or "", secret)
            target = newest.digest or ""
        else:
            expected = derive_hmac_over_digest(sha_record.digest or "", secret)
            target = hmac_record.digest or ""
        equal = constant_time_equal(expected, target)
        return {
            "hash_status": "synchronized_derived_match" if equal else "mismatch",
            "hash_code": None if equal else E_HASH_MISMATCH,
            "comparable": True,
            "digest_changed": not equal,
            "normalization": "hmac_sha256(secret, bytes.fromhex(sha256_digest))",
            "algorithms": [oldest.algorithm, newest.algorithm],
            "expected_derived_digest": expected,
            "digest": newest.digest,
            "digest_bits": new_spec.bits,
            "sync_marker": marker,
        }

    return {
        "hash_status": "algorithm_diverged_not_comparable",
        "hash_code": E_HASH_NOT_COMPARABLE,
        "comparable": False,
        "digest_changed": None,
        "algorithms": [oldest.algorithm, newest.algorithm],
        "digest_bits": [old_spec.bits, new_spec.bits],
        "sync_marker": marker,
    }


def merge_views(records: Iterable[ComponentRecord | dict[str, Any]], sync_markers: dict[str, dict[str, Any]] | None = None):
    sync_markers = sync_markers or {}
    materialized = [r if isinstance(r, ComponentRecord) else ComponentRecord(**r) for r in records]
    by_key: dict[str, list[ComponentRecord]] = {}
    for record in materialized:
        by_key.setdefault(record.canonical_key, []).append(record)

    views: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    hash_by_key: dict[str, dict[str, Any]] = {}

    for key in sorted(by_key):
        candidates = sorted(by_key[key], key=_sort_key)
        authority = candidates[-1]
        by_generation: dict[int, ComponentRecord] = {}
        for record in candidates:
            current = by_generation.get(record.generation)
            if current is None or _sort_key(record) > _sort_key(current):
                by_generation[record.generation] = record

        hash_result = _evaluate_hash(key, by_generation, sync_markers)
        hash_by_key[key] = hash_result
        revoked = any(r.revoked for r in candidates) or authority.revoked
        tombstoned = authority.tombstone
        seed_reasons: list[str] = []
        codes: list[str] = []
        if tombstoned:
            seed_reasons.append("tombstoned")
            codes.append(E_TOMBSTONE)
        if revoked:
            seed_reasons.append("revoked")
            codes.append(E_REVOKED)
        if hash_result.get("digest_changed") is True:
            seed_reasons.append("digest_changed")
        if hash_result.get("hash_code") == E_HASH_MISMATCH:
            seed_reasons.append("digest_mismatch")
            codes.append(E_HASH_MISMATCH)
        if hash_result.get("hash_code") == "E_HASH_ALGO":
            codes.append("E_HASH_ALGO")
        elif hash_result.get("hash_code") == E_HASH_NOT_COMPARABLE:
            codes.append(E_HASH_NOT_COMPARABLE)

        views.append({
            "canonical_key": key,
            "component_id": authority.component_id,
            "version": authority.version,
            "digest_algorithm": authority.algorithm,
            "digest": authority.digest,
            "signature_status": authority.signature_status,
            "published_at_ms": authority.published_at_ms,
            "authoritative_generation": authority.generation,
            "authoritative_snapshot_id": authority.snapshot_id,
            "authority_reason": "generation+published_at_ms tie-break",
            "tombstoned": tombstoned,
            "revoked": revoked,
            "basis_generations": sorted(by_generation),
            "source_basis": [_source_basis(by_generation[gen]) for gen in sorted(by_generation)],
            "hash": hash_result,
            "seed_reasons": seed_reasons,
            "codes": sorted(set(codes)),
            "conclusion": "invalid" if seed_reasons else "pending_closure",
        })

        generations = list(by_generation.values())
        if len(generations) >= 2:
            first = min(generations, key=lambda r: r.generation)
            second = max(generations, key=lambda r: r.generation)
            for field_name, left, right in (
                ("version", first.version, second.version),
                ("digest_algorithm", first.algorithm, second.algorithm),
            ):
                if left is not None and right is not None and left != right:
                    conflicts.append({
                        "canonical_key": key,
                        "field": field_name,
                        "code": E_MERGE_CONFLICT,
                        "generations": [first.generation, second.generation],
                        "values": {str(first.generation): left, str(second.generation): right},
                        "sources": [_source_basis(first), _source_basis(second)],
                        "resolution": f"authoritative_generation_{second.generation}",
                    })
            if hash_result.get("hash_code") == E_HASH_MISMATCH:
                conflicts.append({
                    "canonical_key": key,
                    "field": "digest",
                    "code": E_MERGE_CONFLICT,
                    "business_code": E_HASH_MISMATCH,
                    "generations": [first.generation, second.generation],
                    "values": {str(first.generation): first.digest, str(second.generation): second.digest},
                    "sources": [_source_basis(first), _source_basis(second)],
                    "resolution": "marked_changed_and_invalidated",
                })
            elif hash_result.get("hash_code") == E_HASH_NOT_COMPARABLE:
                conflicts.append({
                    "canonical_key": key,
                    "field": "digest_algorithm",
                    "code": E_MERGE_CONFLICT,
                    "business_code": E_HASH_NOT_COMPARABLE,
                    "generations": [first.generation, second.generation],
                    "sources": [_source_basis(first), _source_basis(second)],
                    "resolution": "not_treated_as_tampering_without_sync_marker",
                })

    conflicts.sort(key=lambda c: (c["canonical_key"], c["field"], str(c.get("business_code", ""))))
    return views, conflicts, hash_by_key


def build_integrity_report(
    records: Iterable[ComponentRecord | dict[str, Any]],
    edges: Iterable[dict[str, Any]] | DependencyGraph = (),
    *,
    rejections: Iterable[dict[str, Any]] = (),
    sync_markers: dict[str, dict[str, Any]] | None = None,
    batch_reports: Iterable[dict[str, Any]] | None = None,
    policy_versions: dict[str, str | int] | None = None,
) -> dict[str, Any]:
    record_list = list(records)
    graph = edges if isinstance(edges, DependencyGraph) else DependencyGraph(edges)
    views, conflicts, _ = merge_views(record_list, sync_markers)
    return assemble_integrity_report(
        views,
        conflicts,
        graph,
        records=record_list,
        rejections=rejections,
        batch_reports=batch_reports,
        policy_versions=policy_versions,
    )


def assemble_integrity_report(
    views: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    graph: DependencyGraph,
    *,
    records: Iterable[ComponentRecord | dict[str, Any]] | None = None,
    rejections: Iterable[dict[str, Any]] = (),
    batch_reports: Iterable[dict[str, Any]] | None = None,
    policy_versions: dict[str, str | int] | None = None,
    incremental: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record_list = list(records or [])
    view_by_key = {view["canonical_key"]: view for view in views}

    for node in sorted(graph.nodes):
        if node not in view_by_key:
            missing_view = {
                "canonical_key": node,
                "component_id": node.rsplit("/", 1)[-1],
                "version": None,
                "digest_algorithm": None,
                "digest": None,
                "signature_status": "unknown",
                "published_at_ms": None,
                "authoritative_generation": None,
                "authoritative_snapshot_id": None,
                "tombstoned": False,
                "revoked": False,
                "basis_generations": [],
                "source_basis": [],
                "hash": {"hash_status": "missing", "hash_code": E_MISSING_COMPONENT},
                "seed_reasons": ["missing_from_integrity_manifest"],
                "codes": [E_MISSING_COMPONENT],
                "conclusion": "invalid",
            }
            views.append(missing_view)
            view_by_key[node] = missing_view

    cycles = graph.find_cycles()
    cycle_errors = [{"code": E_DEP_CYCLE, "cycle": cycle, "canonical_keys": sorted(set(cycle[:-1]))} for cycle in cycles]
    versions = {key: view.get("version") for key, view in view_by_key.items()}
    version_violations = graph.version_violations(versions)

    record_keys = {record.canonical_key if isinstance(record, ComponentRecord) else record["canonical_key"] for record in record_list}
    seeds = sorted(key for key, view in view_by_key.items() if view.get("seed_reasons") and key in record_keys)
    closure, paths = graph.reverse_closure(seeds)

    closure_details: dict[str, Any] = {}
    for key in sorted(closure):
        view = view_by_key.get(key, {})
        reasons = view.get("seed_reasons", []) if key in seeds else ["upstream_integrity_failure"]
        closure_details[key] = {
            "canonical_key": key,
            "reasons": reasons,
            "codes": view.get("codes", []),
            "reverse_dependency_paths": paths.get(key, []),
            "direct_incoming_edges": [
                {"from": e["from"], "to": e["to"], "constraint": e.get("constraint", "*")}
                for e in graph.reverse.get(key, [])
            ],
        }

    for view in views:
        key = view["canonical_key"]
        view["conclusion"] = "invalid" if key in seeds else "affected" if key in closure else "valid"
        view["in_closure"] = key in closure

    rejection_list = list(rejections)
    batch_report_list = list(batch_reports or [])
    failed_batches = [b for b in batch_report_list if b.get("status") in {"error", "rejected", "partial"}]
    code_set: set[str] = set()
    for view in views:
        code_set.update(code for code in view.get("codes", []) if code)
    for conflict in conflicts:
        if conflict.get("business_code"):
            code_set.add(conflict["business_code"])
    if conflicts:
        code_set.add(E_MERGE_CONFLICT)
    if cycles:
        code_set.add(E_DEP_CYCLE)
    if version_violations:
        code_set.add(E_VERSION_CONSTRAINT)
    for rejection in rejection_list:
        if rejection.get("code"):
            code_set.add(rejection["code"])
    for batch in failed_batches:
        if batch.get("code"):
            code_set.add(batch["code"])
    global_codes = sorted(code_set)

    report = {
        "schema": "sbom-audit.report.v1",
        "policy_versions": policy_versions or {},
        "status": "partial_failure" if rejection_list or failed_batches or cycles else "ok",
        "global_codes": global_codes,
        "summary": {
            "record_count": len(record_list),
            "component_count": len(views),
            "conflict_count": len(conflicts),
            "rejection_count": len(rejection_list),
            "edge_count": len(graph.edges),
            "cycle_count": len(cycles),
            "seed_count": len(seeds),
            "closure_size": len(closure),
            "version_violation_count": len(version_violations),
            "failed_batch_count": len(failed_batches),
        },
        "integrity_view": sorted(views, key=lambda v: v["canonical_key"]),
        "conflicts": conflicts,
        "rejections": rejection_list,
        "dependency": {
            "edges": graph.canonical_edges(),
            "cycles": cycle_errors,
            "version_violations": sorted(version_violations, key=lambda v: (v["from"], v["to"], v.get("constraint", ""))),
        },
        "seeds": seeds,
        "closure": sorted(closure),
        "invalid_closure": sorted(closure),
        "reverse_dependents": sorted(closure - set(seeds)),
        "closure_details": {key: closure_details[key] for key in sorted(closure_details)},
        "batch_reports": batch_report_list,
    }
    if incremental is not None:
        report["incremental"] = incremental
    report["view_digest"] = canonical_report_digest(report)
    return report


def report_without_digest(report: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in report.items() if key != "view_digest"}


def canonical_report_digest(report: dict[str, Any]) -> str:
    return digest_object(report_without_digest(report))
