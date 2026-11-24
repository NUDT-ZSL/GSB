"""Unified interactive and batch MFA decision engine."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .atomic import read_json
from .checkpoint import CheckpointManager
from .errors import Codes, MFAError
from .fixedpoint import compare_device
from .ledger import Ledger
from .models import (
    Attempt,
    BatchReport,
    DecisionResult,
    Factor,
    PolicyView,
    Registration,
    TrustedFactor,
)
from .otp import evaluate_otp
from .parser import (
    parse_attempts_jsonl_text,
    parse_attempts_records,
    parse_registrations_document,
)
from .policy import load_policy
from .reporting import write_report
from .util import canonical_id


@dataclass
class EngineConfig:
    canonical_scale: int = 6
    fingerprint_threshold_micro: int = 5_000
    concurrency: int = 4
    timeout_ms: int | None = 2_000
    queue_limit: int | None = 64
    default_timestamp_error_us: int = 500
    auto_recover: bool = True

    def validate(self) -> None:
        if self.canonical_scale < 0 or self.canonical_scale > 6:
            raise MFAError(Codes.SCALE_RANGE, "canonical_scale must be in [0, 6]")
        if self.fingerprint_threshold_micro < 0:
            raise MFAError(
                Codes.INPUT_STRUCTURE,
                "fingerprint_threshold_micro must be non-negative",
            )
        if self.concurrency <= 0:
            raise MFAError(Codes.INPUT_STRUCTURE, "concurrency must be positive")
        if self.timeout_ms is not None and self.timeout_ms <= 0:
            raise MFAError(Codes.INPUT_STRUCTURE, "timeout_ms must be positive")
        if self.queue_limit is not None and self.queue_limit < 0:
            raise MFAError(Codes.INPUT_STRUCTURE, "queue_limit must be non-negative")
        if self.default_timestamp_error_us < 0:
            raise MFAError(
                Codes.INPUT_STRUCTURE,
                "default_timestamp_error_us must be non-negative",
            )


@dataclass
class Preflight:
    attempt: Attempt
    status: str
    reasons: list[str]
    issues: list[dict[str, Any]]
    trusted: TrustedFactor | None
    factor: Factor | None
    fingerprint: dict[str, Any] | None
    otp: dict[str, Any] | None
    ticket_generation: int


def _wall_ms() -> int:
    return time.time_ns() // 1_000_000


def _stable_attempt_key(attempt: Attempt) -> tuple[Any, ...]:
    return (
        attempt.submitted_at_ms,
        attempt.attempt_no,
        attempt.user_id,
        attempt.factor_id,
    )


class MFAEngine:
    """Shared decision function for preview and batch requests."""

    def __init__(
        self,
        *,
        registrations: Iterable[Registration],
        policy: PolicyView,
        ledger_path: str | Path,
        work_dir: str | Path,
        config: EngineConfig | None = None,
        initial_parse_issues: list[dict[str, Any]] | None = None,
        initial_attempts: list[Attempt] | None = None,
    ) -> None:
        self.config = config or EngineConfig()
        self.config.validate()
        self.policy = policy
        self.users = {registration.user_id: registration for registration in registrations}
        self.factors: dict[str, Factor] = {}
        self.factor_users: dict[str, str] = {}
        for registration in self.users.values():
            for factor in registration.factors:
                self.factors[factor.factor_id] = factor
                self.factor_users[factor.factor_id] = registration.user_id

        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(ledger_path)
        self.checkpoints = CheckpointManager(self.ledger, self.work_dir)
        self.initial_parse_issues = list(initial_parse_issues or [])
        self.initial_attempts = list(initial_attempts or [])
        self.recovery_result: dict[str, Any] | None = None
        if self.config.auto_recover:
            self.recovery_result = self.checkpoints.recover()

        policy_state = self.ledger.ensure_policy(policy)
        self.policy_replaced = bool(
            policy_state["rebuilt"] and policy_state["reason"] != "initial"
        )
        self.ledger_rebuilt = bool(policy_state["rebuilt"])
        self.rebuild_reason = policy_state["reason"]
        self.previous_policy_version = policy_state["previous_policy_version"]
        if self.policy_replaced:
            self.checkpoints.clear_files()

    @property
    def policy_version(self) -> str:
        return self.policy.policy_version

    def close(self) -> None:
        self.ledger.close()

    def __enter__(self) -> "MFAEngine":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _preflight(self, attempt: Attempt) -> Preflight:
        issues: list[dict[str, Any]] = []
        registration = self.users.get(attempt.user_id)
        if registration is None:
            return self._early_preflight(
                attempt, Codes.UNKNOWN_USER, "user is not registered"
            )
        factor = self.factors.get(attempt.factor_id)
        if factor is None:
            return self._early_preflight(
                attempt, Codes.UNKNOWN_FACTOR, "factor is not registered"
            )
        if self.factor_users.get(attempt.factor_id) != attempt.user_id:
            return self._early_preflight(
                attempt,
                Codes.FACTOR_NOT_BOUND,
                "factor is not bound to the supplied user",
            )
        trusted = self.policy.trusted_factors.get(attempt.factor_id)
        if trusted is None:
            return self._early_preflight(
                attempt,
                Codes.FACTOR_NOT_TRUSTED,
                "factor is not in the effective trusted policy set",
            )
        if factor.factor_type not in self.policy.allowed_factor_types:
            return self._early_preflight(
                attempt,
                Codes.POLICY_TYPE_DENIED,
                "factor type is outside the effective policy type allowlist",
            )

        attempt_scale = (
            attempt.fingerprint_scale
            if attempt.fingerprint_scale is not None
            else factor.bound_device.scale
        )
        fingerprint = compare_device(
            factor.bound_device,
            attempt.device_components,
            attempt_scale,
            self.config.fingerprint_threshold_micro,
            self.config.canonical_scale,
        )
        otp = evaluate_otp(
            factor.shared_secret_digest,
            attempt.submitted_at_ms,
            attempt.timestamp_error_us,
            trusted.verification_window_ms,
            attempt.totp_code,
        )

        reasons: list[str] = []
        definite_failure = False
        uncertain = False
        if fingerprint.conclusion == "MISMATCH":
            reasons.append(fingerprint.code or Codes.DEVICE_MISMATCH)
            definite_failure = True
        elif fingerprint.conclusion == "AMBIGUOUS":
            reasons.append(Codes.FP_AMBIGUOUS)
            uncertain = True
        if otp.result == "REJECT":
            reasons.append(Codes.OTP_MISMATCH)
            definite_failure = True
        elif otp.result == "AMBIGUOUS":
            reasons.append(Codes.OTP_AMBIGUOUS)
            uncertain = True

        if definite_failure:
            status = "REJECTED"
        elif uncertain:
            status = "INDETERMINATE"
        else:
            status = "APPROVED"
        return Preflight(
            attempt=attempt,
            status=status,
            reasons=reasons,
            issues=issues,
            trusted=trusted,
            factor=factor,
            fingerprint=fingerprint.to_dict(),
            otp=otp.to_dict(),
            ticket_generation=trusted.ticket_generation,
        )

    def _early_preflight(
        self, attempt: Attempt, code: str, message: str
    ) -> Preflight:
        return Preflight(
            attempt=attempt,
            status="PREVALIDATION_REJECTED",
            reasons=[code],
            issues=[
                {
                    "source": "decision_precheck",
                    "record_index": attempt.source_index,
                    "code": code,
                    "message": message,
                    "detail": {
                        "attempt_no": attempt.attempt_no,
                        "user_id": attempt.user_id,
                        "factor_id": attempt.factor_id,
                    },
                }
            ],
            trusted=None,
            factor=self.factors.get(attempt.factor_id),
            fingerprint=None,
            otp=None,
            ticket_generation=0,
        )

    def _preflight_many(self, attempts: list[Attempt]) -> list[Preflight]:
        if self.config.concurrency == 1 or len(attempts) <= 1:
            return [self._preflight(attempt) for attempt in attempts]
        with ThreadPoolExecutor(max_workers=self.config.concurrency) as executor:
            return list(executor.map(self._preflight, attempts))

    def _result_from_preflight(
        self,
        preflight: Preflight,
        *,
        start_ns: int,
        preview: bool,
    ) -> DecisionResult:
        attempt = preflight.attempt
        result = DecisionResult(
            attempt_no=attempt.attempt_no,
            status=preflight.status,
            approved=preflight.status == "APPROVED",
            reasons=list(preflight.reasons),
            policy_version=self.policy.policy_version,
            policy_generation=self.policy.policy_generation,
            canonical_fp_key=(
                preflight.fingerprint.get("canonical_fp_key")
                if preflight.fingerprint
                else None
            ),
            fingerprint=preflight.fingerprint,
            otp=preflight.otp,
            ledger_watermark=attempt.submitted_at_ms,
            preview=preview,
            issues=list(preflight.issues),
            user_id=attempt.user_id,
            factor_id=attempt.factor_id,
            source_index=attempt.source_index,
            submitted_at_ms=attempt.submitted_at_ms,
            risk_score_millis=attempt.risk_score_millis,
        )
        result.latency_ms = max(0.0, (time.perf_counter_ns() - start_ns) / 1_000_000)
        return result

    @staticmethod
    def _repoison_result(
        result: DecisionResult,
        *,
        status: str,
        reason: str,
    ) -> None:
        result.status = status
        result.approved = status == "APPROVED"
        if reason not in result.reasons:
            result.reasons.append(reason)

    def _finalize(
        self,
        result: DecisionResult,
        preflight: Preflight,
        *,
        batch_seq: int,
        persist: bool,
        wall_ms: int,
    ) -> DecisionResult:
        attempt = preflight.attempt
        if not result.adjudicated:
            return result
        trusted = preflight.trusted
        if trusted is None:
            return result

        with self.ledger.transaction(rollback=not persist):
            if result.status == "APPROVED":
                existing = self.ledger.find_ticket_by_user_generation(
                    attempt.user_id, preflight.ticket_generation
                )
                if existing is not None:
                    result.ticket = {
                        "ticket_generation": existing["ticket_generation"],
                        "status": existing["status"],
                        "issued_attempt_no": existing["issued_attempt_no"],
                    }
                    if existing["status"] == "REVOKED":
                        self._repoison_result(
                            result,
                            status="REJECTED",
                            reason=Codes.TICKET_REVOKED,
                        )
                    else:
                        self._repoison_result(
                            result,
                            status="REJECTED",
                            reason=Codes.TICKET_REPLAY,
                        )
                        result.replay = self.ledger.append_replay(
                            user_id=attempt.user_id,
                            factor_id=attempt.factor_id,
                            ticket_generation=preflight.ticket_generation,
                            attempt_no=attempt.attempt_no,
                        )
                else:
                    result.ticket = self.ledger.issue_ticket(
                        user_id=attempt.user_id,
                        factor_id=attempt.factor_id,
                        ticket_generation=preflight.ticket_generation,
                        attempt_no=attempt.attempt_no,
                        submitted_at_ms=attempt.submitted_at_ms,
                        trusted=trusted,
                        policy_version=self.policy.policy_version,
                        canonical_fp_key=result.canonical_fp_key,
                        watermark=attempt.submitted_at_ms,
                        batch_seq=batch_seq,
                    )
            if result.adjudicated and persist:
                self.ledger.insert_decision(
                    result=result,
                    attempt_no=attempt.attempt_no,
                    user_id=attempt.user_id,
                    factor_id=attempt.factor_id,
                    ticket_generation=preflight.ticket_generation,
                    watermark=attempt.submitted_at_ms,
                    batch_seq=batch_seq,
                    wall_ms=wall_ms,
                )
        return result

    def _deadline_exceeded(self, start_ns: int) -> bool:
        if self.config.timeout_ms is None:
            return False
        return (time.perf_counter_ns() - start_ns) / 1_000_000 > self.config.timeout_ms

    def _non_adjudicated_result(
        self,
        attempt: Attempt,
        *,
        status: str,
        code: str,
        start_ns: int,
        preview: bool = False,
    ) -> DecisionResult:
        return DecisionResult(
            attempt_no=attempt.attempt_no,
            status=status,
            approved=False,
            reasons=[code],
            policy_version=self.policy.policy_version,
            policy_generation=self.policy.policy_generation,
            latency_ms=max(0.0, (time.perf_counter_ns() - start_ns) / 1_000_000),
            preview=preview,
            issues=[
                {
                    "source": "batch_gate",
                    "record_index": attempt.source_index,
                    "code": code,
                    "message": (
                        "attempt exceeded the configured real wall-clock timeout"
                        if code == Codes.TIMEOUT
                        else "attempt exceeded the configured pending queue limit"
                    ),
                    "detail": {"attempt_no": attempt.attempt_no},
                }
            ],
            user_id=attempt.user_id,
            factor_id=attempt.factor_id,
            source_index=attempt.source_index,
            submitted_at_ms=attempt.submitted_at_ms,
            risk_score_millis=attempt.risk_score_millis,
        )

    def preview(self, raw_attempt: dict[str, Any] | Attempt) -> DecisionResult:
        start_ns = time.perf_counter_ns()
        if isinstance(raw_attempt, Attempt):
            attempt = raw_attempt
            parse_issues: list[dict[str, Any]] = []
        else:
            parsed = parse_attempts_records(
                [raw_attempt],
                source="preview",
                default_timestamp_error_us=self.config.default_timestamp_error_us,
            )
            if parsed.issues or not parsed.records:
                attempt_value = parsed.records[0] if parsed.records else None
                return DecisionResult(
                    attempt_no=(
                        attempt_value.attempt_no if attempt_value is not None else None
                    ),
                    status="PREVALIDATION_REJECTED",
                    approved=False,
                    reasons=[item.code for item in parsed.issues],
                    policy_version=self.policy.policy_version,
                    policy_generation=self.policy.policy_generation,
                    latency_ms=max(
                        0.0, (time.perf_counter_ns() - start_ns) / 1_000_000
                    ),
                    preview=True,
                    issues=[item.to_dict() for item in parsed.issues],
                )
            attempt = parsed.records[0]

        preflight = self._preflight(attempt)
        result = self._result_from_preflight(
            preflight, start_ns=start_ns, preview=True
        )
        if self._deadline_exceeded(start_ns):
            return self._non_adjudicated_result(
                attempt,
                status="TIMEOUT",
                code=Codes.TIMEOUT,
                start_ns=start_ns,
                preview=True,
            )
        result = self._finalize(
            result,
            preflight,
            batch_seq=0,
            persist=False,
            wall_ms=_wall_ms(),
        )
        result.latency_ms = max(
            0.0, (time.perf_counter_ns() - start_ns) / 1_000_000
        )
        return result

    def process_batch(
        self,
        attempts: Iterable[Attempt] | None = None,
        *,
        parse_issues: list[dict[str, Any]] | None = None,
        checkpoint: bool = True,
    ) -> BatchReport:
        started_wall_ms = _wall_ms()
        batch_start_ns = time.perf_counter_ns()
        candidate_attempts = sorted(
            list(self.initial_attempts if attempts is None else attempts),
            key=_stable_attempt_key,
        )
        parse_rejections = list(
            self.initial_parse_issues if parse_issues is None else parse_issues
        )
        queue_limit = self.config.queue_limit
        accepted = candidate_attempts
        overflow: list[Attempt] = []
        if queue_limit is not None and len(candidate_attempts) > queue_limit:
            accepted = candidate_attempts[:queue_limit]
            overflow = candidate_attempts[queue_limit:]

        preflights = self._preflight_many(accepted)
        results: list[DecisionResult] = []
        batch_seq = self.ledger.allocate_batch_seq()
        maximum_watermark = 0
        for preflight in preflights:
            attempt = preflight.attempt
            maximum_watermark = max(maximum_watermark, attempt.submitted_at_ms)
            if self._deadline_exceeded(batch_start_ns):
                results.append(
                    self._non_adjudicated_result(
                        attempt,
                        status="TIMEOUT",
                        code=Codes.TIMEOUT,
                        start_ns=batch_start_ns,
                    )
                )
                continue
            result = self._result_from_preflight(
                preflight, start_ns=batch_start_ns, preview=False
            )
            if self._deadline_exceeded(batch_start_ns):
                results.append(
                    self._non_adjudicated_result(
                        attempt,
                        status="TIMEOUT",
                        code=Codes.TIMEOUT,
                        start_ns=batch_start_ns,
                    )
                )
                continue
            result = self._finalize(
                result,
                preflight,
                batch_seq=batch_seq,
                persist=True,
                wall_ms=_wall_ms(),
            )
            result.latency_ms = max(
                0.0, (time.perf_counter_ns() - batch_start_ns) / 1_000_000
            )
            results.append(result)

        for attempt in overflow:
            maximum_watermark = max(maximum_watermark, attempt.submitted_at_ms)
            results.append(
                self._non_adjudicated_result(
                    attempt,
                    status="QUEUE_FULL",
                    code=Codes.QUEUE_FULL,
                    start_ns=batch_start_ns,
                )
            )

        if maximum_watermark:
            self.ledger.mark_watermark(maximum_watermark)
        checkpoint_result: dict[str, Any] | None = None
        if checkpoint:
            try:
                checkpoint_result = self.checkpoints.write_checkpoint(
                    batch_seq=batch_seq,
                    watermark=maximum_watermark,
                    policy_version=self.policy.policy_version,
                )
            except MFAError as exc:
                checkpoint_result = {
                    "status": "ERROR",
                    "error": exc.to_dict(),
                }

        return BatchReport(
            batch_seq=batch_seq,
            policy=self.policy,
            results=results,
            parse_issues=parse_rejections,
            policy_issues=[item.to_dict() for item in self.policy.issues],
            policy_replaced=self.policy_replaced,
            ledger_rebuilt=self.ledger_rebuilt,
            rebuild_reason=self.rebuild_reason,
            previous_policy_version=self.previous_policy_version,
            checkpoint=checkpoint_result,
            started_wall_ms=started_wall_ms,
            finished_wall_ms=_wall_ms(),
        )

    def report_dict(self, report: BatchReport) -> dict[str, Any]:
        counts = report.counts()
        checkpoint = report.checkpoint or {}
        rejects = list(report.parse_issues)
        rejects.extend(report.policy_issues)
        for result in report.results:
            rejects.extend(result.issues)
        rejects.sort(
            key=lambda item: (
                str(item.get("source", "")),
                int(item.get("record_index", 0) or 0),
                str(item.get("code", "")),
            )
        )
        return {
            "generated_wall_ms": report.finished_wall_ms,
            "batch_seq": report.batch_seq,
            "policy": {
                "policy_key": report.policy.policy_key,
                "as_of_ms": report.policy.as_of_ms,
                "used_policy_version": report.policy.policy_version,
                "policy_generation": report.policy.policy_generation,
                "layout_version": report.policy.layout_version,
                "trusted_factor_ids": report.policy.trusted_factor_ids,
                "allowed_factor_types": list(report.policy.allowed_factor_types),
                "canonical_scale": report.policy.canonical_scale,
                "source_counts": report.policy.source_counts,
                "snapshot_replaced": report.policy_replaced,
                "ledger_rebuilt": report.ledger_rebuilt,
                "rebuild_reason": report.rebuild_reason,
                "previous_policy_version": report.previous_policy_version,
                "issues": report.policy_issues,
            },
            "counts": counts,
            "results": [result.to_dict() for result in report.results],
            "rejects": rejects,
            "checkpoint": checkpoint,
            "ledger": {
                "path": str(self.ledger.path),
                "digest": self.ledger.ledger_digest(),
                "counts": self.ledger.counts(),
            },
            "recovery": self.recovery_result,
            "wall_clock_span_ms": report.finished_wall_ms - report.started_wall_ms,
            "latency_basis": "time.perf_counter_ns from request intake to visible result",
        }

    def write_report(
        self, report: BatchReport, path: str | Path
    ) -> dict[str, Any]:
        return write_report(path, self.report_dict(report))

    def recover(self) -> dict[str, Any]:
        recovery = self.checkpoints.recover()
        self.recovery_result = recovery
        return recovery

    def revoke_user(
        self, user_id: str, *, reason: str = "user_revoked", batch_seq: int = 0
    ) -> dict[str, Any]:
        canonical = canonical_id(user_id)
        if canonical is None:
            raise MFAError(Codes.ID_INVALID, "invalid user_id")
        return self.ledger.revoke_user(
            canonical,
            reason=reason,
            batch_seq=batch_seq,
            wall_ms=_wall_ms(),
        )

    def revoke_factor(
        self, factor_id: str, *, reason: str = "factor_revoked", batch_seq: int = 0
    ) -> dict[str, Any]:
        canonical = canonical_id(factor_id)
        if canonical is None:
            raise MFAError(Codes.ID_INVALID, "invalid factor_id")
        return self.ledger.revoke_factor(
            canonical,
            reason=reason,
            batch_seq=batch_seq,
            wall_ms=_wall_ms(),
        )

    def renew_ticket(
        self,
        *,
        user_id: str,
        factor_id: str,
        ticket_generation: int,
        now_ms: int,
        batch_seq: int = 0,
    ) -> dict[str, Any]:
        return self.ledger.renew_ticket(
            user_id=canonical_id(user_id) or user_id,
            factor_id=canonical_id(factor_id) or factor_id,
            ticket_generation=ticket_generation,
            now_ms=now_ms,
            batch_seq=batch_seq,
        )

    def add_ticket_edge(self, parent: str, child: str) -> None:
        self.ledger.add_ticket_edge(parent, child)


def _manifest_value(
    manifest: dict[str, Any], *names: str, default: Any = None
) -> Any:
    for name in names:
        if name in manifest:
            return manifest[name]
    return default


def load_engine_from_samples(
    samples_dir: str | Path,
    *,
    ledger_path: str | Path | None = None,
    work_dir: str | Path | None = None,
    config: EngineConfig | None = None,
    auto_recover: bool | None = None,
) -> MFAEngine:
    samples = Path(samples_dir)
    manifest = read_json(samples / "manifest.json")
    if not isinstance(manifest, dict):
        raise MFAError(Codes.INPUT_STRUCTURE, "manifest.json must be an object")

    registrations_doc = read_json(samples / "registrations.json")
    registrations = parse_registrations_document(
        registrations_doc, source="registrations"
    )
    attempts_text = (samples / "attempts.jsonl").read_text(encoding="utf-8")
    default_error_us = int(
        _manifest_value(
            manifest,
            "default_timestamp_error_us",
            default=500,
        )
    )
    attempts = parse_attempts_jsonl_text(
        attempts_text,
        source="attempts",
        default_timestamp_error_us=default_error_us,
    )
    as_of_ms = _manifest_value(
        manifest,
        "policy_as_of_ms",
        default=(
            max((attempt.submitted_at_ms for attempt in attempts.records), default=0)
            or int(manifest.get("observation_clock_ms", 0))
        ),
    )
    factor_types = {
        factor.factor_id: factor.factor_type
        for registration in registrations.records
        for factor in registration.factors
    }
    shard_paths = [
        samples / name for name in manifest.get("policy_v2_shards", [])
    ]
    if not shard_paths:
        shard_paths = sorted((samples / "policy_v2").glob("*.json"))
    policy = load_policy(
        policy_key=_manifest_value(manifest, "policy_key", default="mfa-policy"),
        observation_clock_ms=int(manifest.get("observation_clock_ms", 0)),
        v1_path=samples / _manifest_value(
            manifest, "policy_v1_file", default="policy_v1.json"
        ),
        v2_shard_paths=shard_paths,
        v2_db_path=samples / _manifest_value(
            manifest, "policy_v2_db", default="policy_v2.sqlite"
        ),
        as_of_ms=int(as_of_ms),
        factor_types=factor_types,
        canonical_scale=int(
            _manifest_value(manifest, "canonical_scale", default=6)
        ),
    )

    effective_config = config or EngineConfig(
        canonical_scale=int(
            _manifest_value(manifest, "canonical_scale", default=6)
        ),
        fingerprint_threshold_micro=int(
            _manifest_value(
                manifest,
                "fingerprint_threshold_micro",
                default=5_000,
            )
        ),
        concurrency=int(
            _manifest_value(manifest, "default_concurrency", default=4)
        ),
        timeout_ms=int(
            _manifest_value(manifest, "default_timeout_ms", default=2_000)
        ),
        queue_limit=int(
            _manifest_value(manifest, "default_queue_limit", default=64)
        ),
        default_timestamp_error_us=default_error_us,
        auto_recover=(
            bool(_manifest_value(manifest, "auto_recover", default=True))
            if auto_recover is None
            else auto_recover
        ),
    )
    if auto_recover is not None:
        effective_config.auto_recover = auto_recover

    work = Path(work_dir) if work_dir is not None else samples.parent / "work"
    ledger = Path(ledger_path) if ledger_path is not None else work / "mfa_ledger.sqlite"
    parse_issues = [
        item.to_dict() for item in [*registrations.issues, *attempts.issues]
    ]
    return MFAEngine(
        registrations=registrations.records,
        policy=policy,
        ledger_path=ledger,
        work_dir=work,
        config=effective_config,
        initial_parse_issues=parse_issues,
        initial_attempts=attempts.records,
    )

