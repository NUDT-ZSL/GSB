"""Orchestration: parsing, budgets, concurrency, checkpoints and reporting."""

from __future__ import annotations

import json
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import codes
from .budget import (
    Deadline,
    DeadlineExceeded,
    ExpandedBudgetExceeded,
    FragmentLimitExceeded,
    LockConflict,
)
from .chain import (
    compute_propagation,
    determine_observed_clock,
    evaluate_messages,
    own_propagation,
)
from .checkpoint import CheckpointLoad, load_checkpoint, save_checkpoint
from .fragments import primary_reassembly_code, reassemble
from .locks import atomic_write_many
from .models import (
    AnalysisConfig,
    ConnectionResult,
    Diagnostic,
    RawRecord,
    ReassemblyResult,
    TrustView,
    digest_value,
    sha256_hex,
)
from .normalize import group_records, parse_handshake_jsonl
from .report import build_report, report_bytes
from .trust import diff_trust_views, load_trust_view


@dataclass
class ConnectionInput:
    connection_id: str
    records: List[RawRecord] = field(default_factory=list)
    input_diagnostics: List[Diagnostic] = field(default_factory=list)


@dataclass
class AnalysisRun:
    report: Dict[str, Any]
    trust_view: TrustView
    results: Dict[str, ConnectionResult]
    config: AnalysisConfig
    checkpoint_info: Dict[str, Any]
    propagation: List[Dict[str, Any]]
    input_fingerprint: str
    config_fingerprint: str


def _read_optional(path: Optional[str | Path]) -> Optional[bytes]:
    if path is None:
        return None
    return Path(path).read_bytes()


def _v2_shard_hashes(shards_dir: Optional[str | Path]) -> Dict[str, str]:
    if shards_dir is None:
        return {}
    base = Path(shards_dir)
    if not base.exists() or not base.is_dir():
        return {}
    return {
        path.relative_to(base).as_posix(): sha256_hex(path.read_bytes())
        for path in sorted(base.rglob("*.json"), key=lambda p: p.relative_to(base).as_posix())
        if path.is_file()
    }


def compute_fingerprints(
    *,
    handshake_bytes: bytes,
    v1_bytes: Optional[bytes],
    v2_shards: Optional[str | Path],
    v2_db: Optional[str | Path],
    config: AnalysisConfig,
) -> Tuple[str, str, Dict[str, Any]]:
    db_sha = None
    if v2_db is not None:
        db_path = Path(v2_db)
        db_sha = sha256_hex(db_path.read_bytes()) if db_path.exists() else "missing"
    material = {
        "engine_version": codes.ENGINE_VERSION,
        "report_schema_version": codes.REPORT_SCHEMA_VERSION,
        "checkpoint_structure_version": codes.CHECKPOINT_STRUCTURE_VERSION,
        "merge_policy_version": codes.TRUST_MERGE_POLICY_VERSION,
        "sequence_policy_version": codes.SEQUENCE_POLICY_VERSION,
        "handshake_sha256": sha256_hex(handshake_bytes),
        "v1_sha256": sha256_hex(v1_bytes) if v1_bytes is not None else None,
        "v2_shards": _v2_shard_hashes(v2_shards),
        "v2_db_sha256": db_sha,
        "config": config.budget_fingerprint_material(),
    }
    input_fingerprint = digest_value(material)
    config_fingerprint = digest_value(config.budget_fingerprint_material())
    return input_fingerprint, config_fingerprint, material


def _preflight_budget_result(
    connection: ConnectionInput,
    *,
    code: str,
    detail: Dict[str, Any],
    config: AnalysisConfig,
) -> ConnectionResult:
    result = ConnectionResult(
        connection_id=connection.connection_id,
        status="error",
        record_count=len(connection.records),
        directions=sorted({r.direction for r in connection.records}),
        trust_conclusion="indeterminate",
        primary_code=code,
        timeout_ms=config.timeout_ms,
    )
    result.reassembly = ReassemblyResult(
        status="fragment_limit" if code == codes.E_FRAGS else "budget",
        complete=False,
        fragment_count=len(connection.records),
        unique_fragment_count=0,
        expanded_bytes=sum(len(r.payload) for r in connection.records),
    )
    result.issues.extend(connection.input_diagnostics)
    result.issues.append(
        Diagnostic(
            code=code,
            message=(
                "connection fragment count exceeds the configured limit"
                if code == codes.E_FRAGS
                else "connection expanded byte budget is exhausted"
            ),
            connection_id=connection.connection_id,
            detail=detail,
        )
    )
    result.budget = {
        "fragment_count": len(connection.records),
        "expanded_bytes": sum(len(r.payload) for r in connection.records),
        "max_frags_per_connection": config.max_frags_per_connection,
        "max_expanded_bytes_per_connection": config.max_expanded_bytes_per_connection,
        "max_total_expanded_bytes": config.max_total_expanded_bytes,
        **detail,
    }
    result.compute_digest()
    return result


