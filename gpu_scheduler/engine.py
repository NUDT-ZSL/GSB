from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import (
    FAULT_ERROR_CODES,
    InjectedFault,
    NotFoundError,
    SchedulerError,
    ValidationError,
)
from .models import load_bundle
from .planner import (
    card_placement,
    evaluate_grant,
    ledger_invariant_errors,
    plan_preemption,
    plan_reorganization,
)
from .storage import SchedulerStore
from .utils import canonical_json, crash_hook, digest_value, sha256_text, utc_wall_now


class SchedulerEngine:
    def __init__(self, store: SchedulerStore, bundle: Dict[str, Any]) -> None:
        self.store = store
        self.bundle = bundle

    @classmethod
    def from_sample_dir(cls, sample_dir: str | Path, db_path: str | Path) -> "SchedulerEngine":
        bundle = load_bundle(Path(sample_dir))
        return cls(SchedulerStore(db_path), bundle)

    def initialize_bundle(self) -> Dict[str, Any]:
        bundle_digest = digest_value(self.bundle)
        with self.store.file_lock(), self.store.connection() as conn:
            self.store.init_schema(conn)
            version = self.store.current_version(conn)
            if version > 0:
                row = conn.execute(
                    "SELECT digest FROM input_bundles WHERE name='default'"
                ).fetchone()
                if row is None or row["digest"] != bundle_digest:
                    raise SchedulerError(
                        "database already contains a different input bundle",
                        error_code="E_INPUT_BUNDLE_CHANGED",
                        details={"db_path": str(self.store.db_path)},
                    )
                return {"ok": True, "loaded": False, "identical": True, "ledger_version": version}

            snapshot = self.initial_snapshot()
            snapshot_digest = digest_value(snapshot)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO input_bundles(name, digest, payload_json, loaded_at) "
                "VALUES('default', ?, ?, ?)",
                (bundle_digest, canonical_json(self.bundle), utc_wall_now()),
            )
            conn.execute(
                """
                INSERT INTO ledger_versions(
                    version, round_id, action, parent_version, digest,
                    chain_digest, snapshot_json, created_wall
                ) VALUES(1, 'load', 'load', 0, ?, ?, ?, ?)
                """,
                (
                    snapshot_digest,
                    digest_value(
                        {
                            "parent_chain_digest": "0" * 64,
                            "snapshot_digest": snapshot_digest,
                            "version": 1,
                        }
                    ),
                    canonical_json(snapshot),
                    utc_wall_now(),
                ),
            )
            self.store.set_pointer(conn, 1)
            self.store.add_event(
                conn,
                ledger_version=1,
                round_id="load",
                clock_ms=snapshot["clock_ms"],
                code="LOAD",
                message="validated sample bundle and initialized ledger",
                fields={"cards": len(self.bundle["cards"]), "jobs": len(self.bundle["jobs"])},
            )
            conn.commit()
            return {"ok": True, "loaded": True, "identical": True, "ledger_version": 1}

    def initial_snapshot(self) -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "clock_ms": int(self.bundle["config"]["logical_clock_start_ms"]),
            "config": self.bundle["config"],
            "quotas": self.bundle["quotas"],
            "cards": self.bundle["cards"],
            "jobs": self.bundle["jobs"],
            "injections": self.bundle["injections"],
            "leases": {},
            "job_active_lease": {},
            "quota_usage": {
                group: {"compute_used": 0, "memory_used": 0}
                for group in sorted(self.bundle["quotas"])
            },
            "wait_queue": {},
            "grant_decisions": [],
            "recycle_records": [],
            "reorg_reports": [],
            "preemption_reports": [],
            "refunds": {},
            "rounds": {},
            "sequence": {"lease": 0, "recycle": 0, "refund": 0},
            "pointer_version": 1,
        }

    # ------------------------------------------------------------------
    # Idempotent operation pipeline
    # ------------------------------------------------------------------

    @staticmethod
    def _default_request_key(request: Dict[str, Any]) -> str:
        job_part = request.get("job_id") or request.get("quota_group") or "batch"
        return f"{request['round_id']}:{request['action']}:{job_part}"

    def submit(self, request: Dict[str, Any]) -> Dict[str, Any]:
        request = dict(request)
        if not request.get("request_key"):
            request["request_key"] = self._default_request_key(request)
        request.setdefault("stage", request["action"])
        request_key = request["request_key"]
        request_json = canonical_json(request)
        request_hash = sha256_text(request_json)

        with self.store.file_lock(), self.store.connection() as conn:
            self.store.init_schema(conn)
            self.store.begin_immediate(conn)
            idem_row = self.store.get_idempotent_result(conn, request_key)
            op_row = self.store.get_operation(conn, request_key)

            if idem_row is not None:
                if op_row is not None and op_row["request_hash"] != request_hash:
                    conn.rollback()
                    raise self._hash_mismatch(request_key)
                self._complete_existing(conn, request_key)
                conn.commit()
                result = json.loads(idem_row["result_json"])
                result["idempotent_replay"] = True
                result["request_key"] = request_key
                return result

            if op_row is not None:
                if op_row["request_hash"] != request_hash:
                    conn.rollback()
                    raise self._hash_mismatch(request_key)
                if op_row["status"] == "completed" and op_row["result_json"]:
                    conn.rollback()
                    result = json.loads(op_row["result_json"])
                    result["idempotent_replay"] = True
                    return result
                if op_row["status"] == "applied" and op_row["result_json"]:
                    self._complete_existing(conn, request_key)
                    conn.commit()
                    result = json.loads(op_row["result_json"])
                    result["recovered_applied_operation"] = True
                    return result
                if op_row["status"] == "failed":
                    conn.rollback()
                    raise self._stored_error(op_row)
                if op_row["status"] == "planned":
                    conn.commit()
                    return self._execute_planned(conn, request, request_hash)

            self.store.insert_intent(
                conn,
                request_key=request_key,
                op_id=request_key,
                round_id=request["round_id"],
                action=request["action"],
                job_id=request.get("job_id"),
                request_hash=request_hash,
                request_json=request_json,
            )
            conn.commit()
            crash_hook(f"{request['stage']}:after_intent")
            return self._execute_planned(conn, request, request_hash)

    @staticmethod
    def _hash_mismatch(request_key: str) -> SchedulerError:
        return SchedulerError(
            "same request_key was reused with different parameters",
            error_code="E_IDEMPOTENCY_KEY_PAYLOAD_MISMATCH",
            details={"request_key": request_key},
            exit_code=49,
        )

    @staticmethod
    def _stored_error(op_row: Any) -> SchedulerError:
        try:
            payload = json.loads(op_row["result_json"] or "{}")
            error = payload.get("error", payload)
            return SchedulerError(
                error.get("message", op_row["error_code"] or "stored operation failure"),
                details=error.get("details", {}),
                error_code=error.get("error_code", op_row["error_code"] or "E_SCHEDULER"),
                exit_code=int(error.get("exit_code", 40)),
            )
        except Exception:
            return SchedulerError(
                op_row["error_code"] or "stored operation failure",
                error_code=op_row["error_code"] or "E_SCHEDULER",
            )

    def _complete_existing(self, conn: Any, request_key: str) -> None:
        row = self.store.get_operation(conn, request_key)
        if row is not None and row["status"] != "completed":
            now = utc_wall_now()
            conn.execute(
                """
                UPDATE operations
                   SET status='completed', stage='done', updated_wall=?
                 WHERE request_key=?
                """,
                (now, request_key),
            )
            self.store.add_event(
                conn,
                ledger_version=row["ledger_version"],
                round_id=row["round_id"],
                clock_ms=None,
                code="OPERATION_FINALIZED",
                message="finalized durable ledger commit",
                fields={"request_key": request_key},
            )

    def _execute_planned(
        self, conn: Any, request: Dict[str, Any], request_hash: str
    ) -> Dict[str, Any]:
        request_key = request["request_key"]
        try:
            self.store.begin_immediate(conn)
            base_version = self.store.current_version(conn)
            snapshot = self.store.load_snapshot(conn, base_version)
            consumed = {
                row["injection_id"]
                for row in conn.execute(
                    "SELECT injection_id FROM injection_runs WHERE status='triggered'"
                ).fetchall()
            }
            prepared = self._prepare(request, snapshot, consumed)
            result = prepared["result"]
            result.setdefault("ok", True)
            result["request_key"] = request_key

            invariant_errors = ledger_invariant_errors(snapshot)
            if invariant_errors:
                raise SchedulerError(
                    "ledger invariant validation failed before commit",
                    error_code="E_LEDGER_INVARIANT",
                    details={"errors": invariant_errors},
                    exit_code=50,
                )

            for refund in prepared.get("refund_rows", []):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO refunds(
                        refund_id, job_id, lease_id, generation, quota_group,
                        compute_shares, memory_shares, reason, status,
                        created_clock, detail_json, updated_wall
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        refund["refund_id"],
                        refund["job_id"],
                        refund["lease_id"],
                        int(refund["generation"]),
                        refund["quota_group"],
                        int(refund["compute_shares"]),
                        int(refund["memory_shares"]),
                        refund["reason"],
                        refund.get("status", "open"),
                        int(refund["created_clock"]),
                        canonical_json(refund),
                        utc_wall_now(),
                    ),
                )

            crash_hook(f"{request['stage']}:before_commit")
            result["ledger_version"] = base_version + 1
            version = self.store.commit_snapshot(
                conn,
                base_version=base_version,
                round_id=request["round_id"],
                action=request["action"],
                snapshot=snapshot,
                request_key=request_key,
                result=result,
                event=prepared.get("event"),
            )
            conn.commit()
        except InjectedFault as exc:
            conn.rollback()
            self._persist_failure(conn, request, request_hash, exc)
            raise
        except SchedulerError as exc:
            conn.rollback()
            self._persist_failure(conn, request, request_hash, exc)
            raise
        except Exception as exc:  # defensive boundary for CLI diagnostics
            conn.rollback()
            wrapped = SchedulerError(
                f"internal scheduler error: {type(exc).__name__}: {exc}",
                error_code="E_INTERNAL",
                details={"type": type(exc).__name__},
                exit_code=50,
            )
            self._persist_failure(conn, request, request_hash, wrapped)
            raise wrapped from exc

        crash_hook(f"{request['stage']}:after_commit")
        self._finalize_completed(request_key, version)
        result["ledger_version"] = version
        result["operation_status"] = "completed"
        return result

    def _finalize_completed(self, request_key: str, version: int) -> None:
        with self.store.connection() as conn:
            self.store.begin_immediate(conn)
            row = self.store.get_operation(conn, request_key)
            if row is not None and row["status"] != "completed":
                conn.execute(
                    """
                    UPDATE operations
                       SET status='completed', stage='done', updated_wall=?
                     WHERE request_key=?
                    """,
                    (utc_wall_now(), request_key),
                )
                self.store.add_event(
                    conn,
                    ledger_version=version,
                    round_id=row["round_id"],
                    clock_ms=None,
                    code="OPERATION_COMPLETED",
                    message="operation replay/finalization completed",
                    fields={"request_key": request_key},
                )
            conn.commit()

    def _persist_failure(
        self,
        conn: Any,
        request: Dict[str, Any],
        request_hash: str,
        exc: SchedulerError,
    ) -> None:
        error_payload = exc.to_dict()
        try:
            self.store.begin_immediate(conn)
            injection_id = exc.details.get("injection_id")
            if injection_id:
                inj = self._injection_by_id(request.get("_snapshot"), injection_id)
                conn.execute(
                    """
                    INSERT INTO injection_runs(
                        injection_id, phase, fault_type, round_id, job_id, card_id,
                        status, error_code, result_json, run_count, updated_wall
                    ) VALUES(?, ?, ?, ?, ?, ?, 'triggered', ?, ?, 1, ?)
                    ON CONFLICT(injection_id) DO UPDATE SET
                        status='triggered', error_code=excluded.error_code,
                        result_json=excluded.result_json,
                        run_count=injection_runs.run_count+1,
                        updated_wall=excluded.updated_wall
                    """,
                    (
                        injection_id,
                        exc.details.get("phase", request.get("stage")),
                        exc.details.get("fault_type", "unknown"),
                        request.get("round_id"),
                        request.get("job_id"),
                        exc.details.get("card_id"),
                        exc.error_code,
                        canonical_json({"ok": False, "error": error_payload}),
                        utc_wall_now(),
                    ),
                )
            conn.execute(
                """
                UPDATE operations
                   SET status='failed', stage='error', error_code=?,
                       result_json=?, attempts=attempts+1, updated_wall=?
                 WHERE request_key=?
                """,
                (
                    exc.error_code,
                    canonical_json({"ok": False, "error": error_payload}),
                    utc_wall_now(),
                    request["request_key"],
                ),
            )
            self.store.add_event(
                conn,
                ledger_version=self.store.current_version(conn),
                round_id=request.get("round_id"),
                clock_ms=request.get("now_ms"),
                level="error",
                code=exc.error_code,
                message=exc.message,
                fields=exc.details,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            # The original business exception is more useful than persistence failure.

    def _injection_by_id(self, snapshot: Optional[Dict[str, Any]], injection_id: str):
        if not snapshot:
            return None
        return next(
            (item for item in snapshot.get("injections", []) if item["injection_id"] == injection_id),
            None,
        )

    # ------------------------------------------------------------------
    # Recovery entry point
    # ------------------------------------------------------------------

    def resume(self) -> Dict[str, Any]:
        recovered: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        with self.store.file_lock(), self.store.connection() as conn:
            self.store.init_schema(conn)
            rows = self.store.list_pending_operations(conn)
            for row in rows:
                op = dict(row)
                request = json.loads(op["request_json"])
                try:
                    if op["status"] == "applied":
                        self.store.begin_immediate(conn)
                        self._complete_existing(conn, op["request_key"])
                        conn.commit()
                        recovered.append(
                            {
                                "request_key": op["request_key"],
                                "mode": "finalized_applied_commit",
                                "ledger_version": op["ledger_version"],
                            }
                        )
                    elif op["status"] == "planned":
                        result = self._execute_planned(conn, request, op["request_hash"])
                        recovered.append(
                            {
                                "request_key": op["request_key"],
                                "mode": "replayed_planned_operation",
                                "ledger_version": result.get("ledger_version"),
                            }
                        )
                except SchedulerError as exc:
                    errors.append(
                        {"request_key": op["request_key"], "error": exc.to_dict()}
                    )
        return {
            "ok": not errors,
            "recovered_count": len(recovered),
            "recovered": recovered,
            "errors": errors,
        }

    # ------------------------------------------------------------------
    # Request dispatch and domain mutations
    # ------------------------------------------------------------------

    def _prepare(
        self,
        request: Dict[str, Any],
        snapshot: Dict[str, Any],
        consumed_injections: set[str],
    ) -> Dict[str, Any]:
        action = request["action"]
        now_ms = request.get("now_ms")
        if now_ms is not None:
            self._advance_clock(snapshot, int(now_ms))

        if action == "grant":
            return self._prepare_grant(request, snapshot, consumed_injections)
        if action == "grant_batch":
            return self._prepare_grant_batch(request, snapshot, consumed_injections)
        if action == "renew":
            return self._prepare_renew(request, snapshot, consumed_injections)
        if action == "recover":
            return self._prepare_recover(request, snapshot, consumed_injections)
        if action == "reorganize":
            return self._prepare_reorganize(request, snapshot, consumed_injections)
        if action == "preempt_execute":
            return self._prepare_preempt_execute(request, snapshot, consumed_injections)
        raise ValidationError(
            f"unsupported action: {action}",
            error_code="E_UNSUPPORTED_ACTION",
            details={"action": action},
        )

    @staticmethod
    def _advance_clock(snapshot: Dict[str, Any], now_ms: int) -> None:
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
            raise ValidationError(
                "now_ms must be a non-negative integer",
                error_code="E_BAD_LOGICAL_CLOCK",
                details={"now_ms": now_ms},
            )
        if now_ms < int(snapshot.get("clock_ms", 0)):
            raise SchedulerError(
                "injected logical clock moved backwards",
                error_code="E_LOGICAL_CLOCK_BACKWARD",
                details={"now_ms": now_ms, "clock_watermark_ms": snapshot.get("clock_ms")},
                exit_code=42,
            )
        snapshot["clock_ms"] = now_ms

    def _find_injection(
        self,
        snapshot: Dict[str, Any],
        phase: str,
        fault_type: str,
        request: Dict[str, Any],
        consumed: set[str],
    ) -> Optional[Dict[str, Any]]:
        for injection in sorted(snapshot.get("injections", []), key=lambda item: item["injection_id"]):
            if not injection.get("enabled", True):
                continue
            if injection["phase"] != phase or injection["fault_type"] != fault_type:
                continue
            if injection.get("once", True) and injection["injection_id"] in consumed:
                continue
            target = injection.get("target", {})
            if target.get("round_id") and target["round_id"] != request.get("round_id"):
                continue
            if target.get("job_id") and request.get("job_id") and target["job_id"] != request.get("job_id"):
                continue
            if target.get("card_id") and request.get("card_id") and target["card_id"] != request.get("card_id"):
                continue
            return injection
        return None

    @staticmethod
    def _injected_error(
        injection: Dict[str, Any], message: str, details: Dict[str, Any]
    ) -> InjectedFault:
        merged = {
            "injection_id": injection["injection_id"],
            "phase": injection["phase"],
            "fault_type": injection["fault_type"],
            **details,
        }
        return InjectedFault(
            message,
            details=merged,
            error_code=FAULT_ERROR_CODES[injection["fault_type"]],
            exit_code=58,
        )

    def _prepare_grant(
        self,
        request: Dict[str, Any],
        snapshot: Dict[str, Any],
        consumed: set[str],
    ) -> Dict[str, Any]:
        job_id = request["job_id"]
        job = snapshot["jobs"].get(job_id)
        if job is None:
            raise NotFoundError(f"job not found: {job_id}", details={"job_id": job_id})

        overlap_injection = self._find_injection(
            snapshot, "grant", "interval_overlap", request, consumed
        )
        if overlap_injection is not None:
            self._inject_interval_overlap(snapshot, overlap_injection, job)

        existing_lease_id = snapshot.get("job_active_lease", {}).get(job_id)
        if existing_lease_id and existing_lease_id in snapshot["leases"]:
            lease = snapshot["leases"][existing_lease_id]
            result = {
                "job_id": job_id,
                "granted": True,
                "duplicate_active_lease": True,
                "lease_id": existing_lease_id,
                "generation": lease["generation"],
                "card_id": lease["card_id"],
                "compute_interval": lease["compute_interval"],
                "memory_interval": lease["memory_interval"],
            }
            return {"result": result, "event": {"code": "GRANT_DUPLICATE", "message": job_id}}

        if request.get("card_id"):
            decision = self._evaluate_pinned_card(snapshot, job, request["card_id"])
        else:
            decision = evaluate_grant(snapshot, job)
        if not decision["granted"]:
            snapshot.setdefault("wait_queue", {})[job_id] = {
                "at_clock_ms": snapshot["clock_ms"],
                "round_id": request["round_id"],
                **decision,
            }
            snapshot["grant_decisions"].append(
                {"round_id": request["round_id"], "clock_ms": snapshot["clock_ms"], **decision}
            )
            result = {
                "job_id": job_id,
                "granted": False,
                "decision": decision,
                "constraint": decision.get("constraint"),
            }
            return {
                "result": result,
                "event": {"level": "warning", "code": "GRANT_DENIED", "message": job_id, "fields": decision},
            }

        lease = self._create_lease(snapshot, job, decision, request["round_id"])
        snapshot["grant_decisions"].append(
            {
                "round_id": request["round_id"],
                "clock_ms": snapshot["clock_ms"],
                **decision,
                "lease_id": lease["lease_id"],
                "generation": lease["generation"],
            }
        )
        result = {
            "job_id": job_id,
            "granted": True,
            "lease_id": lease["lease_id"],
            "generation": lease["generation"],
            "card_id": lease["card_id"],
            "compute_interval": lease["compute_interval"],
            "memory_interval": lease["memory_interval"],
            "expires_at_ms": lease["expires_at_ms"],
            "decision": decision,
        }
        return {
            "result": result,
            "event": {
                "code": "GRANT_COMMITTED",
                "message": f"{job_id} -> {lease['card_id']}",
                "fields": {"lease_id": lease["lease_id"], "generation": lease["generation"]},
            },
        }

    def _create_lease(
        self, snapshot: Dict[str, Any], job: Dict[str, Any], decision: Dict[str, Any], round_id: str
    ) -> Dict[str, Any]:
        snapshot["sequence"]["lease"] += 1
        sequence = snapshot["sequence"]["lease"]
        now_ms = int(snapshot["clock_ms"])
        lease_id = f"L{sequence:04d}"
        grace = int(snapshot["config"]["renew_grace_ms"])
        lease = {
            "lease_id": lease_id,
            "job_id": job["job_id"],
            "generation": 1,
            "card_id": decision["card_id"],
            "compute_request": int(job["compute_request"]),
            "memory_request": int(job["memory_request"]),
            "compute_interval": list(decision["compute_interval"]),
            "memory_interval": list(decision["memory_interval"]),
            "priority": job["priority"],
            "priority_rank": job["priority_rank"],
            "quota_group": job["quota_group"],
            "lease_duration_ms": int(job["lease_duration_ms"]),
            "granted_clock_ms": now_ms,
            "expires_at_ms": now_ms + int(job["lease_duration_ms"]),
            "renew_deadline_ms": now_ms + int(job["lease_duration_ms"]) + grace,
            "last_renewed_clock_ms": None,
            "renewal_count": 0,
            "status": "active",
            "grant_round_id": round_id,
        }
        snapshot["leases"][lease_id] = lease
        snapshot["job_active_lease"][job["job_id"]] = lease_id
        usage = snapshot["quota_usage"].setdefault(
            job["quota_group"], {"compute_used": 0, "memory_used": 0}
        )
        usage["compute_used"] += int(job["compute_request"])
        usage["memory_used"] += int(job["memory_request"])
        snapshot.setdefault("rounds", {})[round_id] = {
            "clock_ms": now_ms,
            "last_action": "grant",
        }
        return lease

    def _evaluate_pinned_card(
        self, snapshot: Dict[str, Any], job: Dict[str, Any], card_id: str
    ) -> Dict[str, Any]:
        quota_failure = None
        from .planner import quota_decision

        quota_failure = quota_decision(snapshot, job)
        if quota_failure:
            return {
                "job_id": job["job_id"],
                "granted": False,
                **quota_failure,
                "attempts": [],
                "pinned_card_id": card_id,
            }
        card = snapshot["cards"].get(card_id)
        if card is None:
            raise NotFoundError(
                f"card not found: {card_id}",
                error_code="E_CARD_NOT_FOUND",
                details={"card_id": card_id, "job_id": job["job_id"]},
            )
        placement = card_placement(
            card, snapshot["leases"], job["compute_request"], job["memory_request"]
        )
        attempt = {
            "card_id": card_id,
            "feasible": placement["feasible"],
            "reason": placement["reason"],
            "total_compute": card["total_compute"],
            "total_memory": card["total_memory"],
            "remaining_compute": sum(item["length"] for item in placement["compute_fragments"]),
            "remaining_memory": sum(item["length"] for item in placement["memory_fragments"]),
            "requested_compute": job["compute_request"],
            "requested_memory": job["memory_request"],
            "max_compute_fragment": placement["max_compute_fragment"],
            "max_memory_fragment": placement["max_memory_fragment"],
        }
        if placement["feasible"]:
            return {
                "job_id": job["job_id"],
                "granted": True,
                "constraint": None,
                "card_id": card_id,
                "compute_interval": placement["compute_interval"],
                "memory_interval": placement["memory_interval"],
                "attempts": [attempt],
                "pinned_card_id": card_id,
            }
        reason_to_constraint = {
            "card_compute_insufficient": "card_compute",
            "card_memory_insufficient": "card_memory",
            "card_unsplittable": "card_unsplittable",
            "card_unhealthy": "card_unhealthy",
            "interval_unaligned": "interval_unaligned",
        }
        return {
            "job_id": job["job_id"],
            "granted": False,
            "constraint": reason_to_constraint.get(placement["reason"], placement["reason"]),
            "attempts": [attempt],
            "blocker": attempt,
            "pinned_card_id": card_id,
            "requested_compute": job["compute_request"],
            "requested_memory": job["memory_request"],
        }

    def _inject_interval_overlap(
        self, snapshot: Dict[str, Any], injection: Dict[str, Any], job: Dict[str, Any]
    ) -> None:
        card_id = injection["target"]["card_id"]
        card = snapshot["cards"][card_id]
        granularity = int(card["granularity"])
        request_length = max(granularity, min(int(job["compute_request"]), card["total_compute"]))
        request_length = (request_length // granularity) * granularity or granularity
        end = min(card["total_compute"], request_length)
        fake_common = {
            "generation": 999,
            "job_id": job["job_id"],
            "quota_group": job["quota_group"],
            "compute_request": end,
            "memory_request": min(end, card["total_memory"]),
            "priority": job["priority"],
            "status": "active",
        }
        snapshot["leases"]["__inject_overlap_a"] = {
            **fake_common,
            "lease_id": "__inject_overlap_a",
            "card_id": card_id,
            "compute_interval": [0, end],
            "memory_interval": [0, min(end, card["total_memory"])],
        }
        snapshot["leases"]["__inject_overlap_b"] = {
            **fake_common,
            "lease_id": "__inject_overlap_b",
            "card_id": card_id,
            "compute_interval": [0, end],
            "memory_interval": [granularity, granularity + min(end, card["total_memory"])],
        }
        errors = ledger_invariant_errors(snapshot)
        raise self._injected_error(
            injection,
            "injected compute interval overlap detected",
            {
                "round_id": injection["target"].get("round_id"),
                "card_id": card_id,
                "job_id": job["job_id"],
                "overlap_interval": [0, end],
                "invariant_errors": errors,
            },
        )

    def _prepare_grant_batch(
        self,
        request: Dict[str, Any],
        snapshot: Dict[str, Any],
        consumed: set[str],
    ) -> Dict[str, Any]:
        job_ids = request.get("job_ids") or sorted(snapshot["jobs"])
        max_grants = int(request.get("max_grants") or snapshot["config"]["max_concurrent_grants"])
        results: List[Dict[str, Any]] = []
        newly_granted = 0
        denied = 0
        for job_id in job_ids:
            if newly_granted >= max_grants:
                break
            item_request = {**request, "action": "grant", "job_id": job_id}
            prepared = self._prepare_grant(item_request, snapshot, consumed)
            item_result = prepared["result"]
            results.append(item_result)
            if item_result.get("granted") and not item_result.get("duplicate_active_lease"):
                newly_granted += 1
            if not item_result.get("granted"):
                denied += 1
        result = {
            "round_id": request["round_id"],
            "grants": results,
            "processed_count": len(results),
            "newly_granted_count": newly_granted,
            "denied_count": denied,
            "max_concurrent_grants": max_grants,
        }
        return {
            "result": result,
            "event": {
                "code": "GRANT_BATCH_COMMITTED",
                "message": request["round_id"],
                "fields": {"granted": newly_granted, "denied": denied},
            },
        }

    def _prepare_renew(
        self,
        request: Dict[str, Any],
        snapshot: Dict[str, Any],
        consumed: set[str],
    ) -> Dict[str, Any]:
        job_id = request["job_id"]
        lease_id = snapshot.get("job_active_lease", {}).get(job_id)
        if not lease_id or lease_id not in snapshot["leases"]:
            raise NotFoundError(
                "no active lease for job",
                error_code="E_ACTIVE_LEASE_NOT_FOUND",
                details={"job_id": job_id, "round_id": request["round_id"]},
            )
        lease = snapshot["leases"][lease_id]
        current_generation = int(lease["generation"])
        provided_generation = int(request["generation"])
        if provided_generation != current_generation:
            raise SchedulerError(
                "renew generation does not match active lease",
                error_code="E_GENERATION_MISMATCH",
                exit_code=49,
                details={
                    "job_id": job_id,
                    "lease_id": lease_id,
                    "provided_generation": provided_generation,
                    "current_generation": current_generation,
                    "round_id": request["round_id"],
                },
            )
        now_ms = int(snapshot["clock_ms"])
        if now_ms > int(lease["renew_deadline_ms"]):
            raise SchedulerError(
                "lease has expired beyond renewal grace window",
                error_code="E_LEASE_EXPIRED_BEYOND_GRACE",
                exit_code=48,
                details={
                    "job_id": job_id,
                    "lease_id": lease_id,
                    "now_ms": now_ms,
                    "renew_deadline_ms": lease["renew_deadline_ms"],
                },
            )

        injection = self._find_injection(
            snapshot, "renew", "generation_rollback", request, consumed
        )
        if injection is not None:
            lease["generation"] = max(1, current_generation - 1)
            raise self._injected_error(
                injection,
                "injected renew generation rollback detected",
                {
                    "round_id": request["round_id"],
                    "job_id": job_id,
                    "lease_id": lease_id,
                    "expected_new_generation": current_generation + 1,
                    "rolled_back_generation": lease["generation"],
                },
            )

        old_expiry = int(lease["expires_at_ms"])
        duration = int(lease["lease_duration_ms"])
        if now_ms <= old_expiry:
            new_expiry = old_expiry + duration
        else:
            new_expiry = now_ms + duration
        lease["generation"] = current_generation + 1
        lease["expires_at_ms"] = new_expiry
        lease["renew_deadline_ms"] = new_expiry + int(snapshot["config"]["renew_grace_ms"])
        lease["last_renewed_clock_ms"] = now_ms
        lease["renewal_count"] = int(lease.get("renewal_count", 0)) + 1
        result = {
            "job_id": job_id,
            "renewed": True,
            "lease_id": lease_id,
            "old_generation": current_generation,
            "generation": lease["generation"],
            "old_expires_at_ms": old_expiry,
            "expires_at_ms": new_expiry,
            "renew_deadline_ms": lease["renew_deadline_ms"],
        }
        return {
            "result": result,
            "event": {"code": "RENEW_COMMITTED", "message": lease_id, "fields": result},
        }

    def _recycle_lease(
        self,
        snapshot: Dict[str, Any],
        lease: Dict[str, Any],
        now_ms: int,
        reason: str,
        *,
        trigger: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        snapshot["sequence"]["recycle"] += 1
        recycle_id = f"R{snapshot['sequence']['recycle']:04d}"
        compute_interval = list(lease["compute_interval"])
        memory_interval = list(lease["memory_interval"])
        group = lease["quota_group"]
        usage = snapshot["quota_usage"].setdefault(group, {"compute_used": 0, "memory_used": 0})
        usage["compute_used"] = max(0, int(usage["compute_used"]) - int(lease["compute_request"]))
        usage["memory_used"] = max(0, int(usage["memory_used"]) - int(lease["memory_request"]))
        lease["status"] = "recycled"
        lease["recycled_at_ms"] = now_ms
        lease["recycle_reason"] = reason
        if snapshot.get("job_active_lease", {}).get(lease["job_id"]) == lease["lease_id"]:
            snapshot["job_active_lease"].pop(lease["job_id"], None)
        record = {
            "recycle_id": recycle_id,
            "lease_id": lease["lease_id"],
            "job_id": lease["job_id"],
            "card_id": lease["card_id"],
            "generation_at_recycle": lease["generation"],
            "reason": reason,
            "recycled_at_ms": now_ms,
            "released_compute_interval": compute_interval,
            "released_memory_interval": memory_interval,
            "released_compute_shares": lease["compute_request"],
            "released_memory_shares": lease["memory_request"],
            "quota_group": group,
            "quota_usage_after": dict(usage),
            "impact_scope": {
                "card_id": lease["card_id"],
                "resource_intervals": {
                    "compute": compute_interval,
                    "memory": memory_interval,
                },
                "quota_group": group,
                "job_id": lease["job_id"],
            },
            "trigger": trigger or {},
        }
        snapshot["recycle_records"].append(record)
        return record

    def _prepare_recover(
        self,
        request: Dict[str, Any],
        snapshot: Dict[str, Any],
        consumed: set[str],
    ) -> Dict[str, Any]:
        reason = request.get("reason", "normal_expiry")
        now_ms = int(snapshot["clock_ms"])
        records: List[Dict[str, Any]] = []

        if reason == "normal_expiry":
            candidates = [
                lease
                for lease in snapshot["leases"].values()
                if lease.get("status") == "active"
                and now_ms > int(lease["renew_deadline_ms"])
            ]
            candidates.sort(key=lambda lease: (int(lease["expires_at_ms"]), lease["lease_id"]))
            for lease in candidates:
                records.append(self._recycle_lease(snapshot, lease, now_ms, reason))
        elif reason == "quota_shrink":
            group = request["quota_group"]
            if group not in snapshot["quotas"]:
                raise NotFoundError(
                    "quota group not found",
                    details={"quota_group": group},
                    error_code="E_QUOTA_GROUP_NOT_FOUND",
                )
            new_limit = int(request["compute_limit"])
            old_limit = int(snapshot["quotas"][group]["compute_limit"])
            snapshot["quotas"][group]["compute_limit"] = new_limit
            candidates = [
                lease
                for lease in snapshot["leases"].values()
                if lease.get("status") == "active" and lease["quota_group"] == group
            ]
            candidates.sort(
                key=lambda lease: (
                    -int(lease.get("priority_rank", 99)),
                    -int(lease["lease_id"][1:]) if lease["lease_id"].startswith("L") else 0,
                    lease["lease_id"],
                )
            )
            trigger = {"old_compute_limit": old_limit, "new_compute_limit": new_limit}
            for lease in candidates:
                if int(snapshot["quota_usage"][group]["compute_used"]) <= new_limit:
                    break
                records.append(
                    self._recycle_lease(snapshot, lease, now_ms, reason, trigger=trigger)
                )
        else:
            raise ValidationError(
                f"unsupported recover reason: {reason}",
                error_code="E_BAD_RECOVER_REASON",
                details={"reason": reason},
            )

        injection = self._find_injection(
            snapshot, "recover", "ledger_imbalance", request, consumed
        )
        if injection is not None:
            group = injection.get("target", {}).get("quota_group") or request.get("quota_group")
            if not group:
                target_job_id = injection["target"].get("job_id")
                target_job = snapshot["jobs"].get(target_job_id) if target_job_id else None
                group = target_job["quota_group"] if target_job else sorted(snapshot["quotas"])[0]
            snapshot["quota_usage"][group]["compute_used"] += 1
            errors = ledger_invariant_errors(snapshot)
            raise self._injected_error(
                injection,
                "injected ledger share imbalance detected",
                {
                    "round_id": request["round_id"],
                    "quota_group": group,
                    "card_id": injection["target"].get("card_id"),
                    "job_id": injection["target"].get("job_id"),
                    "invariant_errors": errors,
                },
            )

        reorganization = None
        if request.get("reorg_job_id"):
            reorg_job = snapshot["jobs"].get(request["reorg_job_id"])
            if reorg_job is None:
                raise NotFoundError(
                    f"reorganization job not found: {request['reorg_job_id']}",
                    details={"job_id": request["reorg_job_id"]},
                )
            reorganization = plan_reorganization(
                snapshot,
                reorg_job,
                card_id=request.get("reorg_card_id"),
                max_migrations=int(request.get("max_migrations", 4)),
            )
            snapshot.setdefault("reorg_reports", []).append(reorganization)

        result = {
            "recovered": True,
            "reason": reason,
            "recycled_count": len(records),
            "records": records,
            "clock_ms": now_ms,
        }
        if reorganization is not None:
            result["reorganization"] = reorganization
        return {
            "result": result,
            "event": {
                "code": "RECOVER_COMMITTED",
                "message": f"{reason}:{len(records)}",
                "fields": {
                    "recycled_count": len(records),
                    "reorg_conclusion": reorganization["conclusion"] if reorganization else None,
                },
            },
        }

    def _prepare_reorganize(
        self,
        request: Dict[str, Any],
        snapshot: Dict[str, Any],
        consumed: set[str],
    ) -> Dict[str, Any]:
        job_id = request["job_id"]
        job = snapshot["jobs"].get(job_id)
        if job is None:
            raise NotFoundError(f"job not found: {job_id}", details={"job_id": job_id})
        report = plan_reorganization(
            snapshot,
            job,
            card_id=request.get("card_id"),
            max_migrations=int(request.get("max_migrations", 4)),
        )
        injection = self._find_injection(
            snapshot, "reorganize", "reorg_digest_mismatch", request, consumed
        )
        if injection is not None:
            report["injected_summary_state"] = "digest_mismatch"
            expected_digest = report["report_digest"]
            actual_digest = digest_value(
                {key: value for key, value in report.items() if key != "report_digest"}
            )
            if actual_digest != expected_digest:
                raise self._injected_error(
                    injection,
                    "injected reorganization summary digest mismatch",
                    {
                        "round_id": request["round_id"],
                        "job_id": job_id,
                        "card_id": request.get("card_id"),
                        "expected_digest": expected_digest,
                        "actual_digest": actual_digest,
                    },
                )
        snapshot.setdefault("reorg_reports", []).append(report)
        result = {"reorganized": True, "report": report}
        return {
            "result": result,
            "event": {
                "code": "REORGANIZE_COMMITTED",
                "message": job_id,
                "fields": {"conclusion": report["conclusion"]},
            },
        }

    def plan_preemption_now(
        self, job_id: str, now_ms: int, *, max_victims: Optional[int] = None
    ) -> Dict[str, Any]:
        with self.store.connection() as conn:
            version = self.store.current_version(conn)
            snapshot = self.store.load_snapshot(conn, version)
        self._advance_clock(snapshot, int(now_ms))
        job = snapshot["jobs"].get(job_id)
        if job is None:
            raise NotFoundError(f"job not found: {job_id}", details={"job_id": job_id})
        max_victims = max_victims or int(snapshot["config"]["preemption"]["max_victims"])
        plan = plan_preemption(snapshot, job, int(now_ms), max_victims=max_victims)
        return {"ok": True, "committed": False, "ledger_version": version, "plan": plan}

    def _prepare_preempt_execute(
        self,
        request: Dict[str, Any],
        snapshot: Dict[str, Any],
        consumed: set[str],
    ) -> Dict[str, Any]:
        job_id = request["job_id"]
        job = snapshot["jobs"].get(job_id)
        if job is None:
            raise NotFoundError(f"job not found: {job_id}", details={"job_id": job_id})
        now_ms = int(snapshot["clock_ms"])
        max_victims = int(
            request.get("max_victims") or snapshot["config"]["preemption"]["max_victims"]
        )
        plan = plan_preemption(snapshot, job, now_ms, max_victims=max_victims)
        if request.get("expected_plan_digest") and request["expected_plan_digest"] != plan["plan_digest"]:
            raise SchedulerError(
                "preemption plan digest does not match dry-run plan",
                error_code="E_PREEMPT_PLAN_DIGEST_MISMATCH",
                exit_code=49,
                details={
                    "expected": request["expected_plan_digest"],
                    "actual": plan["plan_digest"],
                },
            )
        if not plan.get("feasible"):
            raise SchedulerError(
                "no feasible preemption chain",
                error_code="E_PREEMPTION_NOT_FEASIBLE",
                exit_code=47,
                details={"plan": plan},
            )

        records = []
        refund_rows = []
        for victim_id in plan["victim_lease_ids"]:
            lease = snapshot["leases"][victim_id]
            record = self._recycle_lease(
                snapshot,
                lease,
                now_ms,
                "preempted",
                trigger={"target_job_id": job_id, "plan_digest": plan["plan_digest"]},
            )
            records.append(record)

        grant_prepared = self._prepare_grant(
            {**request, "action": "grant", "job_id": job_id}, snapshot, consumed
        )
        grant_result = grant_prepared["result"]
        if not grant_result.get("granted"):
            raise SchedulerError(
                "preemption execution failed to grant target after victim recycle",
                error_code="E_PREEMPT_EXECUTION_MISMATCH",
                details={"plan": plan, "grant_result": grant_result},
            )
        decision = grant_result["decision"]
        planned_decision = plan["decision_after"]
        if (
            grant_result["card_id"] != planned_decision.get("card_id")
            or grant_result["compute_interval"] != planned_decision.get("compute_interval")
            or grant_result["memory_interval"] != planned_decision.get("memory_interval")
        ):
            raise SchedulerError(
                "executed preemption differs from simulated plan",
                error_code="E_PREEMPT_EXECUTION_MISMATCH",
                details={"plan": plan, "executed": grant_result},
            )

        for refund in plan["refunds"]:
            snapshot["sequence"]["refund"] += 1
            refund_id = refund["refund_id"]
            refund_record = {
                **refund,
                "created_clock": now_ms,
                "target_job_id": job_id,
                "plan_digest": plan["plan_digest"],
            }
            snapshot["refunds"][refund_id] = refund_record
            refund_rows.append(refund_record)

        snapshot.setdefault("preemption_reports", []).append(
            {"round_id": request["round_id"], "executed": True, **plan}
        )
        result = {
            "executed": True,
            "job_id": job_id,
            "plan_digest": plan["plan_digest"],
            "victim_lease_ids": plan["victim_lease_ids"],
            "recycle_records": records,
            "refunds": refund_rows,
            "grant": grant_result,
        }
        return {
            "result": result,
            "refund_rows": refund_rows,
            "event": {
                "code": "PREEMPT_EXECUTED",
                "message": job_id,
                "fields": {"victims": plan["victim_lease_ids"]},
            },
        }

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        with self.store.connection() as conn:
            version = self.store.current_version(conn)
            if version == 0:
                return {"ok": True, "initialized": False, "ledger_version": 0}
            snapshot = self.store.load_snapshot(conn, version)
            pending = [dict(row) for row in self.store.list_pending_operations(conn)]
            refund_rows = conn.execute("SELECT * FROM refunds ORDER BY refund_id").fetchall()

        cards: Dict[str, Any] = {}
        for card_id in sorted(snapshot["cards"]):
            card = snapshot["cards"][card_id]
            from .planner import free_fragments, occupied_intervals

            compute_busy = occupied_intervals(snapshot["leases"], card_id, "compute")
            memory_busy = occupied_intervals(snapshot["leases"], card_id, "memory")
            compute_fragments = free_fragments(card["total_compute"], compute_busy)
            memory_fragments = free_fragments(card["total_memory"], memory_busy)
            compute_used = sum(end - start for start, end in compute_busy)
            memory_used = sum(end - start for start, end in memory_busy)
            cards[card_id] = {
                "card_id": card_id,
                "health": card["health"],
                "splittable": card["splittable"],
                "granularity": card["granularity"],
                "total_compute": card["total_compute"],
                "total_memory": card["total_memory"],
                "used_compute": compute_used,
                "used_memory": memory_used,
                "remaining_compute": card["total_compute"] - compute_used,
                "remaining_memory": card["total_memory"] - memory_used,
                "compute_fragments": compute_fragments,
                "memory_fragments": memory_fragments,
            }

        leases = {}
        for lease_id in sorted(snapshot["leases"]):
            lease = snapshot["leases"][lease_id]
            leases[lease_id] = {
                "lease_id": lease_id,
                "job_id": lease["job_id"],
                "status": lease["status"],
                "generation": lease["generation"],
                "card_id": lease["card_id"],
                "priority": lease["priority"],
                "quota_group": lease["quota_group"],
                "expires_at_ms": lease["expires_at_ms"],
                "renew_deadline_ms": lease["renew_deadline_ms"],
                "compute_interval": lease["compute_interval"],
                "memory_interval": lease["memory_interval"],
                "recycle_reason": lease.get("recycle_reason"),
            }

        quota_groups = {}
        for group in sorted(snapshot["quotas"]):
            quota = snapshot["quotas"][group]
            used = snapshot["quota_usage"].get(group, {"compute_used": 0, "memory_used": 0})
            quota_groups[group] = {
                "compute_limit": quota["compute_limit"],
                "memory_limit": quota["memory_limit"],
                "compute_used": used["compute_used"],
                "memory_used": used["memory_used"],
                "compute_remaining": quota["compute_limit"] - used["compute_used"],
            }

        return {
            "ok": True,
            "initialized": True,
            "ledger_version": version,
            "clock_ms": snapshot["clock_ms"],
            "cards": cards,
            "leases": leases,
            "quota_groups": quota_groups,
            "open_refund_count": sum(1 for row in refund_rows if row["status"] == "open"),
            "refunds": [
                {key: row[key] for key in row.keys() if key != "detail_json"}
                for row in refund_rows
            ],
            "pending_operations": [
                {
                    "request_key": row["request_key"],
                    "action": row["action"],
                    "status": row["status"],
                    "stage": row["stage"],
                    "ledger_version": row["ledger_version"],
                }
                for row in pending
            ],
            "recycled_count": len(snapshot.get("recycle_records", [])),
            "reorg_report_count": len(snapshot.get("reorg_reports", [])),
        }

    def verify_current_invariants(self) -> Dict[str, Any]:
        with self.store.connection() as conn:
            version = self.store.current_version(conn)
            snapshot = self.store.load_snapshot(conn, version)
        errors = ledger_invariant_errors(snapshot)
        return {"ok": not errors, "ledger_version": version, "errors": errors}
