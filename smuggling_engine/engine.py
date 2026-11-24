"""Concurrent, budgeted decision pipeline and incremental invalidation replay."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable

from .closure import (
    ClosureResult,
    attach_affected_connections,
    compute_invalid_closure,
)
from .checkpoint import CheckpointManager
from .constants import (
    ANALYSIS_POLICY_VERSION,
    FINDING_PRIORITY,
    MERGE_POLICY_VERSION,
    REPORT_SCHEMA_VERSION,
)
from .http_analysis import analyze_request_stream
from .models import (
    ConnectionReport,
    EngineReport,
    Finding,
    Rejection,
    RuleHit,
    RuleView,
)
from .parsing import ConnectionGroup, group_connections, parse_messages
from .rules import canonical_json, load_rule_view


@dataclass(frozen=True)
class EngineConfig:
    concurrency: int = 4
    per_connection_timeout_ms: int | None = 5000
    max_messages_per_connection: int | None = 10000
    max_bytes_per_connection: int | None = 16 * 1024 * 1024
    first_sequence: int = 1
    max_requests_per_connection: int = 10000

    def validate(self) -> None:
        if isinstance(self.concurrency, bool) or not isinstance(self.concurrency, int):
            raise ValueError("concurrency must be an integer")
        if self.concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if self.per_connection_timeout_ms is not None:
            if (
                isinstance(self.per_connection_timeout_ms, bool)
                or not isinstance(self.per_connection_timeout_ms, int)
                or self.per_connection_timeout_ms < 0
            ):
                raise ValueError("per_connection_timeout_ms must be null or >= 0")
        for name, value in (
            ("max_messages_per_connection", self.max_messages_per_connection),
            ("max_bytes_per_connection", self.max_bytes_per_connection),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be null or >= 0")
        if self.first_sequence < 1:
            raise ValueError("first_sequence must be >= 1")
        if self.max_requests_per_connection < 1:
            raise ValueError("max_requests_per_connection must be >= 1")

    def fingerprint(self) -> str:
        return hashlib.sha256(
            canonical_json(
                {
                    **asdict(self),
                    "analysis_policy_version": ANALYSIS_POLICY_VERSION,
                }
            ).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "analysis_policy_version": ANALYSIS_POLICY_VERSION,
            "fingerprint": self.fingerprint(),
        }


class BudgetStop(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(f"{code}: {message}")

    def error(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass(frozen=True)
class _Decision:
    hits: list[RuleHit]
    selected_rule: str | None
    selected_generation: int | None
    selected_finding_code: str | None
    business_code: str | None
    verdict: str


def _match_pattern(pattern: str | None, finding: Finding) -> bool:
    if not pattern:
        return False
    normalized = " ".join(pattern.casefold().split())
    if not normalized:
        return False
    candidates = {
        finding.code.casefold(),
        finding.code.replace("_", "-").casefold(),
        *finding.match_tokens,
    }
    details_text = json.dumps(finding.details, ensure_ascii=False, sort_keys=True).casefold()
    for token in candidates:
        normalized_token = " ".join(token.casefold().split())
        if not normalized_token:
            continue
        if normalized == normalized_token:
            return True
        if normalized in normalized_token:
            return True
        if len(normalized) >= 3 and normalized in details_text:
            return True
    return False


def _evaluate_findings(
    connection_id: str,
    findings: list[Finding],
    view: RuleView,
    invalid_rules: set[str],
) -> _Decision:
    raw_hits: list[tuple[RuleHit, Finding]] = []
    for record in view.candidates_for_match():
        if record.direction not in ("request", "both"):
            continue
        for finding in findings:
            if not _match_pattern(record.pattern, finding):
                continue
            entity = view.entities.get(record.canonical_name)
            record_is_winner = entity is not None and entity.winner == record
            valid = (
                record_is_winner
                and entity.active
                and record.canonical_name not in invalid_rules
            )
            hit = RuleHit(
                canonical_name=record.canonical_name,
                generation=record.generation,
                effective_at_ms=record.effective_at_ms,
                direction=record.direction,
                source=record.source,
                source_index=record.source_index,
                pattern=record.pattern or "",
                finding_code=finding.code,
                byte_start=finding.byte_start,
                byte_end=finding.byte_end,
                byte_offset=finding.byte_offset,
                record_active=record.active_candidate,
                record_valid=valid,
                selected=False,
            )
            raw_hits.append((hit, finding))

    # Preserve all evidence, including shadowed historical records and invalid
    # winners.  The selected decision can only come from a valid winner.
    unique: dict[tuple[Any, ...], tuple[RuleHit, Finding]] = {}
    for hit, finding in raw_hits:
        key = (
            hit.canonical_name,
            hit.generation,
            hit.effective_at_ms,
            hit.source,
            hit.source_index,
            hit.finding_code,
            hit.byte_offset,
        )
        unique.setdefault(key, (hit, finding))
    raw_hits = list(unique.values())

    selected_name: str | None = None
    selected_generation: int | None = None
    selected_code: str | None = None
    selected_key = None
    for hit, _ in raw_hits:
        if not hit.record_valid:
            continue
        key = (
            -hit.generation,
            -hit.effective_at_ms,
            hit.canonical_name,
            FINDING_PRIORITY.get(hit.finding_code, 99),
            hit.byte_offset,
            hit.source,
            hit.source_index,
        )
        if selected_key is None or key < selected_key:
            selected_key = key
            selected_name = hit.canonical_name
            selected_generation = hit.generation
            selected_code = hit.finding_code

    hits: list[RuleHit] = []
    selected_marked = False
    for hit, _ in raw_hits:
        should_mark = (
            not selected_marked
            and hit.record_valid
            and hit.canonical_name == selected_name
            and hit.generation == selected_generation
            and hit.finding_code == selected_code
        )
        if should_mark:
            selected_marked = True
        hits.append(replace(hit, selected=should_mark))
    hits.sort(
        key=lambda hit: (
            -hit.generation,
            -hit.effective_at_ms,
            hit.canonical_name,
            FINDING_PRIORITY.get(hit.finding_code, 99),
            hit.byte_offset,
            hit.source,
            hit.source_index,
        )
    )

    business_code: str | None = None
    verdict = "CLEAN"
    if findings:
        effective_findings = [
            finding
            for finding in findings
            if finding.code
            in {
                "SMUG_CL_TE",
                "SMUG_DUP_HEADER",
                "SMUG_CHUNK_HEX",
                "SMUG_TRAILING_BYTES",
            }
        ]
        pool = effective_findings or findings
        best_finding = min(
            pool,
            key=lambda finding: (
                FINDING_PRIORITY.get(finding.code, 99),
                finding.byte_offset,
                finding.code,
            ),
        )
        business_code = selected_code or best_finding.code
        verdict = "SMUGGLING_DETECTED" if selected_name else "DETECTED_UNRULED"
    return _Decision(
        hits=hits,
        selected_rule=selected_name,
        selected_generation=selected_generation,
        selected_finding_code=selected_code,
        business_code=business_code,
        verdict=verdict,
    )


def _hit_signature(hits: Iterable[RuleHit]) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        sorted(
            (
                hit.canonical_name,
                hit.generation,
                hit.effective_at_ms,
                hit.pattern,
                hit.finding_code,
                hit.byte_start,
                hit.byte_end,
                hit.byte_offset,
                hit.record_active,
                hit.record_valid,
                hit.selected,
            )
            for hit in hits
        )
    )


def connection_report_from_dict(value: dict[str, Any]) -> ConnectionReport:
    findings = [
        Finding(
            code=item["code"],
            connection_id=item["connection_id"],
            message_ordinal=int(item["message_ordinal"]),
            byte_start=int(item["byte_range"]["start"]),
            byte_end=int(item["byte_range"]["end"]),
            byte_offset=int(item["byte_offset"]),
            details=dict(item.get("details", {})),
            match_tokens=tuple(item.get("match_tokens", ())),
        )
        for item in value.get("findings", [])
    ]
    hits = [
        RuleHit(
            canonical_name=item["canonical_name"],
            generation=int(item["generation"]),
            effective_at_ms=int(item["effective_at_ms"]),
            direction=item["direction"],
            source=item["source"],
            source_index=int(item["source_index"]),
            pattern=item["pattern"],
            finding_code=item["finding_code"],
            byte_start=int(
                item.get("byte_range", {}).get("start", item.get("byte_offset", 0))
            ),
            byte_end=int(
                item.get("byte_range", {}).get(
                    "end", item.get("byte_offset", 0)
                )
            ),
            byte_offset=int(item["byte_offset"]),
            record_active=bool(item["record_active"]),
            record_valid=bool(item["record_valid"]),
            selected=bool(item["selected"]),
        )
        for item in value.get("hits", [])
    ]
    return ConnectionReport(
        connection_id=value["connection_id"],
        status=value["status"],
        verdict=value["verdict"],
        business_code=value.get("business_code"),
        errors=list(value.get("errors", [])),
        streams=dict(value.get("streams", {})),
        findings=findings,
        hits=hits,
        selected_rule=value.get("selected_rule"),
        selected_generation=value.get("selected_generation"),
        selected_finding_code=value.get("selected_finding_code"),
        invalidated_hits=tuple(value.get("invalidated_hits", ())),
        affected_by_invalid_rules=bool(
            value.get("affected_by_invalid_rules", False)
        ),
        terminated=bool(value.get("terminated", False)),
        termination_reason=value.get("termination_reason"),
    )


def group_fingerprint(group: ConnectionGroup) -> str:
    material = []
    for message in sorted(
        group.messages, key=lambda item: (item.direction, item.sequence)
    ):
        material.append(
            {
                "connection_id": message.connection_id,
                "direction": message.direction,
                "sequence": message.sequence,
                "timestamp_ms": message.timestamp_ms,
                "payload_hex": message.payload.hex(),
                "sync_marker": message.sync_marker,
                "duplicate_count": message.duplicate_count,
                "duplicate_conflicts": message.duplicate_conflicts,
                "duplicate_bytes": message.duplicate_bytes,
            }
        )
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


def _timeout_checker(start: float, timeout_ms: int | None) -> Callable[[], None]:
    def check() -> None:
        if timeout_ms is None:
            return
        elapsed_ms = (time.monotonic() - start) * 1000.0
        if timeout_ms == 0 or elapsed_ms > timeout_ms:
            raise BudgetStop(
                "E_TIMEOUT",
                "per-connection wall-clock deadline exceeded",
                {
                    "limit_ms": timeout_ms,
                    "observed_ms": round(elapsed_ms, 3),
                },
            )

    return check


def _streams_report(group: ConnectionGroup) -> dict[str, Any]:
    return {
        "message_count": group.message_count,
        "delivered_message_count": group.delivered_message_count,
        "total_bytes": group.total_bytes,
        "delivered_bytes": group.delivered_bytes,
        "duplicate_count": group.duplicate_count,
        "duplicate_conflicts": group.duplicate_conflicts,
        "directions": {
            direction: stream.report_dict()
            for direction, stream in sorted(group.streams.items())
        },
    }


def _error_report(
    group: ConnectionGroup,
    code: str,
    errors: list[dict[str, Any]],
    *,
    terminated: bool,
    findings: list[Finding] | None = None,
) -> ConnectionReport:
    return ConnectionReport(
        connection_id=group.connection_id,
        status="TERMINATED" if terminated else "ERROR",
        verdict=code,
        business_code=code,
        errors=errors,
        streams=_streams_report(group),
        findings=findings or [],
        hits=[],
        selected_rule=None,
        selected_generation=None,
        selected_finding_code=None,
        terminated=terminated,
        termination_reason=code if terminated else None,
    )


class Engine:
    def __init__(self, config: EngineConfig | None = None):
        self.config = config or EngineConfig()
        self.config.validate()

    def analyze_connection(
        self,
        group: ConnectionGroup,
        view: RuleView,
        closure: ClosureResult,
    ) -> ConnectionReport:
        start = time.monotonic()
        check_timeout = _timeout_checker(
            start, self.config.per_connection_timeout_ms
        )
        invalid = set(closure.invalid_rules)
        try:
            check_timeout()
            if (
                self.config.max_messages_per_connection is not None
                and group.delivered_message_count
                > self.config.max_messages_per_connection
            ):
                raise BudgetStop(
                    "E_MSGS",
                    "per-connection message budget exceeded",
                    {
                        "limit": self.config.max_messages_per_connection,
                        "observed": group.delivered_message_count,
                    },
                )
            if (
                self.config.max_bytes_per_connection is not None
                and group.delivered_bytes > self.config.max_bytes_per_connection
            ):
                raise BudgetStop(
                    "E_BYTES",
                    "per-connection decoded byte budget exceeded",
                    {
                        "limit": self.config.max_bytes_per_connection,
                        "observed": group.delivered_bytes,
                    },
                )

            errors: list[dict[str, Any]] = []
            if group.mixup_sequences:
                errors.append(
                    {
                        "code": "E_MIXUP",
                        "message": "cross-connection message injection detected",
                        "details": {
                            "conflicting_sequences": list(group.mixup_sequences)
                        },
                    }
                )
            gaps = group.gaps()
            if gaps:
                errors.append(
                    {
                        "code": "E_MSG_GAP",
                        "message": "message sequence gap prevents reconstruction",
                        "details": {"missing_ranges": gaps},
                    }
                )
            if errors:
                code = "E_MIXUP" if group.mixup_sequences else "E_MSG_GAP"
                return _error_report(group, code, errors, terminated=False)

            findings: list[Finding] = []
            parsed_requests: list[dict[str, Any]] = []
            request_stream = group.streams.get("request")
            if request_stream is not None and request_stream.reconstructed:
                analysis = analyze_request_stream(
                    request_stream.reconstructed,
                    group.connection_id,
                    check_timeout=check_timeout,
                    max_requests=self.config.max_requests_per_connection,
                )
                findings = analysis.findings
                parsed_requests = analysis.report_requests()
            check_timeout()
            streams = _streams_report(group)
            if "request" in streams["directions"]:
                streams["directions"]["request"]["parsed_requests"] = parsed_requests

            decision = _evaluate_findings(
                group.connection_id, findings, view, invalid
            )
            invalidated_hits = tuple(
                sorted(
                    {
                        hit.canonical_name
                        for hit in decision.hits
                        if hit.canonical_name in invalid
                    }
                )
            )
            return ConnectionReport(
                connection_id=group.connection_id,
                status="OK",
                verdict=decision.verdict,
                business_code=decision.business_code,
                errors=[],
                streams=streams,
                findings=findings,
                hits=decision.hits,
                selected_rule=decision.selected_rule,
                selected_generation=decision.selected_generation,
                selected_finding_code=decision.selected_finding_code,
                invalidated_hits=invalidated_hits,
                affected_by_invalid_rules=bool(invalidated_hits),
            )
        except BudgetStop as stop:
            return _error_report(
                group,
                stop.code,
                [stop.error()],
                terminated=True,
            )
        except Exception as exc:  # One connection must never abort the run.
            return _error_report(
                group,
                "E_INTERNAL",
                [
                    {
                        "code": "E_INTERNAL",
                        "message": f"{type(exc).__name__}: {exc}",
                        "details": {},
                    }
                ],
                terminated=True,
            )

    def _cycle_reports(
        self, groups: list[ConnectionGroup], closure: ClosureResult
    ) -> list[ConnectionReport]:
        reports = []
        for group in groups:
            reports.append(
                _error_report(
                    group,
                    "E_RULE_CYCLE",
                    [
                        {
                            "code": "E_RULE_CYCLE",
                            "message": "rule dependency cycle detected",
                            "details": {"rules": list(closure.cycle)},
                        }
                    ],
                    terminated=False,
                )
            )
        return reports

    def _finalize(
        self,
        reports: list[ConnectionReport],
        closure: ClosureResult,
        view: RuleView,
        rejected: list[Rejection],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> EngineReport:
        connection_hits = {
            report.connection_id: {
                hit.canonical_name for hit in report.hits
            }
            for report in reports
        }
        closure = attach_affected_connections(closure, connection_hits)
        invalid = set(closure.invalid_rules)
        for report in reports:
            invalidated = tuple(
                sorted(
                    {
                        hit.canonical_name
                        for hit in report.hits
                        if hit.canonical_name in invalid
                    }
                )
            )
            report.invalidated_hits = invalidated
            report.affected_by_invalid_rules = bool(invalidated)
            if not report.hits:
                report.affected_by_invalid_rules = (
                    report.connection_id in closure.affected_connections
                )

        reports.sort(key=lambda report: report.connection_id)
        rejected = sorted(
            rejected,
            key=lambda item: (
                item.source,
                item.record_index,
                item.code,
                item.message,
                item.record_ref or "",
            ),
        )
        terminated = [
            report.connection_id for report in reports if report.terminated
        ]
        ruleset = view.to_dict()
        ruleset["active_rule_names"] = [
            record.canonical_name for record in view.active_rules
        ]
        ruleset["invalid_rule_names"] = list(closure.invalid_rules)
        ruleset["analysis_policy_version"] = ANALYSIS_POLICY_VERSION
        ruleset["merge_policy_version"] = MERGE_POLICY_VERSION
        report = EngineReport(
            ruleset=ruleset,
            closure=closure.to_dict(),
            connections=reports,
            rejected=rejected,
            terminated_connections=terminated,
            schema_version=REPORT_SCHEMA_VERSION,
            metadata={
                **(metadata or {}),
                "config": self.config.to_dict(),
                "concurrency": self.config.concurrency,
                "analysis_policy_version": ANALYSIS_POLICY_VERSION,
                "merge_policy_version": MERGE_POLICY_VERSION,
            },
        )
        report.summary = report._build_summary()
        return report

    def run(
        self,
        groups: list[ConnectionGroup],
        view: RuleView,
        *,
        rejected: Iterable[Rejection] = (),
        metadata: dict[str, Any] | None = None,
    ) -> EngineReport:
        closure = compute_invalid_closure(view)
        if closure.cycle:
            reports = self._cycle_reports(groups, closure)
        else:
            with ThreadPoolExecutor(max_workers=self.config.concurrency) as executor:
                futures = {
                    executor.submit(
                        self.analyze_connection, group, view, closure
                    ): group
                    for group in groups
                }
                reports = [future.result() for future in futures]
        return self._finalize(
            reports,
            closure,
            view,
            list(rejected) + list(view.rejects),
            metadata=metadata,
        )

    def rejudge(
        self,
        groups: list[ConnectionGroup],
        view: RuleView,
        previous_report: EngineReport,
        *,
        rejected: Iterable[Rejection] = (),
    ) -> EngineReport:
        """Rebuild the rule view but reparse only connections with changed hits.

        Rule matching against the prior report's cached findings is a dry-run
        used to decide whether a connection is affected.  Only affected
        connections run the full byte parser.  Unchanged connection decisions
        are copied from the prior report.
        """

        closure = compute_invalid_closure(view)
        previous_by_id = {
            report.connection_id: report for report in previous_report.connections
        }
        groups_by_id = {group.connection_id: group for group in groups}
        affected: set[str] = set()
        for connection_id, group in groups_by_id.items():
            previous = previous_by_id.get(connection_id)
            if previous is None or closure.cycle:
                affected.add(connection_id)
                continue
            if previous.status != "OK":
                # Budget, gap, mixup and internal terminations have no rule
                # decision to invalidate and are replayed byte-identically.
                continue
            dry_run = _evaluate_findings(
                connection_id,
                previous.findings,
                view,
                set(closure.invalid_rules),
            )
            if (
                _hit_signature(dry_run.hits) != _hit_signature(previous.hits)
                or dry_run.selected_rule != previous.selected_rule
                or dry_run.business_code != previous.business_code
                or dry_run.verdict != previous.verdict
            ):
                affected.add(connection_id)

        reports: list[ConnectionReport] = []
        if closure.cycle:
            reports = self._cycle_reports(
                [groups_by_id[key] for key in sorted(groups_by_id)], closure
            )
            affected = set(groups_by_id)
        else:
            for connection_id in sorted(groups_by_id):
                if connection_id in affected:
                    reports.append(
                        self.analyze_connection(
                            groups_by_id[connection_id], view, closure
                        )
                    )
                else:
                    reports.append(copy.deepcopy(previous_by_id[connection_id]))

        return self._finalize(
            reports,
            closure,
            view,
            list(rejected) + list(view.rejects),
            metadata={
                "incremental": True,
                "recomputed_connections": sorted(affected),
                "reused_connections": sorted(
                    set(groups_by_id) - affected
                ),
            },
        )

    def verify_incremental(
        self,
        groups: list[ConnectionGroup],
        view: RuleView,
        previous_report: EngineReport,
        *,
        rejected: Iterable[Rejection] = (),
    ) -> tuple[bool, EngineReport, EngineReport]:
        incremental = self.rejudge(
            groups, view, previous_report, rejected=rejected
        )
        full = self.run(groups, view, rejected=rejected)
        return (
            incremental.deterministic_dict() == full.deterministic_dict(),
            incremental,
            full,
        )

    def run_with_checkpoint(
        self,
        groups: list[ConnectionGroup],
        view: RuleView,
        manager: CheckpointManager,
        *,
        run_identity: str,
        rejected: Iterable[Rejection] = (),
    ) -> EngineReport:
        """Resume the last complete checkpoint and checkpoint every new result.

        Completed records are replay-validated using an input fingerprint and a
        decision digest before reuse.  A higher-generation half-write snapshot
        is ignored by the checkpoint manager.  Rule-view, merge-policy,
        checkpoint-schema, config and run-identity changes force a full rebuild.
        """

        closure = compute_invalid_closure(view)
        load = manager.load(
            expected_config_fingerprint=self.config.fingerprint(),
            expected_rules_version=view.version,
            expected_merge_policy_version=view.merge_policy_version,
        )
        completed: dict[str, Any] = {}
        resume_reason = load.reason
        if load.payload is not None and not closure.cycle:
            if load.payload.get("run_identity") == run_identity:
                raw_completed = load.payload.get("completed", {})
                if isinstance(raw_completed, dict):
                    completed = raw_completed
            else:
                resume_reason = "E_CHECKPOINT_RUN_ID_CHANGED"

        rejected_list = list(rejected)
        reports: list[ConnectionReport] = []
        reused: list[str] = []
        recomputed: list[str] = []
        if closure.cycle:
            reports = self._cycle_reports(groups, closure)
            recomputed = sorted(group.connection_id for group in groups)
        else:
            for group in groups:
                fingerprint = group_fingerprint(group)
                entry = completed.get(group.connection_id)
                report = None
                if isinstance(entry, dict) and entry.get("input_fingerprint") == fingerprint:
                    decision_digest = entry.get("decision_sha256")
                    try:
                        candidate = connection_report_from_dict(entry["report"])
                    except (KeyError, TypeError, ValueError):
                        candidate = None
                    if candidate is not None:
                        candidate_digest = hashlib.sha256(
                            canonical_json(
                                candidate.stable_decision_dict()
                            ).encode("utf-8")
                        ).hexdigest()
                        if candidate_digest == decision_digest:
                            report = candidate
                if report is not None:
                    reports.append(report)
                    reused.append(group.connection_id)
                    continue
                report = self.analyze_connection(group, view, closure)
                reports.append(report)
                recomputed.append(group.connection_id)
                entry = {
                    "input_fingerprint": fingerprint,
                    "decision_sha256": hashlib.sha256(
                        canonical_json(
                            report.stable_decision_dict()
                        ).encode("utf-8")
                    ).hexdigest(),
                    "report": report.to_dict(),
                }
                completed[group.connection_id] = entry
                verdict_counts: dict[str, int] = {}
                for completed_entry in completed.values():
                    verdict = completed_entry.get("report", {}).get(
                        "verdict", "UNKNOWN"
                    )
                    verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
                payload = {
                    "run_identity": run_identity,
                    "rules_version": view.version,
                    "merge_policy_version": view.merge_policy_version,
                    "checkpoint_schema_version": manager.schema_version,
                    "completed_connection_ids": sorted(completed),
                    "completed": completed,
                    "aggregate": {
                        "connection_count": len(groups),
                        "completed_count": len(completed),
                        "terminated_count": sum(
                            1
                            for item in completed.values()
                            if item.get("report", {}).get("terminated")
                        ),
                        "verdict_counts": dict(sorted(verdict_counts.items())),
                    },
                }
                manager.save(
                    payload,
                    config_fingerprint=self.config.fingerprint(),
                    rules_version=view.version,
                    merge_policy_version=view.merge_policy_version,
                )

        report = self._finalize(
            reports,
            closure,
            view,
            rejected_list + list(view.rejects),
            metadata={
                "checkpoint": {
                    "enabled": True,
                    "resumed_generation": load.generation,
                    "checkpoint_id": load.checkpoint_id,
                    "recovered_from_residue": load.recovered,
                    "residue": list(load.residue),
                    "resume_reason": resume_reason,
                    "reused_connections": sorted(reused),
                    "recomputed_connections": sorted(recomputed),
                }
            },
        )
        return report


def run_engine(
    *,
    messages: str | Path | Iterable[str],
    v1_path: str | Path | None = None,
    v2_shards: Iterable[str | Path] = (),
    v2_database: str | Path | None = None,
    v2_db: str | Path | None = None,
    observed_clock_ms: int | None = None,
    config: EngineConfig | None = None,
    previous_view: RuleView | None = None,
) -> EngineReport:
    config = config or EngineConfig()
    messages_list, parse_rejects = parse_messages(messages)
    groups, _ = group_connections(
        messages_list, first_sequence=config.first_sequence
    )
    view = load_rule_view(
        v1_path=v1_path,
        v2_shards=v2_shards,
        v2_database=v2_database,
        v2_db=v2_db,
        observed_clock_ms=observed_clock_ms,
        previous_view=previous_view,
    )
    return Engine(config).run(groups, view, rejected=parse_rejects)

