"""Policy build and atomic activation orchestration."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Callable

from .closures import build_closures, closure_semantic_rows
from .errors import BusinessError
from .model import ClosureRecord, PolicyCandidate, V1Policy, V2PolicyPackage
from .policy import (
    candidate_json,
    dependency_digest,
    dependency_digest_set,
    effective_v2_dependency_digest,
    load_v1_policy,
    load_v2_package,
    merge_v1_v2,
    overlay_incremental,
    persist_merge_decisions,
    source_summary,
)
from .storage import (
    active_policy_row,
    active_policy_version_id,
    clock_watermark,
    closure_rows,
    connect,
    initialize_runtime,
    init_schema,
    policy_rows,
    semantic_resource_rows,
    transaction,
)
from .util import (
    canonical_json,
    compare_semver,
    now_ms,
    read_json,
    sha256_file,
    sha256_json,
    workspace_lock,
)
from .validation import load_validated_samples


class _CrashInjector:
    def __init__(self, requested: str | None) -> None:
        self.requested = requested

    def hit(self, point: str) -> None:
        if self.requested == point:
            os._exit(78)


def input_digest_for_samples(workdir: Path) -> str:
    samples = workdir / "samples"
    return sha256_json(
        {
            "templates": sha256_file(samples / "templates.json"),
            "requests": sha256_file(samples / "requests.json"),
            "topology": sha256_file(samples / "topology.json"),
            "v1": sha256_file(samples / "policies" / "v1.json"),
            "v2_bundle": load_v2_package(
                samples / "policies" / "v2"
            ).bundle_digest,
        }
    )


def _policy_bundle_digest(
    workdir: Path,
    v1: V1Policy,
    v2: V2PolicyPackage,
    build_clock_ms: int,
) -> str:
    samples = workdir / "samples"
    return sha256_json(
        {
            "v1_file_sha256": sha256_file(samples / "policies" / "v1.json"),
            "v2_bundle_digest": v2.bundle_digest,
            "v1_manifest_version": v1.manifest_version,
            "v2_manifest_version": v2.manifest_version,
            "injected_clock_ms": build_clock_ms,
        }
    )


def _version_payload(
    connection: sqlite3.Connection,
    policy_version_id: int,
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM policy_versions WHERE policy_version_id=?",
        (policy_version_id,),
    ).fetchone()
    if row is None:
        return {}
    return {
        "policy_version_id": row["policy_version_id"],
        "policy_key": row["policy_key"],
        "manifest_version": row["manifest_version"],
        "policy_generation": row["policy_generation"],
        "logical_clock": row["logical_clock"],
        "source_bundle_digest": row["source_bundle_digest"],
        "dependency_digest": row["dependency_digest"],
        "mode": row["mode"],
        "parent_version_id": row["parent_version_id"],
        "state": row["state"],
        "source_summary": read_json_text(row["source_summary_json"]),
    }


def read_json_text(value: str) -> Any:
    import json

    return json.loads(value)


def candidates_from_rows(
    rows: dict[str, dict[str, Any]],
) -> dict[str, PolicyCandidate]:
    return {
        name: PolicyCandidate(
            template_name=row["template_name"],
            version=row["version"],
            dependencies=tuple(row["dependencies"]),
            labels=tuple(row["labels"]),
            logical_clock=row["logical_clock"],
            policy_generation=row["policy_generation"],
            source_format=row["source_format"],
            source_version=row["source_version"],
            original_time=row["original_time"],
            conversion=row["conversion"],
            effective_at_ms=row["effective_at_ms"],
            decision_basis=row["decision_basis"],
            decision_detail=row["decision_detail"],
            tombstone=row["tombstone"],
            tombstone_reason=row["tombstone_reason"],
        )
        for name, row in rows.items()
    }


def load_active_view(
    connection: sqlite3.Connection,
) -> tuple[int, dict[str, PolicyCandidate], dict[str, ClosureRecord]]:
    version_id = active_policy_version_id(connection)
    if version_id is None:
        raise BusinessError(
            "POLICY_NOT_BUILT",
            "No active policy version exists; run build first",
        )
    candidates = candidates_from_rows(policy_rows(connection, version_id))
    raw_closures = closure_rows(connection, version_id)
    closures = {
        name: ClosureRecord(
            template_name=row["template_name"],
            closure=tuple(row["closure"]),
            closure_digest=row["closure_digest"],
            config_fingerprint=row["config_fingerprint"],
            dependency_digest=row["dependency_digest"],
            revision=row["revision"],
            policy_version_row=version_id,
        )
        for name, row in raw_closures.items()
    }
    return version_id, candidates, closures


def _record_apply_job(
    connection: sqlite3.Connection,
    *,
    package_path: Path,
    package: V2PolicyPackage,
    status: str,
    error: str | None = None,
) -> dict[str, Any]:
    job_id = sha256_json(
        {
            "package_path": str(package_path.resolve()),
            "bundle_digest": package.bundle_digest,
        }
    )
    with transaction(connection):
        existing = connection.execute(
            "SELECT * FROM policy_apply_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO policy_apply_jobs(
                    job_id, package_path, bundle_digest, policy_generation,
                    manifest_version, status, created_at_ms, updated_at_ms,
                    last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    str(package_path.resolve()),
                    package.bundle_digest,
                    package.policy_generation,
                    package.manifest_version,
                    status,
                    now_ms(),
                    now_ms(),
                    error,
                ),
            )
        else:
            connection.execute(
                """
                UPDATE policy_apply_jobs
                SET status=?, updated_at_ms=?, last_error=?,
                    package_path=?
                WHERE job_id=?
                """,
                (
                    status,
                    now_ms(),
                    error,
                    str(package_path.resolve()),
                    job_id,
                ),
            )
    row = connection.execute(
        "SELECT * FROM policy_apply_jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    return dict(row)


def _insert_policy_version(
    connection: sqlite3.Connection,
    *,
    policy_key: str,
    manifest_version: str,
    policy_generation: int,
    logical_clock: int,
    source_bundle_digest: str,
    candidates: dict[str, PolicyCandidate],
    closures: dict[str, ClosureRecord],
    mode: str,
    parent_version_id: int | None,
    source_info: dict[str, Any],
    topology_invalidations: list[Any],
    invalidation_events: list[Any],
    merge_clock_ms: int,
    crash: _CrashInjector,
    job_id: str | None,
    job_status: str,
) -> int:
    existing = connection.execute(
        """
        SELECT policy_version_id, dependency_digest, state
        FROM policy_versions
        WHERE policy_key=? AND policy_generation=? AND source_bundle_digest=?
        """,
        (policy_key, policy_generation, source_bundle_digest),
    ).fetchone()
    if existing is not None:
        if existing["state"] == "committed":
            if job_id:
                connection.execute(
                    """
                    UPDATE policy_apply_jobs
                    SET status='noop', updated_at_ms=?
                    WHERE job_id=?
                    """,
                    (now_ms(), job_id),
                )
            return int(existing["policy_version_id"])
        connection.execute(
            "DELETE FROM policy_versions WHERE policy_version_id=?",
            (existing["policy_version_id"],),
        )
    digest = dependency_digest(candidates.values())
    digest_set = dependency_digest_set(candidates.values())
    cursor = connection.execute(
        """
        INSERT INTO policy_versions(
            policy_key, manifest_version, policy_generation, logical_clock,
            source_bundle_digest, dependency_digest,
            dependency_digest_set_json, mode, parent_version_id, state,
            source_summary_json, created_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'staging', ?, ?)
        """,
        (
            policy_key,
            manifest_version,
            policy_generation,
            logical_clock,
            source_bundle_digest,
            digest,
            canonical_json(digest_set),
            mode,
            parent_version_id,
            canonical_json(source_info),
            now_ms(),
        ),
    )
    version_id = int(cursor.lastrowid)
    for name in sorted(candidates):
        candidate = candidates[name]
        connection.execute(
            """
            INSERT INTO policy_nodes(
                policy_version_id, template_name, version,
                dependencies_json, labels_json, logical_clock,
                policy_generation, source_format, source_version,
                original_time_json, conversion_json, effective_at_ms,
                decision_basis, decision_detail, tombstone, tombstone_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                candidate.template_name,
                candidate.version,
                canonical_json(list(candidate.dependencies)),
                canonical_json(list(candidate.labels)),
                candidate.logical_clock,
                candidate.policy_generation,
                candidate.source_format,
                candidate.source_version,
                canonical_json(candidate.original_time),
                canonical_json(candidate.conversion),
                candidate.effective_at_ms,
                candidate.decision_basis,
                candidate.decision_detail,
                1 if candidate.tombstone else 0,
                candidate.tombstone_reason,
            ),
        )
    for name in sorted(candidates):
        candidate = candidates[name]
        for dependency in candidate.dependencies:
            connection.execute(
                """
                INSERT INTO policy_edges(
                    policy_version_id, from_template, to_template,
                    source_format, source_version, logical_clock,
                    policy_generation, original_time_json, conversion_json,
                    effective_at_ms, decision_basis, decision_detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    name,
                    dependency,
                    candidate.source_format,
                    candidate.source_version,
                    candidate.logical_clock,
                    candidate.policy_generation,
                    canonical_json(candidate.original_time),
                    canonical_json(candidate.conversion),
                    candidate.effective_at_ms,
                    candidate.decision_basis,
                    candidate.decision_detail,
                ),
            )
    for name in sorted(closures):
        record = closures[name]
        connection.execute(
            """
            INSERT INTO template_closures(
                policy_version_id, template_name, closure_json,
                closure_digest, config_fingerprint, dependency_digest, revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                name,
                canonical_json(list(record.closure)),
                record.closure_digest,
                record.config_fingerprint,
                record.dependency_digest,
                record.revision,
            ),
        )
    for candidate in candidates.values():
        if candidate.tombstone:
            connection.execute(
                """
                INSERT INTO policy_tombstones(
                    policy_version_id, template_name, version,
                    logical_clock, reason
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    candidate.template_name,
                    candidate.version,
                    candidate.logical_clock,
                    candidate.tombstone_reason or "policy-tombstone",
                ),
            )
    for invalidation in topology_invalidations:
        connection.execute(
            """
            INSERT INTO topology_invalidations(
                policy_version_id, scope, logical_clock, reason
            ) VALUES (?, ?, ?, ?)
            """,
            (
                version_id,
                invalidation.scope,
                invalidation.logical_clock,
                invalidation.reason,
            ),
        )
    for event in invalidation_events:
        payload = event.as_dict()
        trigger = payload["trigger"]
        connection.execute(
            """
            INSERT INTO invalidation_events(
                policy_version_id, trigger_template, trigger_version,
                trigger_generation, trigger_clock, reason, changed_json,
                affected_json, chains_json, before_json, after_json,
                created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                trigger["template_name"],
                trigger["version"],
                trigger["policy_generation"],
                trigger["logical_clock"],
                event.reason,
                canonical_json(payload["changed_templates"]),
                canonical_json(payload["affected_downstream"]),
                canonical_json(payload["chains"]),
                canonical_json(payload["versions_before"]),
                canonical_json(payload["versions_after"]),
                now_ms(),
            ),
        )
    persist_merge_decisions(
        connection,
        policy_key=policy_key,
        injected_clock_ms=merge_clock_ms,
        candidates=candidates,
    )
    crash.hit("policy-stage")
    connection.execute(
        """
        UPDATE policy_versions SET state='committed'
        WHERE policy_version_id=?
        """,
        (version_id,),
    )
    connection.execute(
        """
        INSERT INTO policy_pointer(
            singleton, policy_version_id, updated_at_ms
        ) VALUES (1, ?, ?)
        ON CONFLICT(singleton) DO UPDATE
        SET policy_version_id=excluded.policy_version_id,
            updated_at_ms=excluded.updated_at_ms
        """,
        (version_id, now_ms()),
    )
    crash.hit("policy-pointer")
    if job_id:
        connection.execute(
            """
            UPDATE policy_apply_jobs
            SET status=?, updated_at_ms=?, last_error=NULL
            WHERE job_id=?
            """,
            (job_status, now_ms(), job_id),
        )
    return version_id


