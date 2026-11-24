"""Workbench orchestration: reassembly, fuzzing, echo observation, checkpoints."""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointState,
    CheckpointStore,
    rules_basis_fingerprint,
)
from .codes import (
    D_BOUNDARY,
    D_CHUNK_LENGTH,
    D_CONTENT_LENGTH,
    D_DUP_HEADER,
    D_FRAMING,
    D_TE_CL,
    D_TRAILER,
    E_BUDGET,
    E_INPUT,
    E_TIMEOUT,
    E_UNEXPECTED_STATUS,
)
from .echo_server import EchoSchedule, EchoServer
from .models import (
    ConnectionResult,
    MutationResult,
    MutationSpec,
    ParsedMessage,
    Rule,
    RuleHit,
    WorkspaceData,
)
from .mutations import apply_mutation
from .normalize import load_workspace
from .rules import RuleEngine, detect_rule_cycles, merge_rule_view
from .state_machine import build_connection_results, local_interpretation, parse_mutated_request
from .transport import TransportResult, send_and_observe
from .util import (
    BusinessError,
    WorkdirLock,
    atomic_write_json,
    canonical_json,
    sha256_json,
)

REPORT_SCHEMA_VERSION = 1


@dataclass
class RunConfig:
    concurrency: int = 4
    connection_timeout_s: float = 5.0
    max_messages_per_connection: int = 128
    max_inflight_bytes: int = 1024 * 1024
    evaluation_ms: int | None = None
    resume: bool = True
    echo_host: str = "127.0.0.1"
    echo_port: int = 0
    start_echo_server: bool = True
    default_echo_delay_ms: int = 0
    echo_read_timeout_s: float = 5.0
    te_cl_policy: str = "content-length"
    duplicate_cl_policy: str = "last"
    chunk_policy: str = "lenient"
    trailer_policy: str = "ignore"
    echo_faults: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.concurrency, bool) or self.concurrency < 1:
            raise ValueError("concurrency must be a positive integer")
        if self.connection_timeout_s <= 0:
            raise ValueError("connection_timeout_s must be positive")
        if self.max_messages_per_connection < 1:
            raise ValueError("max_messages_per_connection must be positive")
        if self.max_inflight_bytes < 1:
            raise ValueError("max_inflight_bytes must be positive")
        if self.default_echo_delay_ms < 0:
            raise ValueError("default_echo_delay_ms must be non-negative")
        if self.evaluation_ms is not None and self.evaluation_ms < 0:
            raise ValueError("evaluation_ms must be non-negative")
        try:
            address = ipaddress.ip_address(self.echo_host)
        except ValueError as exc:
            raise ValueError("echo_host must be a literal loopback address") from exc
        if not address.is_loopback:
            raise ValueError("echo_host must be a loopback address")

    def to_dict(self) -> dict[str, Any]:
        return {
            "concurrency": self.concurrency,
            "connection_timeout_s": self.connection_timeout_s,
            "max_messages_per_connection": self.max_messages_per_connection,
            "max_inflight_bytes": self.max_inflight_bytes,
            "evaluation_ms": self.evaluation_ms,
            "resume": self.resume,
            "echo_host": self.echo_host,
            "echo_port": self.echo_port,
            "start_echo_server": self.start_echo_server,
            "default_echo_delay_ms": self.default_echo_delay_ms,
            "echo_read_timeout_s": self.echo_read_timeout_s,
            "te_cl_policy": self.te_cl_policy,
            "duplicate_cl_policy": self.duplicate_cl_policy,
            "chunk_policy": self.chunk_policy,
            "trailer_policy": self.trailer_policy,
            "echo_faults": self.echo_faults,
        }


class _ByteLimiter:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self._in_use = 0
        self._condition = threading.Condition()

    def acquire(self, size: int, deadline_ns: int) -> str:
        if size > self.capacity:
            return "BUDGET"
        with self._condition:
            while self._in_use + size > self.capacity:
                remaining = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
                if remaining <= 0:
                    return "TIMEOUT"
                self._condition.wait(timeout=min(remaining, 0.25))
            self._in_use += size
            return "OK"

    def release(self, size: int) -> None:
        with self._condition:
            self._in_use = max(0, self._in_use - size)
            self._condition.notify_all()