def process_connection(
    connection: ConnectionInput,
    trust_view: TrustView,
    config: AnalysisConfig,
) -> ConnectionResult:
    """Process one connection in isolation.

    All timing checks use ``time.perf_counter``.  On a deadline breach the
    connection returns immediately with E_TIMEOUT; no later stage is run.
    """

    started = time.perf_counter()
    deadline = Deadline(config.timeout_ms)
    result = ConnectionResult(
        connection_id=connection.connection_id,
        timeout_ms=config.timeout_ms,
    )
    result.issues.extend(connection.input_diagnostics)
    records = connection.records
    result.record_count = len(records)
    result.directions = sorted({r.direction for r in records})
    expanded_bytes = sum(len(r.payload) for r in records)
    result.budget = {
        "fragment_count": len(records),
        "expanded_bytes": expanded_bytes,
        "max_frags_per_connection": config.max_frags_per_connection,
        "max_expanded_bytes_per_connection": config.max_expanded_bytes_per_connection,
        "max_total_expanded_bytes": config.max_total_expanded_bytes,
    }

    try:
        deadline.check("start", records[0].record_seq if records else None)
        if len(records) > config.max_frags_per_connection:
            raise FragmentLimitExceeded(
                "connection fragment count exceeds the configured limit",
                fragment_count=len(records),
                max_frags=config.max_frags_per_connection,
            )
        if expanded_bytes > config.max_expanded_bytes_per_connection:
            raise ExpandedBudgetExceeded(
                "connection expanded byte budget is exhausted",
                expanded_bytes=expanded_bytes,
                max_expanded_bytes=config.max_expanded_bytes_per_connection,
            )

        reassembly = reassemble(
            records,
            max_frags=config.max_frags_per_connection,
            max_expanded_bytes=config.max_expanded_bytes_per_connection,
            deadline=deadline,
        )
        result.reassembly = reassembly
        result.issues.extend(reassembly.issues)
        result.budget.update(
            {
                "unique_fragment_count": reassembly.unique_fragment_count,
                "duplicate_count": reassembly.duplicate_count,
                "reassembly_status": reassembly.status,
            }
        )
        deadline.check("after_reassembly")

        if reassembly.messages:
            (
                observed_ms,
                clock_source,
                candidates,
                selected,
                chain_issues,
                conclusion,
                primary_code,
            ) = evaluate_messages(
                reassembly.messages,
                records,
                trust_view,
                connection.connection_id,
                deadline=deadline,
            )
            result.observed_time_ms = observed_ms
            result.clock_source = clock_source
            result.candidate_chains = candidates
            result.selected_chain = selected
            result.issues.extend(chain_issues)
            result.trust_conclusion = conclusion
            result.primary_code = primary_code
            result.revocation_propagation = own_propagation(selected)
        else:
            observed_ms, clock_source, clock_issues = determine_observed_clock(
                [], records, connection.connection_id
            )
            result.observed_time_ms = observed_ms
            result.clock_source = clock_source
            result.issues.extend(clock_issues)
            result.trust_conclusion = "empty_chain"
            result.primary_code = codes.E_CHAIN_EMPTY
            result.issues.append(
                Diagnostic(
                    code=codes.E_CHAIN_EMPTY,
                    message="connection yielded no reassembled messages",
                    connection_id=connection.connection_id,
                )
            )

        reassembly_code = primary_reassembly_code(reassembly)
        fatal_reassembly_codes = {
            codes.E_MIXUP,
            codes.E_FRAG_GAP,
            codes.E_FRAG_LEN,
            codes.E_FRAG_OVERLAP,
            codes.E_FRAG_FORMAT,
            codes.E_MSG_JSON,
            codes.E_MSG_KIND,
        }
        if reassembly_code in fatal_reassembly_codes:
            result.primary_code = reassembly_code
            result.trust_conclusion = "indeterminate"
            result.status = "error"
        elif result.trust_conclusion == "trusted":
            result.status = "ok"
        else:
            result.status = "error"
    except DeadlineExceeded:
        result.status = "timeout"
        result.trust_conclusion = "indeterminate"
        result.primary_code = codes.E_TIMEOUT
        # Discard partial stage results so the timeout conclusion is stable no
        # matter exactly where the wall-clock deadline landed.
        result.reassembly = ReassemblyResult(
            status="timeout",
            complete=False,
            fragment_count=len(records),
            unique_fragment_count=0,
            expanded_bytes=expanded_bytes,
        )
        result.candidate_chains = []
        result.selected_chain = None
        result.observed_time_ms = None
        result.clock_source = None
        result.revocation_propagation = []
        result.issues = list(connection.input_diagnostics) + [
            Diagnostic(
                code=codes.E_TIMEOUT,
                message="connection exceeded the real wall-clock processing limit",
                connection_id=connection.connection_id,
                detail={"limit_ms": config.timeout_ms},
            )
        ]
    except FragmentLimitExceeded as exc:
        result.status = "error"
        result.trust_conclusion = "indeterminate"
        result.primary_code = codes.E_FRAGS
        result.issues.append(
            Diagnostic(
                code=codes.E_FRAGS,
                message=exc.message,
                connection_id=connection.connection_id,
                detail={
                    "fragment_count": exc.detail.get("fragment_count"),
                    "max_frags": exc.detail.get("max_frags"),
                },
            )
        )
    except ExpandedBudgetExceeded as exc:
        result.status = "error"
        result.trust_conclusion = "indeterminate"
        result.primary_code = codes.E_BUDGET
        result.issues.append(
            Diagnostic(
                code=codes.E_BUDGET,
                message=exc.message,
                connection_id=connection.connection_id,
                detail={
                    "expanded_bytes": exc.detail.get("expanded_bytes"),
                    "max_expanded_bytes": exc.detail.get("max_expanded_bytes"),
                },
            )
        )
    except Exception as exc:  # keep one bad connection from aborting the run
        result.status = "error"
        result.trust_conclusion = "indeterminate"
        result.primary_code = codes.E_INTERNAL
        result.issues.append(
            Diagnostic(
                code=codes.E_INTERNAL,
                message="unexpected error while processing connection",
                connection_id=connection.connection_id,
                detail={"error_type": type(exc).__name__, "error": str(exc)},
            )
        )

    result.elapsed_ms = (time.perf_counter() - started) * 1000.0
    result.compute_digest()
    return result


