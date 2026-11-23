from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Callable

from .config import WorkbenchConfig
from .errors import BusinessError, fail
from .locks import TunnelFileLock
from .payload import generate_payload
from .storage import Storage
from .transport import (
    DeterministicLink,
    EchoTransport,
)

KillHook = Callable[[str, dict[str, Any]], None]


@dataclass
class RoundExecution:
    round_no: int
    profile_version: int
    started_clock_ms: int
    deadline_clock_ms: int
    adaptive_mtu: dict[str, Any]
    outcome: str
    started_fragments: int
    acknowledged: int
    timed_out: int
    idempotent_acks: int
    duplicate_echo_acks: int
    business_code: str | None
    trigger_source: str | None
    ended_clock_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "round_no": self.round_no,
            "profile_version": self.profile_version,
            "started_clock_ms": self.started_clock_ms,
            "deadline_clock_ms": self.deadline_clock_ms,
            "ended_clock_ms": self.ended_clock_ms,
            "adaptive_mtu": self.adaptive_mtu,
            "outcome": self.outcome,
            "started_fragments": self.started_fragments,
            "acknowledged": self.acknowledged,
            "timed_out": self.timed_out,
            "idempotent_acks": self.idempotent_acks,
            "duplicate_echo_acks": self.duplicate_echo_acks,
            "business_code": self.business_code,
            "trigger_source": self.trigger_source,
        }