class Workbench:
    def __init__(
        self,
        workspace: str | Path,
        workdir: str | Path,
        config: RunConfig | None = None,
    ):
        self.workspace_path = Path(workspace).resolve()
        self.workdir = Path(workdir).resolve()
        self.config = config or RunConfig()

    def run(self) -> dict[str, Any]:
        started_ns = time.monotonic_ns()
        data = load_workspace(self.workspace_path)
        self.workdir.mkdir(parents=True, exist_ok=True)
        lock = WorkdirLock(self.workdir).acquire()
        server: EchoServer | None = None
        try:
            store = CheckpointStore(self.workdir)
            observed_at = (
                self.config.evaluation_ms
                if self.config.evaluation_ms is not None
                else data.observed_clock_ms
            )
            active, audit = merge_rule_view(data.rules, observed_at)
            detect_rule_cycles(data.rules)
            rule_engine = RuleEngine(active.values(), audit_rules=data.rules)
            rules_basis = rules_basis_fingerprint(
                list(data.rules),
                observed_clock_ms=data.observed_clock_ms,
            )
            checkpoint_info: dict[str, Any] = {
                "used": False,
                "rebuilt": True,
                "reason": "RESUME_DISABLED",
                "residue": [],
                "generation": 0,
            }
            restored: CheckpointState | None = None
            if self.config.resume:
                loaded = store.load(
                    expected_rules_basis=rules_basis,
                    lock_held=True,
                )
                checkpoint_info.update(
                    {
                        "rebuilt": loaded.rebuilt,
                        "reason": loaded.reason,
                        "residue": loaded.residue,
                        "generation": loaded.generation,
                    }
                )
                restored = loaded.state

            connection_results = build_connection_results(
                data.flows,
                rule_engine=rule_engine,
            )
            templates = {
                (message.connection_id, message.request_index): message
                for result in connection_results.values()
                for message in result.messages
            }

            if restored is not None and not self._checkpoint_replays(
                restored,
                connection_results,
                rule_engine,
                templates,
                data,
            ):
                restored = None
                checkpoint_info.update(
                    {
                        "rebuilt": True,
                        "reason": "REPLAY_VERIFICATION_FAILED",
                    }
                )

            if restored is not None:
                checkpoint_info["used"] = True
                connection_reports = dict(restored.connection_results)
                mutation_reports = dict(restored.mutation_results)
                completed = set(restored.completed_connections)
            else:
                connection_reports = {}
                mutation_reports = {}
                completed = set()

            if self.config.start_echo_server:
                server = EchoServer(
                    self.config.echo_host,
                    self.config.echo_port,
                    te_cl_policy=self.config.te_cl_policy,
                    duplicate_cl_policy=self.config.duplicate_cl_policy,
                    chunk_policy=self.config.chunk_policy,
                    trailer_policy=self.config.trailer_policy,
                    default_delay_ms=self.config.default_echo_delay_ms,
                    read_timeout_s=self.config.echo_read_timeout_s,
                ).start()
                echo_host = server.host
                echo_port = server.port
            else:
                echo_host = self.config.echo_host
                echo_port = self.config.echo_port
                if echo_port <= 0:
                    raise BusinessError(
                        E_INPUT,
                        "an external echo mode requires a positive echo_port",
                        {"echo_host": echo_host},
                    )

            limiter = _ByteLimiter(self.config.max_inflight_bytes)
            grouped = self._mutation_groups(data.mutations)
            pending_groups = []
            for connection_id in sorted(grouped):
                specs = grouped[connection_id]
                if connection_id in completed and all(
                    spec.mutation_id in mutation_reports for spec in specs
                ):
                    continue
                pending_groups.append((connection_id, specs))
                completed.discard(connection_id)
                for spec in specs:
                    mutation_reports.pop(spec.mutation_id, None)
                connection_reports.pop(connection_id, None)

            state = CheckpointState(
                schema_version=CHECKPOINT_SCHEMA_VERSION,
                rules_basis=rules_basis,
                completed_connections=sorted(completed),
                connection_results=connection_reports,
                mutation_results=mutation_reports,
                aggregate=self._aggregate(connection_reports, mutation_reports),
            )

            if pending_groups:
                with ThreadPoolExecutor(
                    max_workers=self.config.concurrency,
                    thread_name_prefix="mutation-conn",
                ) as executor:
                    future_map: dict[Future[list[MutationResult]], str] = {}
                    for connection_id, specs in pending_groups:
                        conn_result = connection_results[connection_id]
                        future = executor.submit(
                            self._execute_connection_group,
                            connection_id,
                            specs,
                            templates,
                            conn_result,
                            rule_engine,
                            limiter,
                            echo_host,
                            echo_port,
                            server,
                        )
                        future_map[future] = connection_id
                    for future in as_completed(future_map):
                        connection_id = future_map[future]
                        try:
                            group_results = future.result()
                        except Exception as exc:
                            group_results = [
                                self._error_result(
                                    spec,
                                    connection_id,
                                    code=E_INPUT,
                                    message=f"connection group failed: {exc}",
                                )
                                for spec in grouped[connection_id]
                            ]
                        for result in group_results:
                            mutation_reports[result.mutation_id] = result.to_dict()
                        connection_reports[connection_id] = connection_results[
                            connection_id
                        ].to_dict()
                        completed.add(connection_id)
                        state.completed_connections = sorted(completed)
                        state.connection_results = connection_reports
                        state.mutation_results = mutation_reports
                        state.aggregate = self._aggregate(
                            connection_reports,
                            mutation_reports,
                        )
                        state.complete = True
                        checkpoint_info["generation"] = store.save(
                            state,
                            lock_held=True,
                        )

            for connection_id in sorted(connection_results):
                if connection_id not in completed and connection_id not in grouped:
                    connection_reports[connection_id] = connection_results[
                        connection_id
                    ].to_dict()
                    completed.add(connection_id)

            state.completed_connections = sorted(completed)
            state.connection_results = connection_reports
            state.mutation_results = mutation_reports
            state.aggregate = self._aggregate(connection_reports, mutation_reports)
            state.complete = True
            checkpoint_info["generation"] = store.save(state, lock_held=True)

            report = self._build_report(
                data=data,
                observed_at=observed_at,
                active=active,
                audit=audit,
                connection_reports=connection_reports,
                mutation_reports=mutation_reports,
                checkpoint_info=checkpoint_info,
                echo_address={"host": echo_host, "port": echo_port},
                started_ns=started_ns,
            )
            atomic_write_json(self.workdir / "diagnostic_report.json", report)
            return report
        finally:
            if server is not None:
                server.stop()
            lock.release()

    def _mutation_groups(
        self,
        mutations: Iterable[MutationSpec],
    ) -> dict[str, list[MutationSpec]]:
        groups: dict[str, list[MutationSpec]] = {}
        for spec in mutations:
            groups.setdefault(spec.connection_id, []).append(spec)
        for specs in groups.values():
            specs.sort(
                key=lambda item: (
                    item.mutation_id,
                    canonical_json(item.stable_payload),
                )
            )
        return groups

    def _execute_connection_group(
        self,
        connection_id: str,
        specs: list[MutationSpec],
        templates: dict[tuple[str, int], ParsedMessage],
        connection_result: ConnectionResult,
        rule_engine: RuleEngine,
        limiter: _ByteLimiter,
        echo_host: str,
        echo_port: int,
        server: EchoServer | None,
    ) -> list[MutationResult]:
        started_ns = time.monotonic_ns()
        deadline_ns = started_ns + int(self.config.connection_timeout_s * 1_000_000_000)
        results: list[MutationResult] = []
        terminated_error: dict[str, Any] | None = None
        message_budget_exhausted = (
            len(connection_result.messages)
            > self.config.max_messages_per_connection
        )
        connection_error = (
            connection_result.errors[0] if connection_result.errors else None
        )

        for sequence, spec in enumerate(specs):
            template = templates.get((connection_id, spec.request_index))
            raw: bytes | None = None
            local_messages: list[ParsedMessage] = []
            local_interp: dict[str, Any] = local_interpretation([])
            hits: list[RuleHit] = []
            suppressed: list[dict[str, Any]] = []
            candidates: set[str] = set()
            if template is not None:
                try:
                    raw = apply_mutation(template, spec)
                except (ValueError, BusinessError) as exc:
                    raw = None
                    mutate_error: dict[str, Any] = (
                        exc.to_dict()
                        if isinstance(exc, BusinessError)
                        else {"code": E_INPUT, "message": str(exc)}
                    )
                else:
                    mutate_error = None
                if raw is not None:
                    (
                        local_messages,
                        _transitions,
                        hits,
                        suppressed,
                        candidates,
                        _parse_errors,
                    ) = parse_mutated_request(
                        raw,
                        connection_id=connection_id,
                        rule_engine=rule_engine,
                    )
                    local_interp = local_interpretation(local_messages)
            else:
                mutate_error = {
                    "code": E_INPUT,
                    "message": "mutation template request was not found",
                    "connection_id": connection_id,
                    "request_index": spec.request_index,
                }

            provisional = _provisional_verdict(hits, local_interp)
            if connection_error is not None:
                results.append(
                    self._error_result(
                        spec,
                        connection_id,
                        code=str(connection_error.get("code", E_INPUT)),
                        message=str(connection_error.get("message", "connection error")),
                        sequence=sequence,
                        local_interp=local_interp,
                        hits=hits,
                        suppressed=suppressed,
                        candidates=candidates,
                        provisional=provisional,
                    )
                )
                continue
            if mutate_error is not None:
                results.append(
                    self._error_result(
                        spec,
                        connection_id,
                        code=str(mutate_error.get("code", E_INPUT)),
                        message=str(mutate_error.get("message", "mutation error")),
                        sequence=sequence,
                        local_interp=local_interp,
                        hits=hits,
                        suppressed=suppressed,
                        candidates=candidates,
                        provisional=provisional,
                    )
                )
                continue
            if terminated_error is not None:
                results.append(
                    self._error_result(
                        spec,
                        connection_id,
                        code=str(terminated_error["code"]),
                        message="connection terminated after timeout; request was not sent",
                        sequence=sequence,
                        local_interp=local_interp,
                        hits=hits,
                        suppressed=suppressed,
                        candidates=candidates,
                        provisional=provisional,
                    )
                )
                continue
            if message_budget_exhausted or sequence >= self.config.max_messages_per_connection:
                results.append(
                    self._error_result(
                        spec,
                        connection_id,
                        code=E_BUDGET,
                        message="connection message budget exhausted",
                        sequence=sequence,
                        local_interp=local_interp,
                        hits=hits,
                        suppressed=suppressed,
                        candidates=candidates,
                        provisional=provisional,
                    )
                )
                continue
            remaining = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
            if remaining <= 0:
                terminated_error = {
                    "code": E_TIMEOUT,
                    "message": "connection wall-clock deadline expired",
                }
                results.append(
                    self._error_result(
                        spec,
                        connection_id,
                        code=E_TIMEOUT,
                        message="connection wall-clock deadline expired before send",
                        sequence=sequence,
                        local_interp=local_interp,
                        hits=hits,
                        suppressed=suppressed,
                        candidates=candidates,
                        provisional=provisional,
                    )
                )
                continue
            assert raw is not None
            acquire = limiter.acquire(len(raw), deadline_ns)
            if acquire == "BUDGET":
                results.append(
                    self._error_result(
                        spec,
                        connection_id,
                        code=E_BUDGET,
                        message="request exceeds the in-flight byte budget",
                        sequence=sequence,
                        local_interp=local_interp,
                        hits=hits,
                        suppressed=suppressed,
                        candidates=candidates,
                        provisional=provisional,
                    )
                )
                continue
            if acquire == "TIMEOUT":
                terminated_error = {
                    "code": E_TIMEOUT,
                    "message": "timed out waiting for in-flight byte budget",
                }
                results.append(
                    self._error_result(
                        spec,
                        connection_id,
                        code=E_TIMEOUT,
                        message=terminated_error["message"],
                        sequence=sequence,
                        local_interp=local_interp,
                        hits=hits,
                        suppressed=suppressed,
                        candidates=candidates,
                        provisional=provisional,
                    )
                )
                continue

            fault = self.config.echo_faults.get(spec.mutation_id, {})
            delay_ms = int(
                fault.get(
                    "delay_ms",
                    (
                        spec.delay_ms
                        if spec.delay_ms
                        else self.config.default_echo_delay_ms
                    ),
                )
            )
            if server is not None:
                def register_schedule(source_port: int) -> None:
                    assert server is not None
                    server.register(
                        raw,
                        delay_ms=delay_ms,
                        close_early=bool(fault.get("close_early", False)),
                        status=int(fault.get("status", 200)),
                        source_port=source_port,
                    )
            else:
                register_schedule = None
            try:
                transport = send_and_observe(
                    raw,
                    host=echo_host,
                    port=echo_port,
                    deadline_ns=deadline_ns,
                    on_bound=register_schedule,
                )
            finally:
                limiter.release(len(raw))

            remote_interp = None
            echo_sha = None
            if transport.echo is not None:
                remote_interp = transport.echo.get("interpretation")
                echo_sha = transport.echo.get("echo_sha256")
                if echo_sha != hashlib.sha256(raw).hexdigest():
                    transport.error = {
                        "code": "E_UNEXPECTED_RESPONSE",
                        "message": "echo service did not echo the submitted bytes",
                    }
            discrepancies = compare_interpretations(local_interp, remote_interp)
            final = _final_verdict(
                provisional=provisional,
                discrepancies=discrepancies,
                transport_error=transport.error,
            )
            overturn = int(
                transport.error is None
                and _risk_class(provisional) != _risk_class(final)
            )
            if transport.error is not None:
                status = "ERROR"
            elif discrepancies:
                status = "DIVERGENT"
            else:
                status = "OK"
            result = MutationResult(
                mutation_id=spec.mutation_id,
                connection_id=connection_id,
                stable_key=_stable_key(spec),
                status=status,
                provisional_verdict=provisional,
                final_verdict=final,
                overturn_count=overturn,
                discrepancies=discrepancies,
                local_interpretation=local_interp,
                remote_interpretation=remote_interp,
                rule_hits=hits,
                suppressed_hits=suppressed,
                candidate_rules=tuple(sorted(candidates)),
                error=transport.error,
                echo_sha256=echo_sha,
                echo_bytes_seen=transport.response_bytes_seen,
                submit_monotonic_ns=transport.submit_monotonic_ns,
                visible_monotonic_ns=transport.visible_monotonic_ns,
                latency_ms=transport.latency_ms,
                delay_profile_ms=delay_ms,
                connection_sequence=sequence,
                sent=True,
                peer_outcome=transport.peer_outcome,
            )
            results.append(result)
            if transport.error is not None and transport.error.get("code") == E_TIMEOUT:
                terminated_error = transport.error
        return results

    def _error_result(
        self,
        spec: MutationSpec,
        connection_id: str,
        *,
        code: str,
        message: str,
        sequence: int = 0,
        local_interp: dict[str, Any] | None = None,
        hits: list[RuleHit] | None = None,
        suppressed: list[dict[str, Any]] | None = None,
        candidates: set[str] | None = None,
        provisional: str = "ERROR",
    ) -> MutationResult:
        now = time.monotonic_ns()
        return MutationResult(
            mutation_id=spec.mutation_id,
            connection_id=connection_id,
            stable_key=_stable_key(spec),
            status="ERROR",
            provisional_verdict=provisional,
            final_verdict="ERROR",
            overturn_count=0,
            discrepancies=(),
            local_interpretation=local_interp or local_interpretation([]),
            remote_interpretation=None,
            rule_hits=hits or [],
            suppressed_hits=suppressed or [],
            candidate_rules=tuple(sorted(candidates or ())),
            error={"code": code, "message": message},
            echo_sha256=None,
            echo_bytes_seen=0,
            submit_monotonic_ns=now,
            visible_monotonic_ns=now,
            latency_ms=0.0,
            delay_profile_ms=spec.delay_ms,
            connection_sequence=sequence,
            sent=False,
            peer_outcome="NOT_SENT",
        )

    def _checkpoint_replays(
        self,
        state: CheckpointState,
        connection_results: dict[str, ConnectionResult],
        rule_engine: RuleEngine,
        templates: dict[tuple[str, int], ParsedMessage],
        data: WorkspaceData,
    ) -> bool:
        for connection_id in state.completed_connections:
            stored = state.connection_results.get(connection_id)
            current = connection_results.get(connection_id)
            if stored is None or current is None:
                return False
            if canonical_json(stored) != canonical_json(current.to_dict()):
                return False
        for mutation_id, stored in state.mutation_results.items():
            if not isinstance(stored, dict):
                return False
            spec = next(
                (item for item in data.mutations if item.mutation_id == mutation_id),
                None,
            )
            if spec is None:
                return False
            template = templates.get((spec.connection_id, spec.request_index))
            if template is None:
                continue
            try:
                raw = apply_mutation(template, spec)
            except (ValueError, BusinessError):
                continue
            if stored.get("echo_sha256") is not None and stored.get(
                "echo_sha256"
            ) != hashlib.sha256(raw).hexdigest():
                return False
            (
                messages,
                _transitions,
                hits,
                suppressed,
                candidates,
                _errors,
            ) = parse_mutated_request(
                raw,
                connection_id=spec.connection_id,
                rule_engine=rule_engine,
            )
            local = local_interpretation(messages)
            expected_local = {
                "local_interpretation": local,
                "rule_hits": [hit.to_dict() for hit in hits],
                "suppressed_hits": suppressed,
                "candidate_rules": sorted(candidates),
            }
            actual_local = {
                "local_interpretation": stored.get("local_interpretation"),
                "rule_hits": stored.get("rule_hits"),
                "suppressed_hits": stored.get("suppressed_hits"),
                "candidate_rules": stored.get("candidate_rules", []),
            }
            if canonical_json(expected_local) != canonical_json(actual_local):
                return False
        return True

    def _aggregate(
        self,
        connection_reports: dict[str, Any],
        mutation_reports: dict[str, Any],
    ) -> dict[str, Any]:
        values = [mutation_reports[key] for key in sorted(mutation_reports)]
        verdicts: dict[str, int] = {}
        discrepancies: dict[str, int] = {}
        errors: dict[str, int] = {}
        overturned = 0
        for item in values:
            verdict = str(item.get("final_verdict", "UNKNOWN"))
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
            for code in item.get("discrepancies", []):
                discrepancies[code] = discrepancies.get(code, 0) + 1
            error = item.get("error")
            if error:
                code = str(error.get("code", "UNKNOWN"))
                errors[code] = errors.get(code, 0) + 1
            overturned += int(item.get("overturn_count", 0))
        return {
            "completed_connections": len(connection_reports),
            "mutation_count": len(values),
            "verdicts": dict(sorted(verdicts.items())),
            "discrepancies": dict(sorted(discrepancies.items())),
            "errors": dict(sorted(errors.items())),
            "overturn_count": overturned,
        }

    def _build_report(
        self,
        *,
        data: WorkspaceData,
        observed_at: int,
        active: dict[str, Rule],
        audit: list[dict[str, Any]],
        connection_reports: dict[str, Any],
        mutation_reports: dict[str, Any],
        checkpoint_info: dict[str, Any],
        echo_address: dict[str, Any],
        started_ns: int,
    ) -> dict[str, Any]:
        mutation_values = [
            mutation_reports[key] for key in sorted(mutation_reports)
        ]
        mutation_values.sort(
            key=lambda item: (
                item.get("stable_key", ""),
                item.get("mutation_id", ""),
            )
        )
        deterministic = {
            "mutation_results": [
                _deterministic_from_stored(item) for item in mutation_values
            ],
            "aggregate": self._aggregate(
                connection_reports,
                mutation_reports,
            ),
        }
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "run": {
                "sample_seed": data.sample_seed,
                "workspace": data.root,
                "observed_clock_ms": data.observed_clock_ms,
                "evaluation_ms": observed_at,
                "elapsed_wall_ms": (time.monotonic_ns() - started_ns)
                / 1_000_000.0,
                "echo_address": echo_address,
            },
            "config": self.config.to_dict(),
            "inputs": data.source_files,
            "rejections": [item.to_dict() for item in data.rejections],
            "rules": {
                "active": [active[key].to_dict() for key in sorted(active)],
                "merge_audit": audit,
            },
            "connections": {
                key: connection_reports[key] for key in sorted(connection_reports)
            },
            "mutations": mutation_values,
            "deterministic_summary": deterministic,
            "checkpoint": checkpoint_info,
            "validation": {
                "rejection_count": len(data.rejections),
                "completed_connections": len(connection_reports),
                "mutation_count": len(mutation_values),
                "all_latencies_wall_clock": all(
                    _latency_is_self_consistent(item) for item in mutation_values
                ),
            },
        }


