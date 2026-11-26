from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional

from .contracts import (
    ACTIVE_STATES,
    STATE_ABANDONED,
    STATE_BLOCKED,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_PENDING,
    STATE_RECOVERED,
    STATE_RUNNING,
    event_request_hash,
)
from .crash import maybe_kill
from .errors import ChaosError
from .storage import Database
from .utils import canonical_json, sha256_json, stable_hash_int


TRANSITIONS: Dict[str, Dict[str, str]] = {
    STATE_RUNNING: {
        "blocked": STATE_BLOCKED,
        "completed": STATE_COMPLETED,
        "failed": STATE_FAILED,
    },
    STATE_BLOCKED: {
        "recovered": STATE_RUNNING,
        "failed": STATE_FAILED,
    },
    STATE_FAILED: {
        "recovered": STATE_RECOVERED,
    },
}

REJECTABLE_OBSERVATION_ERRORS = {
    "INVALID_TRANSITION",
    "GENERATION_MISMATCH",
    "GENERATION_REQUIRED",
    "IDENTITY_MISMATCH",
    "BUDGET_EXHAUSTED",
    "OBSERVATION_ORDER_CONFLICT",
}


class Engine:
    def __init__(self, db: Database, bundle: Optional[Mapping[str, Any]] = None) -> None:
        if db.conn is None:
            db.connect()
        self.db = db
        self.conn = db.conn
        self.bundle = bundle
        self.config = json.loads(db.meta("budget_config_json", "{}"))
        self.policy_values = json.loads(db.meta("policy_values_json", "{}"))

    def policy_int(self, key: str, default: int) -> int:
        value = self.policy_values.get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _row_to_dict(row: Any) -> Dict[str, Any]:
        return dict(row)

    def get_experiment(self, experiment_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM experiments WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()
        if row is None:
            raise ChaosError("UNKNOWN_EXPERIMENT", f"unknown experiment: {experiment_id}")
        return self._row_to_dict(row)

    def _advance_watermark(self, conn: Any, clock_ms: int) -> int:
        row = conn.execute("SELECT value FROM meta WHERE key='clock_watermark_ms'").fetchone()
        current = int(row["value"])
        updated = max(current, int(clock_ms))
        if updated != current:
            conn.execute(
                "UPDATE meta SET value=? WHERE key='clock_watermark_ms'",
                (str(updated),),
            )
        return updated

    def _next_decision_seq(self, conn: Any) -> int:
        conn.execute("UPDATE decision_counter SET next_seq=next_seq+1 WHERE id=1")
        row = conn.execute("SELECT next_seq FROM decision_counter WHERE id=1").fetchone()
        return int(row["next_seq"])

    def _record_decision(
        self,
        conn: Any,
        *,
        action: str,
        clock_ms: int,
        result: str,
        evidence: Dict[str, Any],
        idempotency_key: Optional[str] = None,
        experiment_id: Optional[str] = None,
        generation: Optional[int] = None,
        round_id: Optional[str] = None,
        constraint_code: Optional[str] = None,
        request_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        seq = self._next_decision_seq(conn)
        prev_row = conn.execute(
            "SELECT evidence_hash FROM decisions ORDER BY decision_seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = prev_row["evidence_hash"] if prev_row is not None else None
        decision_id = f"DEC-{seq:010d}"
        evidence_core = dict(evidence)
        evidence_core.update(
            {
                "decision_seq": seq,
                "decision_id": decision_id,
                "action": action,
                "result": result,
                "clock_ms": clock_ms,
                "experiment_id": experiment_id,
                "generation": generation,
                "round_id": round_id,
                "constraint_code": constraint_code,
                "prev_hash": prev_hash,
            }
        )
        evidence_hash = sha256_json(evidence_core)
        evidence_core["evidence_hash"] = evidence_hash
        conn.execute(
            """
            INSERT INTO decisions(
                decision_seq, decision_id, idempotency_key, experiment_id, generation,
                round_id, clock_ms, action, result, constraint_code, request_hash,
                evidence_json, prev_hash, evidence_hash
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seq,
                decision_id,
                idempotency_key,
                experiment_id,
                generation,
                round_id,
                clock_ms,
                action,
                result,
                constraint_code,
                request_hash,
                canonical_json(evidence_core),
                prev_hash,
                evidence_hash,
            ),
        )
        if experiment_id is not None:
            conn.execute(
                "UPDATE experiments SET last_decision_seq=?, revision=revision+1 WHERE experiment_id=?",
                (seq, experiment_id),
            )
        return {
            "decision_seq": seq,
            "decision_id": decision_id,
            "evidence_hash": evidence_hash,
            "prev_hash": prev_hash,
            "evidence": evidence_core,
        }

    def _finalize_decision(self, conn: Any, decision: Dict[str, Any]) -> str:
        evidence = decision["evidence"]
        evidence.pop("evidence_hash", None)
        final_hash = sha256_json(evidence)
        evidence["evidence_hash"] = final_hash
        decision["evidence_hash"] = final_hash
        conn.execute(
            "UPDATE decisions SET evidence_json=?, evidence_hash=? WHERE decision_seq=?",
            (canonical_json(evidence), final_hash, decision["decision_seq"]),
        )
        conn.execute(
            "UPDATE budget_ledger SET evidence_hash=? WHERE decision_seq=?",
            (final_hash, decision["decision_seq"]),
        )
        return final_hash

    def _post_ledger(
        self,
        conn: Any,
        *,
        decision_seq: int,
        account_id: str,
        delta_amount: int,
        reason: str,
        idempotency_key: str,
        evidence_hash: str,
    ) -> Dict[str, Any]:
        row = conn.execute(
            "SELECT balance FROM budget_accounts WHERE account_id=?",
            (account_id,),
        ).fetchone()
        if row is None:
            raise ChaosError("ACCOUNT_NOT_FOUND", f"budget account not found: {account_id}")
        before = int(row["balance"])
        after = before + int(delta_amount)
        if after < 0:
            raise ChaosError(
                "BUDGET_EXHAUSTED",
                f"posting {delta_amount} to {account_id} would make balance negative",
                details={
                    "account_id": account_id,
                    "balance_before": before,
                    "delta_amount": delta_amount,
                    "balance_after": after,
                    "reason": reason,
                },
            )
        conn.execute(
            """
            UPDATE budget_accounts
               SET balance=?, version=version+1,
                   total_debited=total_debited + CASE WHEN ? < 0 THEN -? ELSE 0 END,
                   total_credited=total_credited + CASE WHEN ? > 0 THEN ? ELSE 0 END
             WHERE account_id=?
            """,
            (after, delta_amount, delta_amount, delta_amount, delta_amount, account_id),
        )
        conn.execute(
            """
            INSERT INTO budget_ledger(
                decision_seq, account_id, delta_amount, balance_after,
                reason, idempotency_key, evidence_hash
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_seq,
                account_id,
                delta_amount,
                after,
                reason,
                idempotency_key,
                evidence_hash,
            ),
        )
        return {
            "account_id": account_id,
            "balance_before": before,
            "delta_amount": delta_amount,
            "balance_after": after,
            "reason": reason,
        }

    def compute_backoff(
        self,
        experiment_id: str,
        consecutive_failures: int,
        failure_clock_ms: int,
        generation: int,
    ) -> Dict[str, int | str]:
        base = int(self.config["backoff_base_ms"])
        cap = int(self.config["backoff_cap_ms"])
        exponential = base
        for _ in range(max(0, consecutive_failures - 1)):
            exponential = min(cap, exponential * 2)
        jitter_modulus = max(1, base)
        material = canonical_json(
            {
                "experiment_id": experiment_id,
                "consecutive_failures": consecutive_failures,
                "failure_clock_ms": failure_clock_ms,
                "generation": generation,
                "seed_namespace": "controlled-chaos-budget-v1",
            }
        )
        jitter_ms = stable_hash_int(material, jitter_modulus)
        backoff_ms = min(cap, exponential + jitter_ms)
        return {
            "base_ms": base,
            "cap_ms": cap,
            "consecutive_failures": consecutive_failures,
            "exponential_backoff_ms": exponential,
            "jitter_ms": jitter_ms,
            "jitter_modulus_ms": jitter_modulus,
            "backoff_ms": backoff_ms,
            "next_start_clock_ms": failure_clock_ms + backoff_ms,
            "formula": "min(cap, base * 2 ** (failures - 1) + sha256_jitter_mod_base)",
        }

    def evaluate_admission(self, exp: Mapping[str, Any], clock_ms: int) -> Dict[str, Any]:
        active_row = self.conn.execute(
            f"SELECT COUNT(*) AS count FROM experiments WHERE state IN ({','.join('?' for _ in ACTIVE_STATES)})",
            ACTIVE_STATES,
        ).fetchone()
        active_count = int(active_row["count"])
        concurrency_limit = int(self.config["global_concurrency_limit"])
        duration = int(exp["expected_duration_ms"])
        cost_per_ms = int(self.config["cost_per_ms"])
        admission_fee = int(self.config["admission_charge_points"])
        estimated_points = duration * cost_per_ms + admission_fee
        reserved_points = duration * cost_per_ms

        experiment_account = self.conn.execute(
            "SELECT balance FROM budget_accounts WHERE account_id=?",
            (f"experiment:{exp['experiment_id']}:points",),
        ).fetchone()
        round_account = self.conn.execute(
            "SELECT balance FROM budget_accounts WHERE account_id=?",
            (f"round:{exp['round_id']}:logical_time_ms",),
        ).fetchone()

        checks = [
            {
                "constraint": "global_concurrency_limit",
                "limit": concurrency_limit,
                "active_count": active_count,
                "needed_slots": 1,
                "passed": active_count < concurrency_limit,
            },
            {
                "constraint": "experiment_budget",
                "required_points": estimated_points,
                "balance": int(experiment_account["balance"]),
                "passed": int(experiment_account["balance"]) >= estimated_points,
            },
            {
                "constraint": "round_logical_time_budget",
                "required_ms": duration,
                "balance_ms": int(round_account["balance"]),
                "passed": int(round_account["balance"]) >= duration,
            },
        ]
        first_failure = next((check for check in checks if not check["passed"]), None)
        code = None
        if first_failure is not None:
            code = {
                "global_concurrency_limit": "CONCURRENCY_LIMIT_EXHAUSTED",
                "experiment_budget": "EXPERIMENT_BUDGET_INSUFFICIENT",
                "round_logical_time_budget": "ROUND_BUDGET_INSUFFICIENT",
            }[first_failure["constraint"]]
        return {
            "admissible": first_failure is None,
            "constraint_code": code,
            "first_failed_check": first_failure,
            "checks": checks,
            "estimated_points": estimated_points,
            "reserved_points": reserved_points,
            "round_charge_ms": duration,
            "active_count": active_count,
        }

    def try_admit(
        self,
        experiment_id: str,
        clock_ms: int,
        *,
        allow_recovered: bool = False,
        trigger: str = "run",
    ) -> Dict[str, Any]:
        exp = self.get_experiment(experiment_id)
        if exp["state"] in ACTIVE_STATES:
            admission = self.conn.execute(
                "SELECT * FROM admissions WHERE experiment_id=? ORDER BY generation DESC LIMIT 1",
                (experiment_id,),
            ).fetchone()
            decision = None
            if admission is not None:
                decision = self.conn.execute(
                    "SELECT decision_seq, evidence_hash FROM decisions WHERE decision_seq=?",
                    (admission["decision_seq"],),
                ).fetchone()
            return {
                "experiment_id": experiment_id,
                "admitted": False,
                "idempotent": True,
                "state": exp["state"],
                "generation": admission["generation"] if admission else exp["generation"],
                "decision_seq": admission["decision_seq"] if admission else exp["last_decision_seq"],
                "evidence_hash": decision["evidence_hash"] if decision else None,
            }

        if exp["state"] == STATE_ABANDONED:
            raise ChaosError(
                "EXPERIMENT_ABANDONED",
                f"experiment {experiment_id} was abandoned and cannot be admitted",
                details={"first_constraint_code": exp["first_constraint_code"]},
            )
        if exp["state"] in (STATE_COMPLETED,):
            raise ChaosError("INVALID_TRANSITION", f"experiment {experiment_id} is already completed")
        if exp["state"] == STATE_RECOVERED and not allow_recovered:
            return {
                "experiment_id": experiment_id,
                "admitted": False,
                "skipped": True,
                "reason": "RECOVERED_USE_RESUME",
                "backoff_until_ms": exp["backoff_until_ms"],
            }
        if exp["state"] not in (STATE_PENDING, STATE_RECOVERED):
            raise ChaosError("INVALID_TRANSITION", f"experiment {experiment_id} cannot be admitted from {exp['state']}")

        if clock_ms < int(exp["start_clock_ms"]):
            return {
                "experiment_id": experiment_id,
                "admitted": False,
                "skipped": True,
                "reason": "START_CLOCK_IN_FUTURE",
                "start_clock_ms": exp["start_clock_ms"],
            }
        if exp["state"] == STATE_RECOVERED and clock_ms < int(exp["backoff_until_ms"]):
            return {
                "experiment_id": experiment_id,
                "admitted": False,
                "skipped": True,
                "reason": "BACKOFF_WAIT",
                "backoff_until_ms": exp["backoff_until_ms"],
            }

        evaluation = self.evaluate_admission(exp, clock_ms)
        if not evaluation["admissible"]:
            with self.db.transaction() as conn:
                self.conn = conn
                decision = self._record_decision(
                    conn,
                    action="admit",
                    clock_ms=clock_ms,
                    result="rejected",
                    constraint_code=evaluation["constraint_code"],
                    experiment_id=experiment_id,
                    generation=exp["generation"],
                    round_id=exp["round_id"],
                    idempotency_key=f"admit-reject:{experiment_id}:{exp['generation']}:{clock_ms}:{trigger}",
                    evidence={"evaluation": evaluation, "trigger": trigger},
                )
                conn.execute(
                    """
                    UPDATE experiments
                       SET first_constraint_code=?, first_constraint_json=?, updated_clock_ms=?
                     WHERE experiment_id=?
                    """,
                    (
                        evaluation["constraint_code"],
                        canonical_json(evaluation["first_failed_check"]),
                        clock_ms,
                        experiment_id,
                    ),
                )
                self._advance_watermark(conn, clock_ms)
            return {
                "experiment_id": experiment_id,
                "admitted": False,
                "rejected": True,
                "constraint_code": evaluation["constraint_code"],
                "evaluation": evaluation,
                "decision": decision,
            }

        with self.db.transaction() as conn:
            self.conn = conn
            current = conn.execute(
                "SELECT * FROM experiments WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()
            current = dict(current)
            admission_generation = int(current["generation"])
            if current["state"] == STATE_RECOVERED:
                admission_generation += 1

            idempotency_key = f"admit:{experiment_id}:v{admission_generation}"
            existing = conn.execute(
                "SELECT decision_seq, evidence_hash FROM decisions WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return {
                    "experiment_id": experiment_id,
                    "admitted": False,
                    "idempotent": True,
                    "generation": admission_generation,
                    "decision_seq": existing["decision_seq"],
                    "evidence_hash": existing["evidence_hash"],
                }

            evaluation = self.evaluate_admission(current, clock_ms)
            if not evaluation["admissible"]:
                decision = self._record_decision(
                    conn,
                    action="admit",
                    clock_ms=clock_ms,
                    result="rejected",
                    constraint_code=evaluation["constraint_code"],
                    experiment_id=experiment_id,
                    generation=admission_generation,
                    round_id=current["round_id"],
                    idempotency_key=f"admit-reject:{experiment_id}:v{admission_generation}:{clock_ms}:{trigger}:tx",
                    evidence={"evaluation": evaluation, "trigger": trigger, "reevaluated_in_transaction": True},
                )
                conn.execute(
                    """
                    UPDATE experiments
                       SET first_constraint_code=?, first_constraint_json=?, updated_clock_ms=?
                     WHERE experiment_id=?
                    """,
                    (
                        evaluation["constraint_code"],
                        canonical_json(evaluation["first_failed_check"]),
                        clock_ms,
                        experiment_id,
                    ),
                )
                self._advance_watermark(conn, clock_ms)
                return {
                    "experiment_id": experiment_id,
                    "admitted": False,
                    "rejected": True,
                    "constraint_code": evaluation["constraint_code"],
                    "evaluation": evaluation,
                    "decision": decision,
                }

            decision = self._record_decision(
                conn,
                action="admit",
                clock_ms=clock_ms,
                result="accepted",
                experiment_id=experiment_id,
                generation=admission_generation,
                round_id=current["round_id"],
                idempotency_key=idempotency_key,
                evidence={"evaluation": evaluation, "trigger": trigger},
            )
            ledger_effects = []
            experiment_account = f"experiment:{experiment_id}:points"
            round_account = f"round:{current['round_id']}:logical_time_ms"
            admission_fee = int(self.config["admission_charge_points"])
            if admission_fee:
                ledger_effects.append(
                    self._post_ledger(
                        conn,
                        decision_seq=decision["decision_seq"],
                        account_id=experiment_account,
                        delta_amount=-admission_fee,
                        reason="admission_fee",
                        idempotency_key=f"{idempotency_key}:fee",
                        evidence_hash=decision["evidence_hash"],
                    )
                )
            ledger_effects.append(
                self._post_ledger(
                    conn,
                    decision_seq=decision["decision_seq"],
                    account_id=experiment_account,
                    delta_amount=-int(evaluation["reserved_points"]),
                    reason="estimated_duration_reservation",
                    idempotency_key=f"{idempotency_key}:reserve",
                    evidence_hash=decision["evidence_hash"],
                )
            )
            ledger_effects.append(
                self._post_ledger(
                    conn,
                    decision_seq=decision["decision_seq"],
                    account_id=round_account,
                    delta_amount=-int(evaluation["round_charge_ms"]),
                    reason="round_time_reservation",
                    idempotency_key=f"{idempotency_key}:round",
                    evidence_hash=decision["evidence_hash"],
                )
            )
            conn.execute(
                """
                INSERT INTO admissions(
                    experiment_id, generation, decision_seq, clock_ms,
                    charge_points, round_charge_ms, idempotency_key
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    admission_generation,
                    decision["decision_seq"],
                    clock_ms,
                    evaluation["estimated_points"],
                    evaluation["round_charge_ms"],
                    idempotency_key,
                ),
            )
            conn.execute(
                """
                UPDATE experiments
                   SET state=?, disposition='running', outcome_category=?,
                       generation=?, admitted_clock_ms=?, reserved_points=?,
                       reserved_round_ms=?, backoff_until_ms=0,
                       first_constraint_code=NULL, first_constraint_json=NULL,
                       updated_clock_ms=?
                 WHERE experiment_id=?
                """,
                (
                    STATE_RUNNING,
                    "running",
                    admission_generation,
                    clock_ms,
                    evaluation["reserved_points"],
                    evaluation["round_charge_ms"],
                    clock_ms,
                    experiment_id,
                ),
            )
            self._advance_watermark(conn, clock_ms)
            self._finalize_decision(conn, decision)
            maybe_kill("before_admission_commit")

        maybe_kill("after_admission_commit")
        return {
            "experiment_id": experiment_id,
            "admitted": True,
            "generation": admission_generation,
            "decision": decision,
            "ledger_effects": ledger_effects,
        }

    def abandon_candidates(
        self,
        experiment_ids: List[str],
        clock_ms: int,
        constraint_code: str,
        *,
        message: str,
        trigger_experiment_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for experiment_id in experiment_ids:
            exp = self.get_experiment(experiment_id)
            if exp["state"] == STATE_ABANDONED:
                results.append({"experiment_id": experiment_id, "idempotent": True, "state": exp["state"]})
                continue
            if exp["state"] not in (STATE_PENDING, STATE_RECOVERED):
                continue
            with self.db.transaction() as conn:
                self.conn = conn
                idempotency_key = f"abandon:{experiment_id}:v{exp['generation']}"
                existing = conn.execute(
                    "SELECT decision_seq, evidence_hash FROM decisions WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    results.append(
                        {
                            "experiment_id": experiment_id,
                            "idempotent": True,
                            "decision_seq": existing["decision_seq"],
                        }
                    )
                    continue
                decision = self._record_decision(
                    conn,
                    action="abandon",
                    clock_ms=clock_ms,
                    result="voluntarily_abandoned",
                    constraint_code=constraint_code,
                    experiment_id=experiment_id,
                    generation=exp["generation"],
                    round_id=exp["round_id"],
                    idempotency_key=idempotency_key,
                    evidence={
                        "message": message,
                        "trigger_experiment_id": trigger_experiment_id,
                    },
                )
                constraint_detail = {
                    "code": constraint_code,
                    "message": message,
                    "trigger_experiment_id": trigger_experiment_id,
                }
                conn.execute(
                    """
                    UPDATE experiments
                       SET state=?, disposition='voluntarily_abandoned',
                           outcome_category='voluntarily_abandoned',
                           first_constraint_code=?, first_constraint_json=?,
                           updated_clock_ms=?
                     WHERE experiment_id=?
                    """,
                    (
                        STATE_ABANDONED,
                        constraint_code,
                        canonical_json(constraint_detail),
                        clock_ms,
                        experiment_id,
                    ),
                )
                self._advance_watermark(conn, clock_ms)
                results.append(
                    {
                        "experiment_id": experiment_id,
                        "abandoned": True,
                        "constraint_code": constraint_code,
                        "decision_seq": decision["decision_seq"],
                        "evidence_hash": decision["evidence_hash"],
                    }
                )
        return results

    def run_scheduling(
        self,
        clock_ms: int,
        *,
        round_id: Optional[str] = None,
        experiment_id: Optional[str] = None,
        allow_recovered: bool = False,
        abandon_remaining: bool = True,
    ) -> Dict[str, Any]:
        self.recover_pending_inbox()
        if experiment_id is not None:
            result = self.try_admit(
                experiment_id,
                clock_ms,
                allow_recovered=allow_recovered,
                trigger="run_experiment",
            )
            return {"round_id": round_id, "clock_ms": clock_ms, "results": [result]}

        sql = """
            SELECT * FROM experiments
             WHERE state IN (?, ?) AND start_clock_ms <= ?
        """
        params: List[Any] = [STATE_PENDING, STATE_RECOVERED, clock_ms]
        if round_id is not None:
            sql += " AND round_id=?"
            params.append(round_id)
        sql += " ORDER BY priority ASC, start_clock_ms ASC, experiment_id ASC"
        rows = self.conn.execute(sql, params).fetchall()
        candidates = [dict(row) for row in rows]
        if not allow_recovered:
            candidates = [row for row in candidates if row["state"] == STATE_PENDING]
        else:
            candidates = [
                row
                for row in candidates
                if row["state"] == STATE_PENDING or clock_ms >= int(row["backoff_until_ms"])
            ]

        results: List[Dict[str, Any]] = []
        stopped: Optional[Dict[str, Any]] = None
        for index, candidate in enumerate(candidates):
            result = self.try_admit(
                candidate["experiment_id"],
                clock_ms,
                allow_recovered=allow_recovered,
                trigger="scheduler",
            )
            results.append(result)
            if result.get("admitted") or result.get("idempotent"):
                continue
            if result.get("skipped"):
                continue
            constraint_code = result.get("constraint_code")
            if constraint_code and abandon_remaining:
                remaining_ids = [
                    row["experiment_id"]
                    for row in candidates[index:]
                    if row["state"] in (STATE_PENDING, STATE_RECOVERED)
                ]
                abandon_results = self.abandon_candidates(
                    remaining_ids,
                    clock_ms,
                    constraint_code,
                    message="scheduler stopped after first binding constraint",
                    trigger_experiment_id=candidate["experiment_id"],
                )
                stopped = {
                    "constraint_code": constraint_code,
                    "trigger_experiment_id": candidate["experiment_id"],
                    "remaining_experiment_ids": remaining_ids,
                    "abandon_results": abandon_results,
                }
                break

        return {
            "round_id": round_id or "all",
            "clock_ms": clock_ms,
            "candidate_count": len(candidates),
            "results": results,
            "stopped": stopped,
        }

    def submit_observation(self, event: Mapping[str, Any]) -> Dict[str, Any]:
        experiment_id = event["experiment_id"]
        sequence = int(event["sequence"])
        request_hash = event_request_hash(event)
        serializable_event = {key: value for key, value in event.items() if key != "raw"}
        request_json = canonical_json(serializable_event)
        clock_ms = int(event["clock_ms"])

        with self.db.transaction() as conn:
            self.conn = conn
            exp_row = conn.execute(
                "SELECT experiment_id FROM experiments WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()
            if exp_row is None:
                raise ChaosError("UNKNOWN_EXPERIMENT", f"unknown experiment: {experiment_id}")

            existing = conn.execute(
                "SELECT * FROM observation_inbox WHERE experiment_id=? AND sequence=?",
                (experiment_id, sequence),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise ChaosError(
                        "IDEMPOTENCY_CONFLICT",
                        "same experiment/sequence was already submitted with different content",
                        details={"experiment_id": experiment_id, "sequence": sequence},
                    )
                if existing["status"] in ("processed", "rejected"):
                    result = json.loads(existing["result_json"] or "{}")
                    return {
                        "idempotent": True,
                        "status": existing["status"],
                        "decision_seq": existing["decision_seq"],
                        "result": result,
                    }
            else:
                max_row = conn.execute(
                    "SELECT MAX(sequence) AS max_sequence FROM observation_inbox WHERE experiment_id=?",
                    (experiment_id,),
                ).fetchone()
                max_sequence = max_row["max_sequence"]
                if max_sequence is not None and sequence < int(max_sequence):
                    raise ChaosError(
                        "OBSERVATION_ORDER_CONFLICT",
                        "observation sequence is lower than a previously submitted sequence",
                        details={
                            "experiment_id": experiment_id,
                            "sequence": sequence,
                            "max_submitted_sequence": int(max_sequence),
                        },
                    )
                conn.execute(
                    """
                    INSERT INTO observation_inbox(
                        experiment_id, sequence, request_hash, request_json,
                        status, received_clock_ms, updated_clock_ms
                    ) VALUES(?, ?, ?, ?, 'received', ?, ?)
                    """,
                    (experiment_id, sequence, request_hash, request_json, clock_ms, clock_ms),
                )
            self._advance_watermark(conn, clock_ms)

        maybe_kill("after_inbox_commit")
        return self.process_inbox_item(experiment_id, sequence)

    def process_pending_inbox(self, limit: Optional[int] = None) -> Dict[str, Any]:
        sql = """
            SELECT experiment_id, sequence
              FROM observation_inbox
             WHERE status='received'
             ORDER BY experiment_id ASC, sequence ASC
        """
        params: List[Any] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        processed: List[Dict[str, Any]] = []
        for row in rows:
            processed.append(self.process_inbox_item(row["experiment_id"], int(row["sequence"])))
        return {"replayed_count": len(processed), "items": processed}

    def recover_pending_inbox(self, limit: Optional[int] = None) -> Dict[str, Any]:
        return self.process_pending_inbox(limit)

    def process_inbox_item(self, experiment_id: str, sequence: int) -> Dict[str, Any]:
        try:
            with self.db.transaction() as conn:
                self.conn = conn
                result = self._process_observation(conn, experiment_id, sequence)
                maybe_kill("before_process_commit")
            maybe_kill("after_process_commit")
            return result
        except ChaosError as exc:
            if exc.code in REJECTABLE_OBSERVATION_ERRORS:
                self._reject_inbox_item(experiment_id, sequence, exc)
            raise

    def _reject_inbox_item(self, experiment_id: str, sequence: int, error: ChaosError) -> None:
        with self.db.transaction() as conn:
            self.conn = conn
            row = conn.execute(
                "SELECT status FROM observation_inbox WHERE experiment_id=? AND sequence=?",
                (experiment_id, sequence),
            ).fetchone()
            if row is None or row["status"] != "received":
                return
            clock_ms = self.db.watermark()
            decision = self._record_decision(
                conn,
                action="observe",
                clock_ms=clock_ms,
                result="rejected",
                constraint_code=error.code,
                experiment_id=experiment_id,
                idempotency_key=f"observe-reject:{experiment_id}:{sequence}:{error.code}",
                evidence={"error": error.to_payload()["error"]},
            )
            result = {
                "processed": False,
                "rejected": True,
                "error": error.to_payload()["error"],
                "decision_seq": decision["decision_seq"],
            }
            conn.execute(
                """
                UPDATE observation_inbox
                   SET status='rejected', decision_seq=?, updated_clock_ms=?, result_json=?
                 WHERE experiment_id=? AND sequence=?
                """,
                (
                    decision["decision_seq"],
                    clock_ms,
                    canonical_json(result),
                    experiment_id,
                    sequence,
                ),
            )

    def _process_observation(self, conn: Any, experiment_id: str, sequence: int) -> Dict[str, Any]:
        inbox = conn.execute(
            "SELECT * FROM observation_inbox WHERE experiment_id=? AND sequence=?",
            (experiment_id, sequence),
        ).fetchone()
        if inbox is None:
            raise ChaosError("OBSERVATION_NOT_FOUND", "observation inbox record not found")
        if inbox["status"] in ("processed", "rejected"):
            return json.loads(inbox["result_json"] or "{}")

        event = json.loads(inbox["request_json"])
        if event_request_hash(event) != inbox["request_hash"]:
            raise ChaosError("IDEMPOTENCY_CONFLICT", "stored observation hash mismatch")

        exp_row = conn.execute(
            "SELECT * FROM experiments WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()
        if exp_row is None:
            raise ChaosError("UNKNOWN_EXPERIMENT", f"unknown experiment: {experiment_id}")
        exp = dict(exp_row)

        observation_type = event["observation_type"]
        clock_ms = int(event["clock_ms"])
        declared_generation = event.get("generation", event.get("expected_generation"))
        target_service = event.get("target_service")
        if target_service is None and isinstance(event.get("detail"), dict):
            target_service = event["detail"].get("target_service")
        if target_service is not None and target_service != exp["target_service"]:
            raise ChaosError(
                "IDENTITY_MISMATCH",
                "recovery/observation target service does not match experiment identity",
                details={"expected": exp["target_service"], "received": target_service},
            )

        allowed = TRANSITIONS.get(exp["state"], {})
        if observation_type not in allowed:
            raise ChaosError(
                "INVALID_TRANSITION",
                f"observation {observation_type!r} is not valid from state {exp['state']!r}",
                details={
                    "experiment_id": experiment_id,
                    "sequence": sequence,
                    "current_state": exp["state"],
                    "observation_type": observation_type,
                },
            )
        new_state = allowed[observation_type]

        if observation_type == "recovered":
            if declared_generation is None:
                raise ChaosError(
                    "GENERATION_REQUIRED",
                    "recovered observation must declare the expected history generation",
                )
            if int(declared_generation) != int(exp["generation"]):
                raise ChaosError(
                    "GENERATION_MISMATCH",
                    "recovery observation generation does not match persisted history generation",
                    details={
                        "experiment_id": experiment_id,
                        "expected_generation": exp["generation"],
                        "received_generation": declared_generation,
                    },
                )

        phase_charge = max(0, self.policy_int("phase_charge_points", int(self.config.get("phase_charge_points", 0))))
        account_id = f"experiment:{experiment_id}:points"
        account_row = conn.execute(
            "SELECT balance FROM budget_accounts WHERE account_id=?",
            (account_id,),
        ).fetchone()
        if int(account_row["balance"]) < phase_charge:
            raise ChaosError(
                "BUDGET_EXHAUSTED",
                "experiment budget is insufficient for phase progression charge",
                details={"balance": int(account_row["balance"]), "phase_charge": phase_charge},
            )

        idempotency_key = f"observe:{experiment_id}:{sequence}:{inbox['request_hash'][:16]}"
        decision = self._record_decision(
            conn,
            action="observe",
            clock_ms=clock_ms,
            result="accepted",
            experiment_id=experiment_id,
            generation=exp["generation"],
            round_id=exp["round_id"],
            idempotency_key=idempotency_key,
            request_hash=inbox["request_hash"],
            evidence={
                "sequence": sequence,
                "observation_type": observation_type,
                "from_state": exp["state"],
                "to_state": new_state,
                "summary": event.get("summary"),
            },
        )
        effects: List[Dict[str, Any]] = []
        effects.append(
            self._post_ledger(
                conn,
                decision_seq=decision["decision_seq"],
                account_id=account_id,
                delta_amount=-phase_charge,
                reason="phase_progression_charge",
                idempotency_key=f"{idempotency_key}:phase",
                evidence_hash=decision["evidence_hash"],
            )
        )

        settlement: Optional[Dict[str, Any]] = None
        backoff: Optional[Dict[str, Any]] = None
        recovery_rule: Optional[Dict[str, Any]] = None
        outcome_category = exp["outcome_category"]
        if new_state == STATE_BLOCKED:
            outcome_category = STATE_BLOCKED
        elif new_state == STATE_RUNNING:
            outcome_category = STATE_RUNNING

        if new_state in (STATE_COMPLETED, STATE_FAILED):
            admitted_clock = int(exp["admitted_clock_ms"] or exp["start_clock_ms"])
            elapsed_ms = max(0, clock_ms - admitted_clock)
            reserved_points = int(exp["reserved_points"])
            reserved_round_ms = int(exp["reserved_round_ms"])
            billable_ms = min(reserved_round_ms, elapsed_ms)
            cost_per_ms = int(self.config["cost_per_ms"])
            time_charge_points = min(reserved_points, billable_ms * cost_per_ms)
            refund_points = reserved_points - time_charge_points
            round_refund_ms = reserved_round_ms - billable_ms

            if refund_points > 0:
                effects.append(
                    self._post_ledger(
                        conn,
                        decision_seq=decision["decision_seq"],
                        account_id=account_id,
                        delta_amount=refund_points,
                        reason="unused_experiment_budget_settlement",
                        idempotency_key=f"{idempotency_key}:exp-refund",
                        evidence_hash=decision["evidence_hash"],
                    )
                )
            if round_refund_ms > 0:
                effects.append(
                    self._post_ledger(
                        conn,
                        decision_seq=decision["decision_seq"],
                        account_id=f"round:{exp['round_id']}:logical_time_ms",
                        delta_amount=round_refund_ms,
                        reason="unused_round_time_settlement",
                        idempotency_key=f"{idempotency_key}:round-refund",
                        evidence_hash=decision["evidence_hash"],
                    )
                )
            settlement = {
                "admitted_clock_ms": admitted_clock,
                "terminal_clock_ms": clock_ms,
                "elapsed_ms": elapsed_ms,
                "billable_ms": billable_ms,
                "time_charge_points": time_charge_points,
                "phase_charge_points": phase_charge,
                "refund_points": refund_points,
                "round_refund_ms": round_refund_ms,
            }

        consecutive_failures = int(exp["consecutive_failures"])
        if new_state == STATE_FAILED:
            consecutive_failures += 1
            backoff = self.compute_backoff(
                experiment_id,
                consecutive_failures,
                clock_ms,
                int(exp["generation"]),
            )
            outcome_category = "failed_after_start"

        if exp["state"] == STATE_FAILED and new_state == STATE_RECOVERED:
            restore_bps = max(0, min(10000, self.policy_int("recovery_restore_bps", 5000)))
            recovery_fee = max(0, self.policy_int("recovery_fee_points", 0))
            last_charge = int(exp["last_charge_points"] or 0)
            restore_points = (last_charge * restore_bps) // 10000
            if restore_points > 0:
                effects.append(
                    self._post_ledger(
                        conn,
                        decision_seq=decision["decision_seq"],
                        account_id=account_id,
                        delta_amount=restore_points,
                        reason="recovery_budget_restoration",
                        idempotency_key=f"{idempotency_key}:restore",
                        evidence_hash=decision["evidence_hash"],
                    )
                )
            if recovery_fee > 0:
                effects.append(
                    self._post_ledger(
                        conn,
                        decision_seq=decision["decision_seq"],
                        account_id=account_id,
                        delta_amount=-recovery_fee,
                        reason="recovery_budget_charge",
                        idempotency_key=f"{idempotency_key}:recovery-fee",
                        evidence_hash=decision["evidence_hash"],
                    )
                )
            recovery_rule = {
                "restore_bps": restore_bps,
                "recovery_fee_points": recovery_fee,
                "reference_last_charge_points": last_charge,
                "restore_points": restore_points,
            }
            outcome_category = "recovered"

        if new_state == STATE_COMPLETED:
            outcome_category = "completed"

        update_fields: Dict[str, Any] = {
            "state": new_state,
            "disposition": new_state,
            "outcome_category": outcome_category,
            "updated_clock_ms": clock_ms,
        }
        if new_state == STATE_FAILED:
            update_fields["consecutive_failures"] = consecutive_failures
            update_fields["backoff_until_ms"] = int(backoff["next_start_clock_ms"])
            update_fields["last_charge_points"] = int(settlement["time_charge_points"]) + phase_charge
        if new_state == STATE_COMPLETED:
            update_fields["consecutive_failures"] = 0
            update_fields["backoff_until_ms"] = 0
            update_fields["reserved_points"] = 0
            update_fields["reserved_round_ms"] = 0
        if new_state in (STATE_COMPLETED, STATE_FAILED):
            update_fields["reserved_points"] = 0
            update_fields["reserved_round_ms"] = 0

        assignments = ", ".join(f"{key}=?" for key in update_fields)
        params = list(update_fields.values())
        params.append(experiment_id)
        conn.execute(f"UPDATE experiments SET {assignments} WHERE experiment_id=?", params)
        self._advance_watermark(conn, clock_ms)

        result = {
            "processed": True,
            "experiment_id": experiment_id,
            "sequence": sequence,
            "decision_seq": decision["decision_seq"],
            "evidence_hash": decision["evidence_hash"],
            "from_state": exp["state"],
            "state": new_state,
            "ledger_effects": effects,
            "settlement": settlement,
            "backoff": backoff,
            "recovery_rule": recovery_rule,
        }
        decision["evidence"]["ledger_effects"] = effects
        decision["evidence"]["settlement"] = settlement
        decision["evidence"]["backoff"] = backoff
        decision["evidence"]["recovery_rule"] = recovery_rule
        final_hash = self._finalize_decision(conn, decision)
        result["evidence_hash"] = final_hash
        conn.execute(
            """
            UPDATE observation_inbox
               SET status='processed', decision_seq=?, updated_clock_ms=?, result_json=?
             WHERE experiment_id=? AND sequence=?
            """,
            (
                decision["decision_seq"],
                clock_ms,
                canonical_json(result),
                experiment_id,
                sequence,
            ),
        )
        return result

    def resume(
        self,
        clock_ms: Optional[int] = None,
        *,
        experiment_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        inbox_recovery = self.process_pending_inbox()
        admission = None
        if experiment_id is not None:
            exp = self.get_experiment(experiment_id)
            effective_clock = clock_ms if clock_ms is not None else self.db.watermark()
            if exp["state"] in (
                STATE_PENDING,
                STATE_RECOVERED,
                STATE_RUNNING,
                STATE_BLOCKED,
            ):
                admission = self.try_admit(
                    experiment_id,
                    effective_clock,
                    allow_recovered=True,
                    trigger="resume",
                )
        return {
            "clock_ms": clock_ms if clock_ms is not None else self.db.watermark(),
            "inbox_recovery": inbox_recovery,
            "admission": admission,
        }
