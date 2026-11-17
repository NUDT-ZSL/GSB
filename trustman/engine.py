"""Application orchestration, deterministic reporting and checkpoint recovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .atomic import atomic_write_json, workspace_lock
from .checkpoint import (
    CheckpointLoad,
    CheckpointStore,
    normalized_results,
    replay_mismatches,
)
from .errors import (
    E_CHECKPOINT_CORRUPT,
    E_CHECKPOINT_INPUT_CHANGED,
    E_CHECKPOINT_MISSING,
    E_CHECKPOINT_POLICY_CHANGED,
    E_CHECKPOINT_REPLAY_MISMATCH,
    E_CHECKPOINT_STRUCTURE_CHANGED,
    E_INTERNAL,
    E_QUEUE_FULL,
    E_SCHEMA,
)
from .models import (
    CHECKPOINT_STRUCTURE_VERSION,
    REPORT_SCHEMA,
    Certificate,
    ChainDeclaration,
    Rejection,
    Snapshot,
    certificate_map,
    effective_snapshot,
    sha256_value,
    snapshot_report_dict,
)
from .parser import (
    ParseResult,
    parse_certificate_files,
    parse_chain_files,
    parse_snapshot_files,
    resolve_snapshot_candidates,
)
from .scheduler import (
    SchedulerConfig,
    SchedulerOutcome,
    TaskObserver,
    run_scheduled,
)
from .store import (
    load_snapshot_candidates,
    merge_snapshot_candidates,
    save_snapshot_candidates,
)
from .validation import (
    StepHook,
    build_expiration_propagation,
    build_revocation_propagation,
    validate_chain,
)


@dataclass
class SnapshotContext:
    accepted: list[Snapshot]
    all_candidates: list[Snapshot]
    rejections: list[Rejection]

    @property
    def effective(self) -> Snapshot | None:
        return effective_snapshot(self.accepted)


@dataclass
class ValidationOutput:
    report: dict[str, Any]
    scheduler: SchedulerOutcome | None = None


@dataclass
class RunMetadata:
    checkpoint_loaded: bool = False
    checkpoint_rebuilt: bool = False
    checkpoint_reason: str | None = None
    checkpoint_message: str | None = None
    reused_chain_ids: list[str] = field(default_factory=list)
    executed_chain_ids: list[str] = field(default_factory=list)
    replay_checked_chain_ids: list[str] = field(default_factory=list)
    ignored_temp_files: list[str] = field(default_factory=list)
    lock: str = "acquired"
    report_written: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_loaded": self.checkpoint_loaded,
            "checkpoint_rebuilt": self.checkpoint_rebuilt,
            "checkpoint_reason": self.checkpoint_reason,
            "checkpoint_message": self.checkpoint_message,
            "reused_chain_ids": sorted(self.reused_chain_ids),
            "executed_chain_ids": sorted(self.executed_chain_ids),
            "replay_checked_chain_ids": sorted(self.replay_checked_chain_ids),
            "ignored_temp_files": sorted(self.ignored_temp_files),
            "lock": self.lock,
            "report_written": self.report_written,
        }


@dataclass
class RunOutcome:
    report: dict[str, Any]
    metadata: RunMetadata


def _basis(snapshot: Snapshot | None) -> dict[str, Any]:
    return {
        "snapshot_version": snapshot.version if snapshot is not None else None,
        "policy_version": snapshot.policy_version if snapshot is not None else None,
        "observed_at_ms": snapshot.observed_at_ms if snapshot is not None else None,
        "snapshot_fingerprint": (
            snapshot.content_fingerprint if snapshot is not None else None
        ),
    }


def _attach_basis(
    results: Iterable[dict[str, Any]], snapshot: Snapshot | None
) -> None:
    basis = _basis(snapshot)
    for result in results:
        if result.get("basis") is None:
            result["basis"] = dict(basis)


def _task_function(
    certificates: dict[str, Certificate],
    snapshot: Snapshot | None,
    step_hook: StepHook | None,
) -> Callable[[ChainDeclaration, float], dict]:
    def run(chain: ChainDeclaration, deadline: float) -> dict:
        return validate_chain(
            chain,
            certificates,
            snapshot,
            deadline_monotonic=deadline,
            step_hook=step_hook,
        )

    return run


def _schema_chain_records(
    rejections: Iterable[Rejection],
    snapshot: Snapshot | None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for rejection in rejections:
        if rejection.scope != "chain" or rejection.entry_index < 0:
            continue
        chain_id = rejection.chain_id or (
            f"__invalid__:{rejection.source_file}:{rejection.entry_index}"
        )
        records.append(
            {
                "chain_id": chain_id,
                "certificate_ids": [],
                "deadline_ms": None,
                "expected_summary": None,
                "source_file": rejection.source_file,
                "entry_index": rejection.entry_index,
                "task_ref": f"{rejection.source_file}#{rejection.entry_index}",
                "status": "rejected",
                "conclusion": None,
                "errors": [
                    {
                        "code": E_SCHEMA,
                        "position": None,
                        "cert_id": None,
                        "reason": rejection.reason,
                        "message": rejection.message,
                    }
                ],
                "invalidations": [],
                "basis": _basis(snapshot),
                "actual_summary": None,
                "matches_expected": False,
            }
        )
    return records


def _rejection_ledger(
    parser_rejections: Iterable[Rejection],
    valid_results: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[Rejection]]:
    ledger_rejections = list(parser_rejections)
    result_list = list(valid_results)
    for result in result_list:
        if result.get("status") != "rejected":
            continue
        errors = result.get("errors") or [{}]
        first_error = errors[0]
        code = first_error.get("code", E_INTERNAL)
        ledger_rejections.append(
            Rejection(
                scope="task",
                source_file=result.get("source_file", ""),
                entry_index=result.get("entry_index", -1),
                code=code,
                reason=first_error.get("reason", "task_rejected"),
                message=first_error.get("message", "task was rejected"),
                chain_id=result.get("chain_id"),
            )
        )
    ledger_rejections.sort(key=lambda item: item.sort_key)
    return [item.as_dict() for item in ledger_rejections], ledger_rejections


def _build_report(
    *,
    chain_parse: ParseResult,
    certificate_parse: ParseResult,
    snapshot_context: SnapshotContext,
    scheduler_outcome: SchedulerOutcome,
    source_rejections: Iterable[Rejection],
) -> dict[str, Any]:
    accepted_chains: list[ChainDeclaration] = chain_parse.items
    effective = snapshot_context.effective
    valid_results = list(scheduler_outcome.results.values())
    _attach_basis(valid_results, effective)

    chain_records = [
        {
            **result,
            "task_ref": (
                f"{result.get('source_file', '')}@"
                f"{result.get('entry_index', -1)}"
            ),
        }
        for result in valid_results
    ]
    chain_records.extend(
        _schema_chain_records(chain_parse.rejections, effective)
    )
    chain_records.sort(
        key=lambda item: (
            item["chain_id"],
            item.get("source_file", ""),
            item.get("entry_index", -1),
        )
    )

    parser_rejections = (
        list(source_rejections)
        + list(certificate_parse.rejections)
        + list(chain_parse.rejections)
    )
    ledger, ledger_objects = _rejection_ledger(parser_rejections, valid_results)

    succeeded = [record for record in chain_records if record["status"] == "success"]
    timed_out = [record for record in chain_records if record["status"] == "timeout"]
    over_budget = [record for record in chain_records if record["status"] == "budget"]
    rejected = [record for record in chain_records if record["status"] == "rejected"]
    queue_rejected = [
        record
        for record in rejected
        if any(
            error.get("code") == E_QUEUE_FULL for error in record["errors"]
        )
    ]
    schema_rejected = [
        record
        for record in rejected
        if any(error.get("code") == E_SCHEMA for error in record["errors"])
    ]
    internal_rejected = [
        record
        for record in rejected
        if any(error.get("code") == E_INTERNAL for error in record["errors"])
    ]
    by_conclusion = {
        "trusted": sum(
            1 for record in succeeded if record["conclusion"] == "trusted"
        ),
        "partially_trusted": sum(
            1
            for record in succeeded
            if record["conclusion"] == "partially_trusted"
        ),
        "untrusted": sum(
            1 for record in succeeded if record["conclusion"] == "untrusted"
        ),
    }
    input_rejections = [
        item
        for item in ledger_objects
        if item.scope in {"certificate", "snapshot"}
        or (item.scope == "chain" and item.entry_index < 0)
    ]
    task_rejections = [
        item
        for item in ledger_objects
        if item.scope == "task"
        or (item.scope == "chain" and item.entry_index >= 0)
    ]
    summary = {
        "tasks_total": len(chain_records),
        "succeeded": len(succeeded),
        "timed_out": len(timed_out),
        "over_budget": len(over_budget),
        "rejected": len(rejected),
        "queue_rejected": len(queue_rejected),
        "schema_rejected": len(schema_rejected),
        "internal_rejected": len(internal_rejected),
        "input_rejections": len(input_rejections),
        "task_rejections": len(task_rejections),
        "rejections_total": len(ledger),
        "by_conclusion": by_conclusion,
        "expected_mismatches": sum(
            1 for record in succeeded if not record["matches_expected"]
        ),
    }
    task_counts_match = (
        summary["tasks_total"]
        == summary["succeeded"]
        + summary["timed_out"]
        + summary["over_budget"]
        + summary["rejected"]
    )
    rejection_counts_match = (
        summary["rejected"] == summary["task_rejections"]
        and summary["rejections_total"]
        == summary["input_rejections"] + summary["task_rejections"]
    )
    detail_status_counts_match = (
        summary["succeeded"]
        == sum(1 for record in chain_records if record["status"] == "success")
        and summary["timed_out"]
        == sum(1 for record in chain_records if record["status"] == "timeout")
        and summary["over_budget"]
        == sum(1 for record in chain_records if record["status"] == "budget")
        and summary["rejected"]
        == sum(1 for record in chain_records if record["status"] == "rejected")
    )
    chain_order = [
        (record["chain_id"], record.get("source_file"), record.get("entry_index"))
        for record in chain_records
    ]
    snapshot_order = [
        (snapshot.version, snapshot.content_fingerprint)
        for snapshot in snapshot_context.accepted
    ]
    reconciliation = {
        "equation": (
            "tasks_total = succeeded + timed_out + over_budget + rejected"
        ),
        "rejection_equation": (
            "rejections_total = input_rejections + task_rejections"
        ),
        "task_counts_match": task_counts_match,
        "rejection_counts_match": rejection_counts_match,
        "detail_status_counts_match": detail_status_counts_match,
        "chain_order_is_stable": chain_order == sorted(chain_order),
        "snapshot_order_is_stable": snapshot_order == sorted(snapshot_order),
    }
    if not all(
        value
        for key, value in reconciliation.items()
        if key.endswith("_match") or key.startswith("chain_order")
        or key.startswith("snapshot_order")
    ):
        raise RuntimeError("report reconciliation invariant failed")

    cert_by_id = certificate_map(certificate_parse.items)
    return {
        "schema": REPORT_SCHEMA,
        "effective_snapshot": (
            snapshot_report_dict(effective) if effective is not None else None
        ),
        "snapshots": [
            snapshot_report_dict(snapshot)
            for snapshot in snapshot_context.accepted
        ],
        "chains": chain_records,
        "rejections": ledger,
        "revocation_propagation": build_revocation_propagation(
            accepted_chains, cert_by_id, effective
        ),
        "expiration_propagation": build_expiration_propagation(
            accepted_chains, cert_by_id, effective
        ),
        "summary": summary,
        "reconciliation": reconciliation,
    }


def validate_dataset(
    *,
    certificate_parse: ParseResult,
    chain_parse: ParseResult,
    snapshot_context: SnapshotContext,
    source_rejections: Iterable[Rejection],
    config: SchedulerConfig,
    resume_results: dict[str, dict[str, Any]] | None = None,
    progress_callback: Callable[[dict[str, dict[str, Any]]], None] | None = None,
    task_observer: TaskObserver | None = None,
    step_hook: StepHook | None = None,
) -> ValidationOutput:
    cert_by_id = certificate_map(certificate_parse.items)
    effective = snapshot_context.effective
    task = _task_function(cert_by_id, effective, step_hook)
    outcome = run_scheduled(
        chain_parse.items,
        task,
        config,
        resume_results=resume_results,
        progress_callback=progress_callback,
        task_observer=task_observer,
    )
    _attach_basis(outcome.results.values(), effective)
    report = _build_report(
        chain_parse=chain_parse,
        certificate_parse=certificate_parse,
        snapshot_context=snapshot_context,
        scheduler_outcome=outcome,
        source_rejections=source_rejections,
    )
    return ValidationOutput(report=report, scheduler=outcome)


def load_and_merge_snapshots(
    workspace: str | Path, snapshot_paths: Iterable[str | Path]
) -> SnapshotContext:
    existing, state_rejections = load_snapshot_candidates(workspace)
    parsed = parse_snapshot_files(snapshot_paths)
    merged = merge_snapshot_candidates(existing, parsed.items)
    if parsed.items:
        save_snapshot_candidates(workspace, merged)
    accepted, conflicts, all_candidates = resolve_snapshot_candidates(merged)
    return SnapshotContext(
        accepted=accepted,
        all_candidates=all_candidates,
        rejections=list(state_rejections)
        + list(parsed.rejections)
        + list(conflicts),
    )


def _fingerprints(
    certificate_parse: ParseResult,
    chain_parse: ParseResult,
    snapshot_context: SnapshotContext,
    config: SchedulerConfig,
) -> dict[str, str]:
    return {
        "snapshots": sha256_value(
            [
                snapshot.canonical_dict()
                for snapshot in snapshot_context.accepted
            ]
        ),
        "certificates": sha256_value(
            [certificate.canonical_dict() for certificate in certificate_parse.items]
        ),
        "chains": sha256_value(
            [chain.canonical_dict() for chain in chain_parse.items]
        ),
        "scheduler": sha256_value(config.fingerprint()),
    }


def _checkpoint_payload(
    *,
    snapshot_context: SnapshotContext,
    fingerprints: dict[str, str],
    results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    effective = snapshot_context.effective
    return {
        "policy_version": (
            effective.policy_version if effective is not None else None
        ),
        "snapshot_watermark": (
            effective.version if effective is not None else 0
        ),
        "snapshot_fingerprint": fingerprints["snapshots"],
        "certificates_fingerprint": fingerprints["certificates"],
        "chains_fingerprint": fingerprints["chains"],
        "scheduler_fingerprint": fingerprints["scheduler"],
        "results": normalized_results(results.values()),
    }


def _checkpoint_compatibility(
    loaded: CheckpointLoad,
    *,
    snapshot_context: SnapshotContext,
    fingerprints: dict[str, str],
) -> tuple[str | None, str | None]:
    if loaded.status == "corrupt":
        return E_CHECKPOINT_CORRUPT, loaded.message or "checkpoint is corrupt"
    if loaded.status == "missing":
        return None, None
    if loaded.status != "ok" or loaded.payload is None:
        return E_CHECKPOINT_CORRUPT, "checkpoint state is unavailable"
    if loaded.structure_version != CHECKPOINT_STRUCTURE_VERSION:
        return (
            E_CHECKPOINT_STRUCTURE_CHANGED,
            "checkpoint structure version changed",
        )
    payload = loaded.payload
    required = {
        "policy_version",
        "snapshot_watermark",
        "snapshot_fingerprint",
        "certificates_fingerprint",
        "chains_fingerprint",
        "scheduler_fingerprint",
        "results",
    }
    if set(payload) != required or not isinstance(payload.get("results"), dict):
        return E_CHECKPOINT_CORRUPT, "checkpoint payload fields are invalid"
    effective = snapshot_context.effective
    current_policy = (
        effective.policy_version if effective is not None else None
    )
    if payload.get("policy_version") != current_policy:
        return (
            E_CHECKPOINT_POLICY_CHANGED,
            "effective policy version changed; full rebuild required",
        )
    current_watermark = effective.version if effective is not None else 0
    if (
        payload.get("snapshot_watermark") != current_watermark
        or payload.get("snapshot_fingerprint") != fingerprints["snapshots"]
        or payload.get("certificates_fingerprint")
        != fingerprints["certificates"]
        or payload.get("chains_fingerprint") != fingerprints["chains"]
        or payload.get("scheduler_fingerprint") != fingerprints["scheduler"]
    ):
        return (
            E_CHECKPOINT_INPUT_CHANGED,
            "input or scheduler identity changed; full rebuild required",
        )
    return None, None


def _verify_checkpoint_replay(
    *,
    stored_results: dict[str, dict[str, Any]],
    certificate_parse: ParseResult,
    chain_parse: ParseResult,
    snapshot_context: SnapshotContext,
) -> list[str]:
    accepted = {chain.chain_id: chain for chain in chain_parse.items}
    cert_by_id = certificate_map(certificate_parse.items)
    effective = snapshot_context.effective
    mismatches = sorted(set(stored_results) - set(accepted))
    expected: dict[str, dict[str, Any]] = {}
    for chain_id, stored in sorted(stored_results.items()):
        if stored.get("status") != "success":
            continue
        chain = accepted.get(chain_id)
        if chain is None:
            expected[chain_id] = {
                "status": "missing",
                "conclusion": None,
                "errors": [],
                "invalidations": [],
                "basis": None,
                "actual_summary": None,
                "matches_expected": False,
            }
            continue
        business = validate_chain(chain, cert_by_id, effective)
        expected[chain_id] = {"status": "success", **business}
    mismatches.extend(
        replay_mismatches(
            {
                chain_id: result
                for chain_id, result in stored_results.items()
                if result.get("status") == "success"
            },
            expected,
        )
    )
    return sorted(set(mismatches))


def run_workspace_validation(
    *,
    workspace: str | Path,
    certificate_paths: Iterable[str | Path],
    chain_paths: Iterable[str | Path],
    snapshot_paths: Iterable[str | Path] = (),
    config: SchedulerConfig | None = None,
    use_resume: bool = True,
    report_path: str | Path | None = None,
    task_observer: TaskObserver | None = None,
    step_hook: StepHook | None = None,
) -> RunOutcome:
    config = config or SchedulerConfig()
    workspace_path = Path(workspace)
    metadata = RunMetadata()
    with workspace_lock(workspace_path):
        snapshot_context = load_and_merge_snapshots(
            workspace_path, snapshot_paths
        )
        certificate_parse = parse_certificate_files(certificate_paths)
        chain_parse = parse_chain_files(chain_paths)
        fingerprints = _fingerprints(
            certificate_parse,
            chain_parse,
            snapshot_context,
            config,
        )
        checkpoint_store = CheckpointStore(workspace_path)
        loaded = checkpoint_store.load()
        metadata.ignored_temp_files = list(loaded.ignored_temp_files)
        resume_results: dict[str, dict[str, Any]] = {}
        if use_resume:
            metadata.checkpoint_loaded = loaded.status == "ok"
            reason, message = _checkpoint_compatibility(
                loaded,
                snapshot_context=snapshot_context,
                fingerprints=fingerprints,
            )
            if reason is not None:
                metadata.checkpoint_rebuilt = True
                metadata.checkpoint_reason = reason
                metadata.checkpoint_message = message
            elif loaded.status == "ok" and loaded.payload is not None:
                stored_results = loaded.payload["results"]
                mismatches = _verify_checkpoint_replay(
                    stored_results=stored_results,
                    certificate_parse=certificate_parse,
                    chain_parse=chain_parse,
                    snapshot_context=snapshot_context,
                )
                if mismatches:
                    metadata.checkpoint_rebuilt = True
                    metadata.checkpoint_reason = (
                        E_CHECKPOINT_REPLAY_MISMATCH
                    )
                    metadata.checkpoint_message = (
                        "checkpoint replay differed for: "
                        + ", ".join(mismatches)
                    )
                else:
                    resume_results = stored_results
                    metadata.replay_checked_chain_ids = sorted(
                        chain_id
                        for chain_id in stored_results
                        if stored_results[chain_id].get("status") == "success"
                    )
        else:
            if loaded.status == "ok":
                metadata.checkpoint_rebuilt = True
                metadata.checkpoint_reason = "resume_disabled"
                metadata.checkpoint_message = "resume disabled by caller"

        effective = snapshot_context.effective

        def progress(results: dict[str, dict[str, Any]]) -> None:
            _attach_basis(results.values(), effective)
            payload = _checkpoint_payload(
                snapshot_context=snapshot_context,
                fingerprints=fingerprints,
                results=results,
            )
            checkpoint_store.save(payload)

        output = validate_dataset(
            certificate_parse=certificate_parse,
            chain_parse=chain_parse,
            snapshot_context=snapshot_context,
            source_rejections=snapshot_context.rejections,
            config=config,
            resume_results=resume_results,
            progress_callback=progress,
            task_observer=task_observer,
            step_hook=step_hook,
        )
        assert output.scheduler is not None
        metadata.reused_chain_ids = list(output.scheduler.reused_chain_ids)
        metadata.executed_chain_ids = list(output.scheduler.executed_chain_ids)
        if report_path is not None:
            atomic_write_json(report_path, output.report, pretty=True)
            metadata.report_written = True
        return RunOutcome(report=output.report, metadata=metadata)


def import_snapshots(
    *, workspace: str | Path, snapshot_paths: Iterable[str | Path]
) -> tuple[SnapshotContext, dict[str, Any]]:
    workspace_path = Path(workspace)
    with workspace_lock(workspace_path):
        existing, state_rejections = load_snapshot_candidates(workspace_path)
        parsed = parse_snapshot_files(snapshot_paths)
        merged = merge_snapshot_candidates(existing, parsed.items)
        if parsed.items:
            save_snapshot_candidates(workspace_path, merged)
        accepted, conflicts, all_candidates = resolve_snapshot_candidates(merged)
        rejections = (
            list(state_rejections) + list(parsed.rejections) + list(conflicts)
        )
        effective = effective_snapshot(accepted)
        existing_keys = {
            (snapshot.version, snapshot.content_fingerprint)
            for snapshot in existing
        }
        imported_versions = sorted(
            {
                snapshot.version
                for snapshot in parsed.items
                if (snapshot.version, snapshot.content_fingerprint)
                not in existing_keys
            }
        )
        result = {
            "ok": not rejections,
            "command": "import-snapshot",
            "imported_versions": imported_versions,
            "snapshot_versions": [snapshot.version for snapshot in accepted],
            "effective_snapshot_version": (
                effective.version if effective is not None else None
            ),
            "effective_policy_version": (
                effective.policy_version if effective is not None else None
            ),
            "rejections": [
                rejection.as_dict()
                for rejection in sorted(
                    rejections, key=lambda item: item.sort_key
                )
            ],
        }
        return (
            SnapshotContext(
                accepted=accepted,
                all_candidates=all_candidates,
                rejections=rejections,
            ),
            result,
        )


def inspect_checkpoint(workspace: str | Path) -> dict[str, Any]:
    store = CheckpointStore(workspace)
    loaded = store.load()
    base = {
        "command": "check-checkpoint",
        "status": loaded.status,
        "path": loaded.path,
        "reason": loaded.reason,
        "message": loaded.message,
        "ignored_temp_files": loaded.ignored_temp_files,
        "has_half_write_residue": bool(loaded.ignored_temp_files),
    }
    if loaded.status == "missing":
        return {
            **base,
            "ok": False,
            "code": E_CHECKPOINT_MISSING,
            "rebuild_required": False,
        }
    if loaded.status == "corrupt" or loaded.payload is None:
        return {
            **base,
            "ok": False,
            "code": E_CHECKPOINT_CORRUPT,
            "rebuild_required": True,
        }
    payload = loaded.payload
    required_payload_fields = {
        "policy_version",
        "snapshot_watermark",
        "snapshot_fingerprint",
        "certificates_fingerprint",
        "chains_fingerprint",
        "scheduler_fingerprint",
        "results",
    }
    if set(payload) != required_payload_fields or not isinstance(
        payload.get("results"), dict
    ):
        return {
            **base,
            "ok": False,
            "code": E_CHECKPOINT_CORRUPT,
            "message": "checkpoint payload fields are invalid",
            "rebuild_required": True,
        }
    if loaded.structure_version != CHECKPOINT_STRUCTURE_VERSION:
        return {
            **base,
            "ok": False,
            "code": E_CHECKPOINT_STRUCTURE_CHANGED,
            "rebuild_required": True,
            "checkpoint_structure_version": loaded.structure_version,
            "current_structure_version": CHECKPOINT_STRUCTURE_VERSION,
        }
    candidates, _ = load_snapshot_candidates(workspace)
    accepted, _, _ = resolve_snapshot_candidates(candidates)
    effective = effective_snapshot(accepted)
    current_policy = (
        effective.policy_version if effective is not None else None
    )
    current_watermark = effective.version if effective is not None else 0
    if payload.get("policy_version") != current_policy:
        return {
            **base,
            "ok": False,
            "code": E_CHECKPOINT_POLICY_CHANGED,
            "rebuild_required": True,
            "checkpoint_policy_version": payload.get("policy_version"),
            "current_policy_version": current_policy,
        }
    if payload.get("snapshot_watermark") != current_watermark:
        return {
            **base,
            "ok": False,
            "code": E_CHECKPOINT_INPUT_CHANGED,
            "rebuild_required": True,
            "checkpoint_watermark": payload.get("snapshot_watermark"),
            "current_watermark": current_watermark,
        }
    results = payload.get("results", {})
    return {
        **base,
        "ok": True,
        "code": "CHECKPOINT_OK",
        "rebuild_required": False,
        "policy_version": payload.get("policy_version"),
        "snapshot_watermark": payload.get("snapshot_watermark"),
        "result_count": len(results) if isinstance(results, dict) else 0,
        "structure_version": loaded.structure_version,
        "checksum_valid": True,
    }


def report_tasks(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        record["chain_id"]: record
        for record in report.get("chains", [])
        if isinstance(record, dict) and "chain_id" in record
    }