def _stable_key(spec: MutationSpec, payload: bytes | None = None) -> str:
    digest = sha256_json(spec.stable_payload)
    _ = payload
    return f"{spec.mutation_id}:{digest[:16]}"


def _provisional_verdict(
    hits: list[RuleHit],
    interpretation: dict[str, Any],
) -> str:
    if any(hit.action == "REJECT" for hit in hits):
        return "BLOCKED"
    if hits or interpretation.get("anomalies"):
        return "SUSPECTED"
    if not interpretation.get("complete", True):
        return "SUSPECTED"
    return "CLEAN"


def _final_verdict(
    *,
    provisional: str,
    discrepancies: tuple[str, ...],
    transport_error: dict[str, Any] | None,
) -> str:
    if transport_error is not None:
        return "ERROR"
    if discrepancies:
        return "SMUGGLED"
    if provisional == "BLOCKED":
        return "BLOCKED"
    return "CLEAN"


def _risk_class(verdict: str) -> str:
    if verdict in {"BLOCKED", "SUSPECTED", "SMUGGLED"}:
        return "RISK"
    if verdict == "ERROR":
        return "ERROR"
    return "CLEAN"


def _coexisting_te_cl(interpretation: dict[str, Any] | None) -> bool:
    if not interpretation:
        return False
    return bool(interpretation.get("content_length_values")) and bool(
        interpretation.get("transfer_encoding")
    )