def _connection_inputs(
    records: Sequence[RawRecord], parse_diagnostics: Sequence[Diagnostic]
) -> Tuple[Dict[str, ConnectionInput], List[Diagnostic]]:
    groups = group_records(records)
    diagnostics_by_conn: Dict[str, List[Diagnostic]] = {}
    global_diagnostics: List[Diagnostic] = []
    for issue in parse_diagnostics:
        if issue.connection_id is not None:
            diagnostics_by_conn.setdefault(issue.connection_id, []).append(issue)
            groups.setdefault(issue.connection_id, [])
        else:
            global_diagnostics.append(issue)
    inputs: Dict[str, ConnectionInput] = {}
    for connection_id in groups:
        inputs[connection_id] = ConnectionInput(
            connection_id=connection_id,
            records=list(groups[connection_id]),
            input_diagnostics=list(diagnostics_by_conn.get(connection_id, [])),
        )
    for connection_id, issues in diagnostics_by_conn.items():
        if connection_id not in inputs:
            inputs[connection_id] = ConnectionInput(
                connection_id=connection_id,
                records=[],
                input_diagnostics=list(issues),
            )
    return inputs, global_diagnostics


def _allocate_global_budget(
    inputs: Dict[str, ConnectionInput], config: AnalysisConfig
) -> Dict[str, Dict[str, Any]]:
    """Deterministically decide which connections exceed the global byte cap."""

    decisions: Dict[str, Dict[str, Any]] = {}
    cumulative = 0
    limit = config.max_total_expanded_bytes
    for connection_id in sorted(inputs):
        connection = inputs[connection_id]
        size = sum(len(r.payload) for r in connection.records)
        would_be = cumulative + size
        if would_be > limit:
            decisions[connection_id] = {
                "code": codes.E_BUDGET,
                "detail": {
                    "global_total_expanded_bytes_limit": limit,
                    "connection_expanded_bytes": size,
                    "global_bytes_before_connection": cumulative,
                },
            }
        cumulative = would_be
    return decisions


