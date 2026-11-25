"""End-to-end deterministic demo used by the CLI and acceptance record."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .cache import ContentAddressedCache
from .invalidation import InvalidationEngine
from .models import ResourceLimits, RoutingPolicy
from .routing import RoutingSession, build_dependencies
from .snapshots import SnapshotStore
from .validation import load_cases
from .weights import build_expert_bank, build_weight_bundle, generate_cases


def _snapshot_payload(
    case: Any,
    bundle: Any,
    limits: ResourceLimits,
) -> dict[str, Any]:
    return {
        "policy": RoutingPolicy.from_case(case).to_dict(),
        "experts": {
            "revisions": bundle.expert_bank.revision_map,
            "bank_digest": bundle.expert_bank.digest,
            "weight_digests": {
                str(expert_id): bundle.expert_bank.expert_digest(expert_id)
                for expert_id in range(case.num_experts)
            },
        },
        "schedule": limits.to_dict(),
    }


def _case_summary(
    session: RoutingSession,
    *,
    query_zero: dict[str, Any],
    consistency: dict[str, Any],
) -> dict[str, Any]:
    overflow_count = sum(
        len(session.blocks[index].assignment.overflow)
        for index in range(session.block_count)
    )
    return {
        "case_id": session.case.case_id,
        "token_len": session.case.token_len,
        "hidden_dim": session.case.hidden_dim,
        "num_experts": session.case.num_experts,
        "top_k": session.case.top_k,
        "capacity_factor": session.case.capacity_factor,
        "router_revision": session.case.router_revision,
        "block_size": session.case.block_size,
        "block_count": session.block_count,
        "first_block_query_ms": query_zero["first_block_query_ms"],
        "first_block_expert_revisions": query_zero["expert_revisions_used"],
        "first_block_capacity_summary": query_zero["capacity_summary"],
        "total_overflow_assignments": overflow_count,
        "consistency": consistency,
        "block_cache_records": [
            {
                "block_index": index,
                "records": {
                    kind: {
                        "key": record["key"],
                        "content_hash": record["content_hash"],
                    }
                    for kind, record in session.blocks[index].cache_records.items()
                },
            }
            for index in range(session.block_count)
        ],
    }


def _choose_invalidation_case(sessions: list[RoutingSession]) -> tuple[RoutingSession, int] | None:
    best: tuple[int, RoutingSession, int] | None = None
    for session in sessions:
        for expert_id in range(session.case.num_experts):
            selected_blocks = [
                index
                for index in range(session.block_count)
                if expert_id
                in {
                    int(value)
                    for value in session.blocks[index].gate.selected_experts.ravel()
                }
            ]
            if not selected_blocks:
                continue
            prefix = selected_blocks[0]
            if prefix > 0 and (best is None or prefix > best[0]):
                best = (prefix, session, expert_id)
    if best is None:
        return None
    return best[1], best[2]


def run_demo(
    *,
    cases_path: str | Path,
    workdir: str | Path,
    reset: bool = False,
    limits: ResourceLimits | None = None,
    tolerance: float = 1e-12,
) -> dict[str, Any]:
    limits = limits or ResourceLimits()
    workdir_path = Path(workdir)
    if reset and workdir_path.exists():
        shutil.rmtree(workdir_path)
    workdir_path.mkdir(parents=True, exist_ok=True)
    cache = ContentAddressedCache(workdir_path / "cache")
    cases = load_cases(cases_path)

    sessions: list[RoutingSession] = []
    case_reports: list[dict[str, Any]] = []
    for case in cases:
        bundle = build_weight_bundle(case)
        session = RoutingSession(bundle, cache, limits=limits)
        snapshot_store = SnapshotStore(workdir_path / "snapshots" / case.case_id)
        snapshot_store.commit(
            version=case.router_revision,
            **_snapshot_payload(case, bundle, limits),
        )
        session.compute_next_block()
        query_zero = session.query_block(0)
        while session.next_block_index < session.block_count:
            session.compute_next_block()
        for index in range(session.block_count):
            session.query_block(index)
        consistency = session.consistency_report(
            tolerance=tolerance, raise_on_mismatch=True
        )
        sessions.append(session)
        case_reports.append(
            _case_summary(
                session,
                query_zero=query_zero,
                consistency=consistency,
            )
        )

    invalidation_report: dict[str, Any] | None = None
    chosen = _choose_invalidation_case(sessions)
    if chosen is not None:
        old_session, changed_expert = chosen
        old_case = old_session.case
        old_bank = old_session.bundle.expert_bank
        new_revision = old_case.router_revision + 1
        new_case = old_case.with_updates(router_revision=new_revision)
        all_revisions = {
            expert_id: (
                new_revision
                if expert_id == changed_expert
                else old_bank.revisions[expert_id]
            )
            for expert_id in range(old_case.num_experts)
        }
        new_bank = build_expert_bank(new_case, all_revisions)
        new_bundle = build_weight_bundle(new_case, all_revisions)
        if new_bundle.expert_bank.digest != new_bank.digest:
            raise AssertionError("expert bank reconstruction is not deterministic")
        stale_probe = {
            "case_id": old_case.case_id,
            "block_index": 0,
            "code": None,
        }
        try:
            old_session.cache.load_checked_artifact(
                case_id=old_case.case_id,
                block_index=0,
                kind="block_output",
                expected_dependencies=build_dependencies(
                    new_case,
                    new_bundle,
                    block_index=0,
                    token_start=0,
                    token_end=min(new_case.block_size, new_case.token_len),
                ),
            )
        except Exception as exc:
            stale_probe["code"] = getattr(exc, "code", type(exc).__name__)
            stale_probe["error_case_id"] = getattr(exc, "case_id", None)
            stale_probe["error_block_index"] = getattr(exc, "block_index", None)
        engine = InvalidationEngine(limits)
        invalidation = engine.apply(
            old_session=old_session,
            new_bundle=new_bundle,
            strategy="suffix",
            parallel_blocks=2,
            check_consistency=True,
            consistency_tolerance=tolerance,
        )
        new_store = SnapshotStore(workdir_path / "snapshots" / old_case.case_id)
        v1 = new_store.load_current()
        v2 = new_store.commit(
            version=new_revision,
            expected_version=old_case.router_revision,
            **_snapshot_payload(new_case, new_bundle, limits),
        )
        rollback_code = None
        try:
            new_store.commit(
                version=old_case.router_revision,
                **_snapshot_payload(old_case, old_session.bundle, limits),
            )
        except Exception as exc:  # report the business code in the artifact
            rollback_code = getattr(exc, "code", type(exc).__name__)
        invalidation_report = {
            "case_id": old_case.case_id,
            "changed_expert": changed_expert,
            "old_revision": old_case.router_revision,
            "new_revision": new_revision,
            "plan": invalidation.plan,
            "consistency": invalidation.consistency_report,
            "stale_result_probe": stale_probe,
            "snapshot": {
                "v1_version": v1["version"] if v1 else None,
                "v1_checksum": v1["checksum"] if v1 else None,
                "v2_version": v2["version"],
                "v2_checksum": v2["checksum"],
                "current_version_after_rollback_attempt": new_store.current_version(),
                "rollback_attempt_code": rollback_code,
            },
        }

    object_check = cache.verify_all_objects()
    report = {
        "component": "moe-router",
        "format_version": "moe-router-v1",
        "cases_file": str(cases_path),
        "seed_policy": "all tokens, gates, and experts generated locally with NumPy SeedSequence",
        "limits": limits.to_dict(),
        "case_count": len(case_reports),
        "all_consistent": all(
            item["consistency"]["consistent"] for item in case_reports
        ),
        "cases": case_reports,
        "invalidation": invalidation_report,
        "cache_object_integrity": object_check,
    }
    report_path = workdir_path / "validation_report.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return report


def run_generate(
    *,
    cases_path: str | Path,
    manifest_path: str | Path,
    seed: int,
    count: int,
) -> dict[str, Any]:
    from .validation import write_cases
    from .weights import manifest_rows

    cases = generate_cases(seed=seed, count=count)
    write_cases(cases_path, cases)
    rows = manifest_rows(cases)
    Path(manifest_path).parent.mkdir(parents=True, exist_ok=True)
    Path(manifest_path).write_text(
        json.dumps({"seed": seed, "cases": rows}, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"cases": len(cases), "cases_path": str(cases_path), "manifest_path": str(manifest_path)}