def _build_result(
    connection: sqlite3.Connection,
    *,
    mode: str,
    reason: str,
    v1: V1Policy,
    v2: V2PolicyPackage | None,
    candidates: dict[str, PolicyCandidate],
    closures: dict[str, ClosureRecord],
    invalidations: list[Any],
) -> dict[str, Any]:
    version_id = active_policy_version_id(connection)
    version = _version_payload(connection, version_id) if version_id else {}
    decisions = [
        {
            "template_name": candidate.template_name,
            "selected_source": candidate.source_format,
            "source_version": candidate.source_version,
            "version": candidate.version,
            "logical_clock": candidate.logical_clock,
            "policy_generation": candidate.policy_generation,
            "effective_at_ms": candidate.effective_at_ms,
            "decision_basis": candidate.decision_basis,
            "decision_detail": candidate.decision_detail,
            "v1_original_time": (
                candidate.original_time
                if candidate.source_format == "v1"
                else None
            ),
            "v1_conversion": (
                candidate.conversion if candidate.source_format == "v1" else None
            ),
        }
        for candidate in sorted(
            candidates.values(),
            key=lambda item: item.template_name,
        )
    ]
    return {
        "ok": True,
        "command": "build",
        "mode": mode,
        "reason": reason,
        "policy": version,
        "source_summary": source_summary(v1, v2) if v2 is not None else {},
        "template_count": len(candidates),
        "edge_count": sum(len(item.dependencies) for item in candidates.values()),
        "closure_count": len(closures),
        "merge_decisions": decisions,
        "invalidations": [event.as_dict() for event in invalidations],
        "closures": closure_semantic_rows(closures),
        "resources": semantic_resource_rows(connection),
    }