def _preflight(
    inputs: Dict[str, ConnectionInput], config: AnalysisConfig
) -> Dict[str, ConnectionResult]:
    decisions = _allocate_global_budget(inputs, config)
    preflight: Dict[str, ConnectionResult] = {}
    for connection_id in sorted(inputs):
        connection = inputs[connection_id]
        frag_count = len(connection.records)
        expanded = sum(len(r.payload) for r in connection.records)
        if frag_count > config.max_frags_per_connection:
            preflight[connection_id] = _preflight_budget_result(
                connection,
                code=codes.E_FRAGS,
                detail={
                    "fragment_count": frag_count,
                    "max_frags_per_connection": config.max_frags_per_connection,
                },
                config=config,
            )
        elif expanded > config.max_expanded_bytes_per_connection:
            preflight[connection_id] = _preflight_budget_result(
                connection,
                code=codes.E_BUDGET,
                detail={
                    "expanded_bytes": expanded,
                    "max_expanded_bytes_per_connection": config.max_expanded_bytes_per_connection,
                },
                config=config,
            )
        elif connection_id in decisions:
            decision = decisions[connection_id]
            preflight[connection_id] = _preflight_budget_result(
                connection,
                code=decision["code"],
                detail=decision["detail"],
                config=config,
            )
    return preflight


def _checkpoint_info_base(
    *,
    path: Optional[Path],
    resume_requested: bool,
    load: Optional[CheckpointLoad],
    trust_change: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "path": str(path) if path is not None else None,
        "resume_requested": resume_requested,
        "resumed": False,
        "recovered_from": load.recovered_from if load else None,
        "half_write_detected": load.half_write_detected if load else False,
        "cleaned_temp_files": list(load.cleaned_temp_files) if load else [],
        "replay_verified_connections": 0,
        "rebuild": bool(load.rebuild) if load else False,
        "rebuild_reasons": list(load.reasons) if load else [],
        "structure_version": codes.CHECKPOINT_STRUCTURE_VERSION,
        "merge_policy_version": codes.TRUST_MERGE_POLICY_VERSION,
        "trust_version_change": trust_change,
    }