def compare_interpretations(
    local: dict[str, Any] | None,
    remote: dict[str, Any] | None,
) -> tuple[str, ...]:
    if local is None or remote is None:
        return ()
    codes: set[str] = set()
    if _coexisting_te_cl(local) and _coexisting_te_cl(remote):
        local_framing = str(local.get("framing", "")).upper()
        remote_framing = str(remote.get("framing", "")).upper()
        if local_framing != remote_framing:
            codes.add(D_TE_CL)
    if str(local.get("framing", "")).upper() != str(
        remote.get("framing", "")
    ).upper():
        codes.add(D_FRAMING)
    if local.get("content_length") != remote.get("content_length"):
        codes.add(D_CONTENT_LENGTH)
    if int(local.get("body_length", 0)) != int(remote.get("body_length", 0)):
        codes.add(D_CONTENT_LENGTH)

    local_dup = local.get("duplicate_headers") or {}
    remote_dup = remote.get("duplicate_headers") or {}
    if local_dup != remote_dup:
        codes.add(D_DUP_HEADER)
    elif local_dup and (
        local.get("content_length") != remote.get("content_length")
        or int(local.get("body_length", 0)) != int(remote.get("body_length", 0))
    ):
        codes.add(D_DUP_HEADER)

    local_chunks = [
        item.get("size") for item in (local.get("chunks") or [])
    ]
    remote_chunks = [
        item.get("size") for item in (remote.get("chunks") or [])
    ]
    if local_chunks != remote_chunks:
        codes.add(D_CHUNK_LENGTH)
    local_chunk_anomalies = sorted(
        item
        for item in (local.get("anomalies") or [])
        if "chunk" in item.casefold()
    )
    remote_chunk_anomalies = sorted(
        item
        for item in (remote.get("anomalies") or [])
        if "chunk" in item.casefold()
    )
    if local_chunk_anomalies != remote_chunk_anomalies:
        codes.add(D_CHUNK_LENGTH)

    if _normalize_trailers(local.get("trailers")) != _normalize_trailers(
        remote.get("trailers")
    ):
        codes.add(D_TRAILER)

    local_boundary = local.get("request_boundary") or [0, 0]
    remote_boundary = remote.get("request_boundary") or [0, 0]
    if int(local.get("next_boundary", local_boundary[-1])) != int(
        remote.get("next_boundary", remote_boundary[-1])
    ):
        codes.add(D_BOUNDARY)
    if local.get("body_sha256") != remote.get("body_sha256"):
        codes.add(D_BOUNDARY)
    if int(remote.get("remaining_bytes", 0)) > 0 and int(
        local.get("request_count", 1)
    ) > 1:
        codes.add(D_BOUNDARY)
    return tuple(sorted(codes))


def _normalize_trailers(value: Any) -> list[tuple[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[tuple[str, str]] = []
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            result.append((str(item[0]).casefold(), str(item[1])))
    return result


def _deterministic_from_stored(item: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "mutation_id",
        "connection_id",
        "stable_key",
        "status",
        "provisional_verdict",
        "final_verdict",
        "overturn_count",
        "discrepancies",
        "local_interpretation",
        "remote_interpretation",
        "rule_hits",
        "suppressed_hits",
        "candidate_rules",
        "error",
        "echo_sha256",
        "echo_bytes_seen",
        "delay_observation",
    )
    return {key: item.get(key) for key in keys}


def _latency_is_self_consistent(item: dict[str, Any]) -> bool:
    if not item.get("sent"):
        return True
    submit = int(item.get("submit_monotonic_ns", 0))
    visible = int(item.get("visible_monotonic_ns", 0))
    measured = float(item.get("latency_ms", -1))
    expected = (visible - submit) / 1_000_000.0
    return visible >= submit and abs(measured - expected) < 0.001