def _build_workspace_locked(
    workdir: Path,
    *,
    clock_ms: int | None = None,
    policy_package: Path | None = None,
    crash_at: str | None = None,
) -> dict[str, Any]:
    manifest_version, injected_clock, templates, _, topology = load_validated_samples(
        workdir
    )
    input_digest = input_digest_for_samples(workdir)
    connection = connect(workdir)
    crash = _CrashInjector(crash_at)
    try:
        init_schema(connection)
        current_clock = clock_watermark(connection)
        build_clock = (
            clock_ms
            if clock_ms is not None
            else (current_clock if current_clock > 0 else injected_clock)
        )
        runtime_init = initialize_runtime(
            connection,
            manifest_version=manifest_version,
            templates=templates,
            topology=topology,
            injected_clock_ms=build_clock,
            input_digest=input_digest,
            allow_clock_advance=clock_ms is None,
        )
        build_clock = int(runtime_init["injected_clock_ms"])
        samples = workdir / "samples"
        v1 = load_v1_policy(samples / "policies" / "v1.json")
        v2_base = load_v2_package(samples / "policies" / "v2")
        base_candidates = merge_v1_v2(
            v1,
            v2_base,
            injected_clock_ms=build_clock,
        )
        active = active_policy_row(connection)
        if policy_package is None:
            base_bundle = _policy_bundle_digest(
                workdir,
                v1,
                v2_base,
                build_clock,
            )
            if active is not None:
                if active["policy_generation"] > v2_base.policy_generation:
                    _, candidates, closures = load_active_view(connection)
                    return _build_result(
                        connection,
                        mode="noop",
                        reason="active policy is newer than base sample snapshot",
                        v1=v1,
                        v2=v2_base,
                        candidates=candidates,
                        closures=closures,
                        invalidations=[],
                    )
                if active["source_bundle_digest"] == base_bundle:
                    _, candidates, closures = load_active_view(connection)
                    return _build_result(
                        connection,
                        mode="noop",
                        reason="identical base snapshot and logical clock",
                        v1=v1,
                        v2=v2_base,
                        candidates=candidates,
                        closures=closures,
                        invalidations=[],
                    )
            previous_nodes: dict[str, PolicyCandidate] | None = None
            previous_closures: dict[str, ClosureRecord] | None = None
            parent_version_id: int | None = None
            if active is not None:
                parent_version_id = int(active["policy_version_id"])
                previous_nodes = candidates_from_rows(
                    policy_rows(connection, parent_version_id)
                )
                raw_previous = closure_rows(connection, parent_version_id)
                previous_closures = {
                    name: ClosureRecord(
                        template_name=row["template_name"],
                        closure=tuple(row["closure"]),
                        closure_digest=row["closure_digest"],
                        config_fingerprint=row["config_fingerprint"],
                        dependency_digest=row["dependency_digest"],
                        revision=row["revision"],
                    )
                    for name, row in raw_previous.items()
                }
            closures, invalidations = build_closures(
                base_candidates,
                previous_nodes=previous_nodes,
                previous_closures=previous_closures,
            )
            with transaction(connection):
                version_id = _insert_policy_version(
                    connection,
                    policy_key=v1.policy_key,
                    manifest_version=v2_base.manifest_version,
                    policy_generation=v2_base.policy_generation,
                    logical_clock=build_clock,
                    source_bundle_digest=base_bundle,
                    candidates=base_candidates,
                    closures=closures,
                    mode="full" if active is None else "rebase",
                    parent_version_id=parent_version_id,
                    source_info=source_summary(v1, v2_base),
                    topology_invalidations=v2_base.topology_invalidations,
                    invalidation_events=invalidations,
                    merge_clock_ms=build_clock,
                    crash=crash,
                    job_id=None,
                    job_status="applied",
                )
            return _build_result(
                connection,
                mode="full" if active is None else "rebase",
                reason=(
                    "initial v1+v2 merge"
                    if active is None
                    else "base snapshot rebased at a new logical clock"
                ),
                v1=v1,
                v2=v2_base,
                candidates=base_candidates,
                closures=closures,
                invalidations=invalidations,
            )

        package_path = policy_package.resolve()
        package = load_v2_package(package_path)
        if active is None:
            raise BusinessError(
                "POLICY_NOT_BUILT",
                "An active policy must exist before applying an update package",
            )
        if compare_semver(package.manifest_version, active["manifest_version"]) < 0:
            raise BusinessError(
                "POLICY_ROLLBACK",
                "Policy package manifest version is older than the active version",
                version=package.manifest_version,
                active_manifest_version=active["manifest_version"],
            )
        if package.policy_generation < active["policy_generation"]:
            raise BusinessError(
                "POLICY_GENERATION_ROLLBACK",
                "Policy package generation is older than the active generation",
                version=package.manifest_version,
                incoming_generation=package.policy_generation,
                active_generation=active["policy_generation"],
            )
        if package.policy_generation == active["policy_generation"]:
            if package.bundle_digest == active["source_bundle_digest"]:
                _record_apply_job(
                    connection,
                    package_path=package_path,
                    package=package,
                    status="noop",
                )
                _, candidates, closures = load_active_view(connection)
                return _build_result(
                    connection,
                    mode="noop",
                    reason="identical policy package is already active",
                    v1=v1,
                    v2=package,
                    candidates=candidates,
                    closures=closures,
                    invalidations=[],
                )
            raise BusinessError(
                "POLICY_GENERATION_CONFLICT",
                "A different package was already applied at this generation",
                version=package.manifest_version,
                policy_generation=package.policy_generation,
                active_bundle_digest=active["source_bundle_digest"],
                incoming_bundle_digest=package.bundle_digest,
            )
        if package.update_mode == "incremental":
            if package.base_generation != active["policy_generation"]:
                raise BusinessError(
                    "POLICY_BASE_GENERATION_MISMATCH",
                    "Incremental package base_generation does not match the "
                    "active generation",
                    version=package.manifest_version,
                    base_generation=package.base_generation,
                    active_generation=active["policy_generation"],
                )
        job = _record_apply_job(
            connection,
            package_path=package_path,
            package=package,
            status="pending",
        )
        current_nodes = candidates_from_rows(
            policy_rows(connection, int(active["policy_version_id"]))
        )
        raw_previous = closure_rows(connection, int(active["policy_version_id"]))
        previous_closures = {
            name: ClosureRecord(
                template_name=row["template_name"],
                closure=tuple(row["closure"]),
                closure_digest=row["closure_digest"],
                config_fingerprint=row["config_fingerprint"],
                dependency_digest=row["dependency_digest"],
                revision=row["revision"],
            )
            for name, row in raw_previous.items()
        }
        if package.update_mode == "incremental":
            candidates = overlay_incremental(current_nodes, package)
            mode = "incremental"
        else:
            candidates = merge_v1_v2(
                v1,
                package,
                injected_clock_ms=build_clock,
            )
            mode = "full_replacement"
        closures, invalidations = build_closures(
            candidates,
            previous_nodes=current_nodes,
            previous_closures=previous_closures,
        )
        with transaction(connection):
            version_id = _insert_policy_version(
                connection,
                policy_key=package.policy_key,
                manifest_version=package.manifest_version,
                policy_generation=package.policy_generation,
                logical_clock=package.logical_clock,
                source_bundle_digest=package.bundle_digest,
                candidates=candidates,
                closures=closures,
                mode=mode,
                parent_version_id=int(active["policy_version_id"]),
                source_info=source_summary(v1, package),
                topology_invalidations=package.topology_invalidations,
                invalidation_events=invalidations,
                merge_clock_ms=package.logical_clock,
                crash=crash,
                job_id=job["job_id"],
                job_status="applied",
            )
        return _build_result(
            connection,
            mode=mode,
            reason=(
                "incremental v2 package appended"
                if mode == "incremental"
                else "v2 package fully replaced the active policy view"
            ),
            v1=v1,
            v2=package,
            candidates=candidates,
            closures=closures,
            invalidations=invalidations,
        )
    finally:
        connection.close()


def build_workspace(
    workdir: Path,
    *,
    clock_ms: int | None = None,
    policy_package: Path | None = None,
    crash_at: str | None = None,
) -> dict[str, Any]:
    """Serialize policy builds/replacements across processes."""

    with workspace_lock(workdir, "policy"):
        return _build_workspace_locked(
            workdir,
            clock_ms=clock_ms,
            policy_package=policy_package,
            crash_at=crash_at,
        )


def pending_policy_jobs(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT * FROM policy_apply_jobs
        WHERE status='pending'
        ORDER BY created_at_ms, job_id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def apply_pending_policy_jobs(
    workdir: Path,
    *,
    crash_at: str | None = None,
) -> list[dict[str, Any]]:
    connection = connect(workdir)
    try:
        init_schema(connection)
        jobs = pending_policy_jobs(connection)
    finally:
        connection.close()
    results: list[dict[str, Any]] = []
    for job in jobs:
        result = build_workspace(
            workdir,
            policy_package=Path(job["package_path"]),
            crash_at=crash_at,
        )
        results.append(
            {
                "job_id": job["job_id"],
                "package_path": job["package_path"],
                "result_mode": result.get("mode"),
            }
        )
    return results