class ForwardEngine:
    def __init__(
        self,
        storage: Storage,
        config: WorkbenchConfig,
        *,
        abort_event: threading.Event | None = None,
        kill_hook: KillHook | None = None,
    ) -> None:
        self.storage = storage
        self.config = config
        self.policy = storage.policy()
        self.abort_event = abort_event or threading.Event()
        self.kill_hook = kill_hook
        self.link = DeterministicLink()

    def _abort_probe(self, run_id: str) -> bool:
        return self.abort_event.is_set() or self.storage.is_abort_requested(run_id)

    def run_new(
        self,
        *,
        tunnel: str,
        payload_index: int,
        rounds: int = 1,
        transport_override: str | None = None,
        salt: int = 0,
    ) -> dict[str, Any]:
        spec = self.storage.tunnel_spec(tunnel)
        if payload_index < 0 or payload_index >= len(spec.payload_lengths):
            fail(
                "E_PAYLOAD_INDEX",
                "payload index is outside the configured distribution",
                details={"payload_index": payload_index, "available": len(spec.payload_lengths)},
            )
        length = spec.payload_lengths[payload_index]
        payload = generate_payload(tunnel, length, salt)
        with TunnelFileLock(self.storage.lock_dir, tunnel):
            run = self.storage.create_or_get_run(
                tunnel=tunnel,
                payload=payload,
                payload_index=payload_index,
                transport_override=transport_override,
            )
            if run["status"] == "COMPLETED":
                return {
                    "ok": True,
                    "idempotent": True,
                    "message": "run was already completed",
                    "run": run,
                    "rounds": [],
                }
            return self._advance_locked(run["run_id"], rounds=rounds, recovery=False)

    def resume(
        self,
        *,
        tunnel: str,
        rounds: int = 1,
        transport_override: str | None = None,
    ) -> dict[str, Any]:
        with TunnelFileLock(self.storage.lock_dir, tunnel):
            run = self.storage.latest_resumable_run(tunnel)
            if self._abort_probe(run["run_id"]):
                self.storage.abort_run(run["run_id"], run["logical_clock_ms"])
                return {
                    "ok": True,
                    "run": self.storage.get_run(run["run_id"]),
                    "rounds": [],
                    "message": "active abort was requested before resume",
                }
            return self._advance_locked(
                run["run_id"],
                rounds=rounds,
                recovery=True,
                transport_override=transport_override,
            )

    def advance_run(
        self, *, run_id: str, rounds: int = 1, recovery: bool = True
    ) -> dict[str, Any]:
        run = self.storage.get_run(run_id)
        with TunnelFileLock(self.storage.lock_dir, run["tunnel"]):
            return self._advance_locked(run_id, rounds=rounds, recovery=recovery)

    def _advance_locked(
        self,
        run_id: str,
        *,
        rounds: int,
        recovery: bool,
        transport_override: str | None = None,
    ) -> dict[str, Any]:
        round_results: list[RoundExecution] = []
        executed = 0
        limit = rounds if rounds > 0 else 10000
        while executed < limit:
            run = self.storage.get_run(run_id)
            if run["status"] in ("COMPLETED", "FAILED", "ABORTED"):
                break
            if self._abort_probe(run_id):
                self.storage.abort_run(run_id, run["logical_clock_ms"])
                break
            snapshot = self.storage.beginning_round(run_id)
            result = self._execute_round(
                snapshot,
                recovery=recovery,
                transport_override=transport_override,
            )
            round_results.append(result)
            executed += 1
            if result.outcome in ("ABORTED_ACTIVE", "FAILED_AFTER_SEND"):
                break
            if result.outcome == "NOT_STARTED_BUDGET" and result.started_fragments == 0:
                break
            if result.outcome == "COMPLETED":
                break
        final_run = self.storage.get_run(run_id)
        report = {
            "ok": final_run["status"] != "FAILED",
            "run": final_run,
            "rounds": [result.as_dict() for result in round_results],
            "final_outcome": final_run["outcome"],
            "business_code": final_run["business_code"],
            "diagnostic": {
                "trigger_source": final_run["trigger_source"],
                "retransmission_generation": final_run["retransmission_generation"],
                "retransmission_count": final_run["retransmission_count"],
                "profile_version": final_run["bound_profile_version"],
                "current_mtu": final_run["current_mtu"],
            },
        }
        return report

    def _execute_round(
        self,
        snapshot: dict[str, Any],
        *,
        recovery: bool,
        transport_override: str | None,
    ) -> RoundExecution:
        run = snapshot["run"]
        profile = snapshot["profile"]
        round_row = snapshot["round"]
        run_id = run["run_id"]
        round_no = round_row["round_no"]
        logical = run["logical_clock_ms"]
        deadline = round_row["deadline_clock_ms"]
        window = max(0.001, profile.rtt_ms + profile.jitter_ms)
        timeout_wall_ms = window
        spec = snapshot["tunnel"]
        transport_name = transport_override or run["transport"]
        transport = EchoTransport(transport_name, spec.listen_host, spec.listen_port)
        started_fragments = 0
        acknowledged = 0
        timed_out = 0
        idempotent_acks = 0
        duplicate_echo_acks = 0
        deferred = False
        failed = False
        aborted = False
        business_code: str | None = None
        trigger_source: str | None = None
        attempted: set[int] = set()

        while started_fragments < self.policy.per_round_concurrency:
            if self._abort_probe(run_id):
                aborted = True
                break
            inflight = self.storage.list_inflight(run_id)
            retry_candidates = [
                fragment
                for fragment in inflight
                if fragment["seq"] not in attempted
                and fragment["attempts"] - 1 < self.policy.retransmission_budget
            ]
            latest_run = self.storage.get_run(run_id)
            if latest_run["status"] in ("COMPLETED", "FAILED", "ABORTED"):
                break
            new_candidate = latest_run["next_offset"] < latest_run["payload_length"]
            if not retry_candidates and not new_candidate:
                break

            if retry_candidates:
                fragment = retry_candidates[0]
                generation = fragment["attempts"] - 1
                recovery_replay = recovery and fragment["trigger_source"] not in (
                    "ACK_TIMEOUT",
                    "PROFILE_LOSS",
                )
                backoff = 0 if recovery_replay else profile.rtt_ms * (2 ** min(generation, 4))
                send_clock = logical + int(round(backoff)) + self.policy.clock_step_ms
                kind = "retry"
            else:
                fragment = None
                send_clock = logical + self.policy.clock_step_ms
                kind = "new"
                if len(inflight) >= self.policy.per_round_concurrency:
                    deferred = True
                    break

            if send_clock > deadline:
                deferred = True
                break

            logical = send_clock
            if kind == "retry":
                assert fragment is not None
                trigger = "RECOVERY_REPLAY" if recovery else "MISSING_CONFIRMATION"
                try:
                    fragment = self.storage.retry_fragment(
                        run_id=run_id,
                        seq=fragment["seq"],
                        send_clock_ms=send_clock,
                        trigger_source=trigger,
                        recovery_replay=recovery_replay,
                    )
                except BusinessError as exc:
                    if exc.code == "E_RETRANSMIT_EXHAUSTED":
                        failed = True
                        business_code = "E_RETRANSMIT_EXHAUSTED"
                        trigger_source = fragment.get("trigger_source", "MISSING_CONFIRMATION")
                        break
                    raise
            else:
                try:
                    fragment = self.storage.allocate_new_fragment(
                        run_id=run_id,
                        send_clock_ms=send_clock,
                        trigger_source="INITIAL",
                        max_inflight=self.policy.per_round_concurrency,
                    )
                except BusinessError as exc:
                    if exc.code == "E_ABORT_REQUESTED":
                        aborted = True
                        break
                    raise
            attempted.add(fragment["seq"])
            started_fragments += 1
            if self.kill_hook is not None:
                self.kill_hook(
                    "AFTER_INTENT",
                    {
                        "run_id": run_id,
                        "round_no": round_no,
                        "seq": fragment["seq"],
                        "profile_version": fragment["profile_version"],
                        "wire_hex": fragment["wire"].hex(),
                    },
                )
            drop = self.link.should_drop(
                run_id=run_id,
                profile_version=fragment["profile_version"],
                sequence=fragment["seq"],
                generation=fragment["retransmission_generation"],
                loss_rate=profile.loss_rate,
            )
            if drop:
                result = transport.wait_without_send(
                    timeout_ms=timeout_wall_ms,
                    abort_probe=lambda: self._abort_probe(run_id),
                )
            else:
                result = transport.send(
                    fragment["wire"],
                    timeout_ms=timeout_wall_ms,
                    abort_probe=lambda: self._abort_probe(run_id),
                )
            if result.status == "ABORTED":
                aborted = True
                trigger_source = "ACTIVE_ABORT"
                break
            if result.status == "TIMEOUT":
                drop_trigger = "PROFILE_LOSS" if drop else "ACK_TIMEOUT"
                timeout_clock = send_clock + int(round(window))
                timeout_result = self.storage.record_timeout(
                    run_id=run_id,
                    seq=fragment["seq"],
                    timeout_clock_ms=timeout_clock,
                    trigger_source=drop_trigger,
                )
                logical = max(logical, timeout_clock)
                timed_out += 1
                trigger_source = drop_trigger
                if timeout_result["exhausted"]:
                    failed = True
                    business_code = "E_RETRANSMIT_EXHAUSTED"
                    break
                continue
            if result.status == "ERROR":
                failed = True
                business_code = "E_ECHO_FAILURE"
                trigger_source = result.error or "ECHO_ERROR"
                failure_clock = send_clock + int(round(window))
                self.storage.fail_run(
                    run_id=run_id,
                    business_code=business_code,
                    trigger_source=trigger_source,
                    clock_ms=failure_clock,
                    retransmission_generation=fragment["retransmission_generation"],
                )
                logical = max(logical, failure_clock)
                break
            response = result.response or {}
            if response.get("duplicate"):
                duplicate_echo_acks += 1
            if self.kill_hook is not None:
                self.kill_hook(
                    "AFTER_ACK_BEFORE_COMMIT",
                    {
                        "run_id": run_id,
                        "round_no": round_no,
                        "seq": fragment["seq"],
                        "fragment_digest": response.get("fragment_digest"),
                    },
                )
            summary = response.get("summary")
            ack_clock = send_clock + int(round(profile.rtt_ms))
            confirmation = self.storage.confirm_fragment(
                run_id=run_id,
                seq=fragment["seq"],
                ack_clock_ms=ack_clock,
                fragment_digest=response.get("fragment_digest", ""),
                receiver_summary=summary if isinstance(summary, dict) else None,
            )
            logical = max(logical, ack_clock)
            if confirmation["idempotent"]:
                idempotent_acks += 1
            else:
                acknowledged += 1
            if confirmation.get("business_code"):
                failed = True
                business_code = confirmation["business_code"]
                trigger_source = "RECEIVER_DIGEST"
                break
            if confirmation["run"]["status"] == "COMPLETED":
                break

        latest = self.storage.get_run(run_id)
        if aborted:
            latest = self.storage.abort_run(run_id, logical)
            outcome = "ABORTED_ACTIVE"
            business_code = "E_ACTIVE_ABORT"
            trigger_source = "ACTIVE_ABORT"
        elif failed or latest["status"] == "FAILED":
            outcome = "FAILED_AFTER_SEND"
            business_code = latest["business_code"] or business_code or "E_SEND_FAILED"
            trigger_source = trigger_source or latest["trigger_source"]
        elif latest["status"] == "COMPLETED":
            outcome = "COMPLETED"
            business_code = None
        else:
            outcome = "NOT_STARTED_BUDGET"
            business_code = "E_BUDGET_DEFERRED"
            if not deferred:
                if started_fragments >= self.policy.per_round_concurrency:
                    trigger_source = "ROUND_CONCURRENCY_REACHED"
                else:
                    triggered = latest["trigger_source"]
                    trigger_source = triggered or "ROUND_LIMIT"
        final = self.storage.finish_round(
            run_id=run_id,
            round_no=round_no,
            outcome=outcome,
            ended_clock_ms=logical,
            business_code=business_code,
            trigger_source=trigger_source,
        )
        return RoundExecution(
            round_no=round_no,
            profile_version=profile.version,
            started_clock_ms=round_row["started_clock_ms"],
            deadline_clock_ms=deadline,
            adaptive_mtu=snapshot["adaptation"],
            outcome=outcome,
            started_fragments=started_fragments,
            acknowledged=acknowledged,
            timed_out=timed_out,
            idempotent_acks=idempotent_acks,
            duplicate_echo_acks=duplicate_echo_acks,
            business_code=final["business_code"],
            trigger_source=final["trigger_source"],
            ended_clock_ms=final["logical_clock_ms"],
        )


def install_test_kill_hook() -> KillHook | None:
    point = os.environ.get("TWB_TEST_KILL_POINT")
    if not point:
        return None
    try:
        count = int(os.environ.get("TWB_TEST_KILL_COUNT", "1"))
    except ValueError:
        count = 1
    seen = {"count": 0}

    def hook(event: str, payload: dict[str, Any]) -> None:
        if event != point:
            return
        seen["count"] += 1
        if seen["count"] >= count:
            os._exit(137)

    return hook