def analyze_run(
    *,
    handshake_path: Optional[str | Path] = None,
    handshake_bytes: Optional[bytes] = None,
    v1_path: Optional[str | Path] = None,
    v1_bytes: Optional[bytes] = None,
    v2_shards: Optional[str | Path] = None,
    v2_db: Optional[str | Path] = None,
    output_dir: Optional[str | Path] = None,
    report_path: Optional[str | Path] = None,
    graph_path: Optional[str | Path] = None,
    checkpoint_path: Optional[str | Path] = None,
    config: Optional[AnalysisConfig] = None,
    resume: Optional[bool] = None,
    write_outputs: bool = True,
) -> AnalysisRun:
    config = config or AnalysisConfig()
    if handshake_bytes is None:
        if handshake_path is None:
            raise ValueError("handshake_path or handshake_bytes is required")
        handshake_bytes = Path(handshake_path).read_bytes()
    handshake_source = str(handshake_path) if handshake_path is not None else "<memory>"
    if v1_bytes is None and v1_path is not None:
        v1_bytes = Path(v1_path).read_bytes()
    v1_source = str(v1_path) if v1_path is not None else "<v1>"

    records, parse_diagnostics = parse_handshake_jsonl(handshake_bytes, handshake_source)
    inputs, global_diagnostics = _connection_inputs(records, parse_diagnostics)
    trust_view = load_trust_view(
        v1_bytes=v1_bytes,
        v1_source=v1_source,
        v2_shards_dir=v2_shards,
        v2_db_path=v2_db,
    )
    input_fingerprint, config_fingerprint, fingerprint_material = compute_fingerprints(
        handshake_bytes=handshake_bytes,
        v1_bytes=v1_bytes,
        v2_shards=v2_shards,
        v2_db=v2_db,
        config=config,
    )

    if output_dir is not None:
        out_dir = Path(output_dir)
    elif report_path is not None:
        out_dir = Path(report_path).parent
    else:
        out_dir = None
    checkpoint_target = (
        Path(checkpoint_path)
        if checkpoint_path is not None
        else (out_dir / "checkpoint.json" if out_dir is not None else None)
    )
    report_target = (
        Path(report_path)
        if report_path is not None
        else (out_dir / "report.json" if out_dir is not None else None)
    )
    graph_target = (
        Path(graph_path)
        if graph_path is not None
        else (out_dir / "graph.json" if out_dir is not None else None)
    )
    resume_requested = config.resume if resume is None else bool(resume)

    load: Optional[CheckpointLoad] = None
    if checkpoint_target is not None and checkpoint_target.exists():
        load = load_checkpoint(
            checkpoint_target,
            expected_input_fingerprint=input_fingerprint,
            expected_config_fingerprint=config_fingerprint,
            expected_trust_version=trust_view.version,
        )
    if not resume_requested and load is not None:
        # Metadata is still useful for snapshot-version change detection, but
        # stored completed results are not reused without --resume.
        load.completed = {}
        load.usable = False

    previous_summary = load.previous_trust_summary if load else []
    if load and load.previous_trust_version:
        trust_change = diff_trust_views(previous_summary, trust_view)
        if load.previous_trust_version == trust_view.version and not trust_change["replaced"]:
            trust_change["status"] = "unchanged"
        else:
            trust_change["status"] = "replaced"
            if load.previous_trust_version != trust_view.version and not trust_change["reasons"]:
                trust_change["reasons"] = ["source_version_changed"]
        trust_change["previous_version"] = load.previous_trust_version
    else:
        trust_change = {
            "status": "initial",
            "replaced": False,
            "added": [],
            "removed": [],
            "changed": {},
            "reasons": [],
            "previous_version": None,
        }
    trust_change["current_version"] = trust_view.version

    checkpoint_info = _checkpoint_info_base(
        path=checkpoint_target,
        resume_requested=resume_requested,
        load=load,
        trust_change=trust_change,
    )
    if load is not None:
        checkpoint_info["sequence"] = load.sequence
        checkpoint_info["aggregate_digest"] = load.aggregate_digest
    results: Dict[str, ConnectionResult] = {}
    replay_diagnostics: List[Diagnostic] = []
    preflight = _preflight(inputs, config)

    if resume_requested and load is not None and load.usable:
        replay_ok = True
        for connection_id in sorted(load.completed):
            if connection_id not in inputs:
                replay_ok = False
                replay_diagnostics.append(
                    Diagnostic(
                        code=codes.E_REPLAY_MISMATCH,
                        message="checkpoint contains a connection that is absent from the inputs",
                        connection_id=connection_id,
                    )
                )
                break
            recomputed = preflight.get(connection_id)
            if recomputed is None:
                recomputed = process_connection(inputs[connection_id], trust_view, config)
            if recomputed.conclusion_digest != load.completed[connection_id].conclusion_digest:
                replay_ok = False
                replay_diagnostics.append(
                    Diagnostic(
                        code=codes.E_REPLAY_MISMATCH,
                        message="replayed conclusion differs from the checkpoint; rebuilding from inputs",
                        connection_id=connection_id,
                        detail={
                            "checkpoint_digest": load.completed[connection_id].conclusion_digest,
                            "replay_digest": recomputed.conclusion_digest,
                        },
                    )
                )
                break
        if replay_ok:
            results.update(load.completed)
            checkpoint_info["resumed"] = True
            checkpoint_info["replay_verified_connections"] = len(load.completed)
        else:
            results.clear()
            checkpoint_info["resumed"] = False
            checkpoint_info["rebuild"] = True
            checkpoint_info["rebuild_reasons"] = sorted(
                set(checkpoint_info["rebuild_reasons"]) | {"replay_mismatch"}
            )
            checkpoint_info["replay_mismatch"] = True
    elif resume_requested:
        checkpoint_info["resumed"] = False
        checkpoint_info["rebuild"] = True

    for connection_id, result in preflight.items():
        results.setdefault(connection_id, result)

    pending_ids = [
        connection_id
        for connection_id in sorted(inputs)
        if connection_id not in results
    ]
    sequence = int(load.sequence if load else 0)
    checkpoint_writes = 0

    def write_checkpoint() -> None:
        nonlocal sequence, checkpoint_writes
        if checkpoint_target is None:
            return
        sequence += 1
        checkpoint_writes += 1
        payload = save_checkpoint(
            checkpoint_target,
            input_fingerprint=input_fingerprint,
            config_fingerprint=config_fingerprint,
            trust_view=trust_view,
            completed=results,
            sequence=sequence,
            lock_timeout_ms=config.lock_timeout_ms,
        )
        checkpoint_info["sequence"] = payload["sequence"]
        checkpoint_info["aggregate_digest"] = payload["aggregate_digest"]
        checkpoint_info["completed_count"] = payload["completed_count"]

    # Persist preflight conclusions immediately so a kill after this point still
    # has a complete checkpoint for those connections.
    if preflight and checkpoint_target is not None:
        write_checkpoint()

    if pending_ids:
        executor = ThreadPoolExecutor(max_workers=config.jobs)
        future_map: Dict[Future[ConnectionResult], str] = {
            executor.submit(process_connection, inputs[connection_id], trust_view, config): connection_id
            for connection_id in pending_ids
        }
        try:
            for future in as_completed(future_map):
                connection_id = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = ConnectionResult(
                        connection_id=connection_id,
                        status="error",
                        trust_conclusion="indeterminate",
                        primary_code=codes.E_INTERNAL,
                        issues=[
                            Diagnostic(
                                code=codes.E_INTERNAL,
                                message="worker failed before producing a connection result",
                                connection_id=connection_id,
                                detail={"error_type": type(exc).__name__, "error": str(exc)},
                            )
                        ],
                    )
                    result.compute_digest()
                results[connection_id] = result
                if config.checkpoint_every_connection and checkpoint_target is not None:
                    write_checkpoint()
        except LockConflict:
            for future in future_map:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    checkpoint_info["completed_count"] = len(results)
    checkpoint_info["aggregate_digest"] = digest_value(
        [
            (connection_id, results[connection_id].conclusion_digest)
            for connection_id in sorted(results)
        ]
    )
    if checkpoint_target is not None:
        write_checkpoint()
    checkpoint_info["writes"] = checkpoint_writes

    propagation = compute_propagation(list(results.values()))
    input_info = {
        "handshake_path": str(handshake_path) if handshake_path is not None else None,
        "v1_path": str(v1_path) if v1_path is not None else None,
        "v2_shards": str(v2_shards) if v2_shards is not None else None,
        "v2_db": str(v2_db) if v2_db is not None else None,
        "input_fingerprint": input_fingerprint,
        "config_fingerprint": config_fingerprint,
        "handshake_sha256": sha256_hex(handshake_bytes),
        "record_count": len(records),
        "connection_count": len(inputs),
        "fingerprint_material": fingerprint_material,
    }
    all_global_diagnostics = (
        list(global_diagnostics)
        + list(load.diagnostics if load else [])
        + replay_diagnostics
    )
    report = build_report(
        input_info=input_info,
        trust_view=trust_view,
        results=list(results.values()),
        propagation=propagation,
        config=config,
        checkpoint_info=checkpoint_info,
        input_diagnostics=all_global_diagnostics,
        trust_version_change=trust_change,
    )

    if write_outputs and out_dir is not None:
        package: Dict[str, bytes] = {}
        if graph_target is not None:
            package[str(graph_target)] = (
                json.dumps(report["dependency_graph"], ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8")
        if report_target is not None:
            report_bytes_value = report_bytes(report)
            package[str(report_target)] = report_bytes_value
        if package:
            atomic_write_many(
                package,
                lock_path=out_dir / ".tlsdiag.lock",
                lock_timeout_ms=config.lock_timeout_ms,
                purpose="report",
            )
    return AnalysisRun(
        report=report,
        trust_view=trust_view,
        results=results,
        config=config,
        checkpoint_info=checkpoint_info,
        propagation=propagation,
        input_fingerprint=input_fingerprint,
        config_fingerprint=config_fingerprint,
    )


def run_analysis(**kwargs: Any) -> Dict[str, Any]:
    """Convenience wrapper returning the JSON-ready report dictionary."""

    return analyze_run(**kwargs).report
