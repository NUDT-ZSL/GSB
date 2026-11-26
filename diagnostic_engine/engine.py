from __future__ import annotations

import json
import os
import shutil
import signal
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_dumps, deterministic_payload, sha256_hex, signature, write_stable_json
from .clock import LogicalClockDriver
from .errors import DiagnosticError, ErrorCode, InjectedForceKill, fault_error_code
from .ledger import Ledger
from .validation import FixtureBundle


POLICY_ALIASES = {
    "metadata_first": "metadata_first",
    "data_first": "metadata_first",
    "direct": "metadata_first",
    "direct_reversed": "direct_reversed",
    "metadata_before_data": "direct_reversed",
    "write_log": "write_log",
    "log": "write_log",
}


def normalize_policy(policy: str) -> str:
    if policy not in POLICY_ALIASES:
        raise DiagnosticError(
            ErrorCode.CLI_INVALID_ARGUMENT,
            "unsupported policy",
            details={"policy": policy, "allowed": sorted(set(POLICY_ALIASES))},
        )
    return POLICY_ALIASES[policy]


class WritePathEngine:
    def __init__(
        self,
        bundle: FixtureBundle,
        state_dir: str | Path,
        policy: str = "metadata_first",
        *,
        run_id: str = "fixed-seed-run",
        worker_id: str | None = None,
        fault_case: dict[str, Any] | None = None,
        hard_kill: bool = False,
        clock_driver: LogicalClockDriver | None = None,
    ) -> None:
        self.bundle = bundle
        self.requested_policy = policy
        self.policy = normalize_policy(policy)
        self.order = "metadata_then_data" if self.policy == "direct_reversed" else "data_then_metadata"
        self.run_id = run_id
        self.fault_case = dict(fault_case) if fault_case else None
        self.hard_kill = hard_kill
        self.clock = clock_driver or LogicalClockDriver(bundle.clock_profile)
        self.ledger = Ledger(state_dir, run_id=run_id, policy=self.policy, worker_id=worker_id)
        self.state_dir = self.ledger.state_dir
        self.trash_dir = self.state_dir / "trash"
        self.trash_dir.mkdir(parents=True, exist_ok=True)

    @property
    def worker_id(self) -> str:
        return self.ledger.worker_id

    def _clock_ms(self, clock: int) -> int:
        return self.bundle.clock_profile.clock_to_ms(clock)

    def _payload(self, event: dict[str, Any]) -> bytes:
        if event["op"] == "DELETE":
            return b""
        return deterministic_payload(
            self.bundle.seed,
            event["seq"],
            event["key"],
            event["content_version"],
            int(event["payload_size"]),
        )

    def _relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.state_dir).as_posix()
        except ValueError:
            return str(path)

    def _final_path(self, event: dict[str, Any]) -> Path:
        data_version = event.get("target_content_version") if event["op"] == "DELETE" else event["content_version"]
        data_version = data_version or event["content_version"]
        return self.ledger.objects_dir / event["key"] / f"v{data_version}.bin"

    def _temp_path(self, event: dict[str, Any]) -> Path:
        safe_worker = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in self.worker_id)
        return self.ledger.tmp_dir / f"event-{event['seq']}.{safe_worker}.part"

    def _trash_path(self, event: dict[str, Any]) -> Path:
        data_version = event.get("target_content_version") if event["op"] == "DELETE" else event["content_version"]
        data_version = data_version or event["content_version"]
        return self.trash_dir / event["key"] / f"v{data_version}.deleted"

    def _fault_matches(self, stage: str, event: dict[str, Any]) -> dict[str, Any] | None:
        fault = self.fault_case
        if not fault or int(fault.get("event_seq", -1)) != int(event["seq"]):
            return None
        if fault.get("stage") != stage:
            return None
        return fault

    def _orphan_record(self, event: dict[str, Any], pending: dict[str, Any] | None, *, status: str,
                       recovery_action: str | None = None) -> dict[str, Any]:
        version = event["content_version"]
        return {
            "orphan_id": f"orphan:{event['key']}:v{version}:seq{event['seq']}",
            "object_key": event["key"],
            "content_version": version,
            "event_seq": event["seq"],
            "logical_clock": event["logical_clock"],
            "expected_signature": event["expected_signature"],
            "detected_stage": (pending or {}).get("stage"),
            "data_path": (pending or {}).get("final_path") or self._relative(self._final_path(event)),
            "injection_id": (pending or {}).get("injection_id"),
            "status": status,
            "recovery_action": recovery_action,
        }

    def _dirty_record(self, event: dict[str, Any], pending: dict[str, Any] | None, *, status: str,
                      reason: str, recovery_action: str | None = None) -> dict[str, Any]:
        version = event["content_version"]
        return {
            "dirty_id": f"dirty:{event['key']}:v{version}:seq{event['seq']}",
            "object_key": event["key"],
            "content_version": version,
            "event_seq": event["seq"],
            "logical_clock": event["logical_clock"],
            "expected_signature": event["expected_signature"],
            "detected_stage": (pending or {}).get("stage"),
            "reason": reason,
            "injection_id": (pending or {}).get("injection_id"),
            "status": status,
            "recovery_action": recovery_action,
        }

    @staticmethod
    def _upsert_record(collection: list[dict[str, Any]], record: dict[str, Any], id_field: str) -> bool:
        for index, existing in enumerate(collection):
            if existing.get(id_field) == record[id_field]:
                collection[index] = {**existing, **record}
                return False
        collection.append(record)
        return True

    def _start_event(self, event: dict[str, Any]) -> dict[str, Any]:
        seq = int(event["seq"])

        def mutate(doc: dict[str, Any], connection: Any) -> dict[str, Any]:
            if seq in doc.get("completed_seqs", []):
                return {"status": "already_completed"}
            key = str(seq)
            if key not in doc["pending_operations"]:
                doc["pending_operations"][key] = {
                    "event_seq": seq,
                    "policy": self.policy,
                    "order": self.order,
                    "injection_id": (self.fault_case or {}).get("injection_id"),
                    "stage": "plan",
                    "metadata_committed": False,
                    "data_finalized": False,
                    "data_removed": False,
                    "log_entry_complete": False,
                    "temp_complete": False,
                    "stage_history": [
                        {"stage": "plan", "status": "started", "logical_clock": event["logical_clock"]}
                    ],
                }
            doc["status"] = "running"
            doc["clock_watermark"] = max(int(doc.get("clock_watermark", 0)), int(event["logical_clock"]))
            return {"status": "started"}

        return self.ledger.update(mutate, logical_clock=event["logical_clock"])

    def _update_pending(self, seq: int, **changes: Any) -> None:
        clock = int(changes.pop("_clock", 0))

        def mutate(doc: dict[str, Any], connection: Any) -> None:
            pending = doc["pending_operations"].get(str(seq))
            if pending is None:
                return
            new_stage = changes.get("stage")
            if new_stage and new_stage != pending.get("stage"):
                pending.setdefault("stage_history", []).append(
                    {"stage": new_stage, "status": "entered", "logical_clock": clock}
                )
            pending.update(changes)
            doc["clock_watermark"] = max(int(doc.get("clock_watermark", 0)), clock)

        self.ledger.update(mutate, logical_clock=clock)

    def _raise_fault(self, stage: str, event: dict[str, Any], *, mark_orphan: bool = False,
                     mark_dirty: bool = False, dirty_reason: str = "metadata_pointer_without_readable_data",
                     log_outcome: str | None = None, extra_details: dict[str, Any] | None = None) -> None:
        fault = self._fault_matches(stage, event)
        if fault is None:
            return
        fault_type = str(fault["fault_type"])
        code = fault_error_code(fault_type)
        details = {"fault_type": fault_type, "timing": fault.get("timing", "unspecified"),
                   "hard_kill_requested": bool(self.hard_kill)}
        if extra_details:
            details.update(extra_details)
        error = DiagnosticError(
            code, f"injected {fault_type} at {stage}", details=details, stage=stage,
            object_key=event["key"], logical_clock=event["logical_clock"],
            expected_signature=event["expected_signature"], injection_id=fault.get("injection_id"),
        )
        evidence = error.evidence()

        def mutate(doc: dict[str, Any], connection: Any) -> None:
            pending = doc["pending_operations"].get(str(event["seq"]))
            if pending is not None:
                pending["stage"] = stage
                pending["last_fault"] = evidence
                pending.setdefault("fault_timeline", []).append(evidence)
            if mark_orphan:
                record = self._orphan_record(event, pending, status="open")
                if self._upsert_record(doc.setdefault("orphans", []), record, "orphan_id"):
                    doc["counts"]["orphan_object"] = int(doc["counts"].get("orphan_object", 0)) + 1
            if mark_dirty:
                record = self._dirty_record(event, pending, status="open", reason=dirty_reason)
                if self._upsert_record(doc.setdefault("dirty", []), record, "dirty_id"):
                    doc["counts"]["dirty_cache"] = int(doc["counts"].get("dirty_cache", 0)) + 1
            if log_outcome:
                outcome = {"event_seq": event["seq"], "outcome": log_outcome, "stage": stage,
                           "injection_id": fault.get("injection_id"), "logical_clock": event["logical_clock"]}
                if outcome not in doc.setdefault("log_outcomes", []):
                    doc["log_outcomes"].append(outcome)

        self.ledger.update(mutate, logical_clock=event["logical_clock"])
        evidence_path = self.ledger.evidence_dir / f"{fault.get('injection_id', 'fault')}-seq{event['seq']}.json"
        write_stable_json(evidence_path, {"ok": False, "error": evidence})

        if fault_type == "force_kill" and self.hard_kill:
            os.sync()
            try:
                os.kill(os.getpid(), signal.SIGKILL)
            finally:
                os._exit(137)
        if fault_type == "force_kill":
            raise InjectedForceKill(
                code, error.message, details=error.details, stage=stage, object_key=event["key"],
                logical_clock=event["logical_clock"], expected_signature=event["expected_signature"],
                injection_id=error.injection_id,
            )
        raise error

    def _write_payload_file(self, event: dict[str, Any], stage: str) -> tuple[Path, bytes]:
        seq = int(event["seq"])
        payload = self._payload(event)
        temp_path = self._temp_path(event)
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        fault = self._fault_matches(stage, event)

        if fault and fault.get("fault_type") in {"generation_rollback", "log_lost"}:
            self._update_pending(seq, stage=stage, _clock=event["logical_clock"])
            self._raise_fault(stage, event, extra_details={"attempted_generation": 0})

        self._update_pending(
            seq, stage=stage, temp_path=self._relative(temp_path), temp_complete=False,
            payload_size=len(payload), _clock=event["logical_clock"],
        )
        kill_or_interrupt = bool(fault and fault.get("fault_type") in {"write_interrupt", "force_kill"})
        if kill_or_interrupt:
            cut = max(1, len(payload) // 2) if payload else 0
            with temp_path.open("wb") as handle:
                handle.write(payload[:cut])
                handle.flush()
                os.fsync(handle.fileno())
            self._update_pending(seq, temp_bytes=cut, temp_complete=False, _clock=event["logical_clock"])
            self._raise_fault(
                stage, event,
                extra_details={"partial_bytes": cut, "target_bytes": len(payload)},
            )

        with temp_path.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        actual_signature = signature(payload)
        if event["op"] in {"WRITE", "UPDATE"} and actual_signature != event["expected_signature"]:
            raise DiagnosticError(
                ErrorCode.SIGNATURE_MISMATCH,
                "deterministic payload signature does not match event signature",
                stage=stage, object_key=event["key"], logical_clock=event["logical_clock"],
                expected_signature=event["expected_signature"],
                details={"actual_signature": actual_signature},
            )
        self._update_pending(seq, temp_bytes=len(payload), temp_complete=True, _clock=event["logical_clock"])
        return temp_path, payload

    def _atomic_write_data(self, event: dict[str, Any], write_stage: str = "data_staging") -> Path:
        seq = int(event["seq"])
        temp_path, _payload = self._write_payload_file(event, write_stage)
        final_path = self._final_path(event)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        rename_fault = self._fault_matches("data_rename", event)
        next_stage = "data_rename" if rename_fault else ("data_rename" if write_stage == "data_staging" else write_stage)
        self._update_pending(seq, stage=next_stage, final_path=self._relative(final_path),
                            _clock=event["logical_clock"])
        os.replace(temp_path, final_path)
        directory_fd = os.open(final_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        self._update_pending(seq, data_finalized=True, final_path=self._relative(final_path),
                            _clock=event["logical_clock"])
        if rename_fault:
            self._raise_fault("data_rename", event, mark_orphan=True)
        return final_path

    def _verify_final_payload(self, event: dict[str, Any]) -> bool:
        path = self._final_path(event)
        if not path.is_file():
            return False
        if event["op"] == "DELETE":
            return True
        data = path.read_bytes()
        return signature(data) == event["expected_signature"] and len(data) == int(event["payload_size"])

    def _atomic_write_data_without_fault(self, event: dict[str, Any]) -> None:
        old_fault = self.fault_case
        self.fault_case = None
        try:
            self._atomic_write_data(event, "data_staging")
        finally:
            self.fault_case = old_fault

    def _ensure_write_data(self, event: dict[str, Any]) -> str:
        if self._verify_final_payload(event):
            return "verified_existing_data"
        final_path = self._final_path(event)
        if final_path.exists():
            final_path.unlink()
        document = self.ledger.read()
        pending = document.get("pending_operations", {}).get(str(event["seq"]), {})
        candidate_paths = [self._temp_path(event)]
        if pending.get("temp_path"):
            candidate_paths.insert(0, self.state_dir / str(pending["temp_path"]))
        for temp_path in candidate_paths:
            if not temp_path.exists():
                continue
            try:
                data = temp_path.read_bytes()
                if event["op"] != "DELETE" and signature(data) == event["expected_signature"]:
                    final_path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(temp_path, final_path)
                    return "renamed_complete_temp"
                if temp_path != self._temp_path(event):
                    temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        self._atomic_write_data_without_fault(event)
        return "rewrote_data_from_deterministic_fixture"

    def _perform_delete_data(self, event: dict[str, Any]) -> str:
        seq = int(event["seq"])
        final_path = self._final_path(event)
        trash_path = self._trash_path(event)
        self._update_pending(
            seq, stage="data_deletion", final_path=self._relative(final_path),
            trash_path=self._relative(trash_path), _clock=event["logical_clock"],
        )
        if final_path.exists():
            trash_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(final_path, trash_path)
            self._update_pending(seq, data_removed=True, _clock=event["logical_clock"])
            return "moved_data_to_trash"
        self._update_pending(seq, data_removed=True, _clock=event["logical_clock"])
        return "delete_already_committed" if trash_path.exists() else "data_already_absent"

    def _commit_early_metadata(self, event: dict[str, Any]) -> None:
        seq = int(event["seq"])
        fault = self._fault_matches("metadata_commit", event)
        if fault and fault.get("fault_type") in {"write_interrupt", "generation_rollback"}:
            self._update_pending(seq, stage="metadata_commit", _clock=event["logical_clock"])
            if fault["fault_type"] == "write_interrupt":
                artifact = self.ledger.tmp_dir / f"metadata-commit-seq{seq}.partial"
                artifact.write_text(canonical_dumps({"seq": seq, "partial": True})[:96], encoding="utf-8")
            self._raise_fault(
                "metadata_commit", event,
                extra_details={"current_generation": 1, "attempted_generation": 0},
            )

        def mutate(doc: dict[str, Any], connection: Any) -> None:
            pending = doc["pending_operations"].get(str(seq))
            if pending is not None:
                pending["stage"] = "metadata_commit"
                pending["metadata_committed"] = True
                pending["early_metadata"] = True
            doc.setdefault("cache", {})[event["key"]] = {
                "object_key": event["key"],
                "content_version": event["content_version"],
                "expected_signature": event["expected_signature"],
                "logical_clock": event["logical_clock"],
                "absolute_clock_ms": self._clock_ms(event["logical_clock"]),
                "cache_generation": 1,
                "policy": self.policy,
                "last_event_seq": seq,
                "state": "tombstone" if event["op"] == "DELETE" else "dirty",
                "data_present": False,
                "tombstone": event["op"] == "DELETE",
            }

        self.ledger.update(mutate, logical_clock=event["logical_clock"])
        self._raise_fault(
            "metadata_commit", event,
            mark_dirty=event["op"] != "DELETE",
            mark_orphan=event["op"] == "DELETE",
            dirty_reason="early_tombstone_visible_but_data_not_deleted",
        )

    def _finalize_event(self, event: dict[str, Any], *, crash_verdict: str | None = None,
                        log_outcome: str | None = None, recovery_action: str | None = None,
                        replay_summary: dict[str, Any] | None = None) -> dict[str, Any]:
        seq = int(event["seq"])
        result: dict[str, Any] = {}

        def mutate(doc: dict[str, Any], connection: Any) -> None:
            pending = doc["pending_operations"].get(str(seq), {})
            claimed, existing_claim = self.ledger.claim_object_version(
                connection, object_key=event["key"], version=int(event["content_version"]),
                event_seq=seq, logical_clock=event["logical_clock"],
                expected_signature=event["expected_signature"],
            )
            if not claimed:
                doc["counts"]["duplicate_skips"] = int(doc["counts"].get("duplicate_skips", 0)) + 1
                doc["pending_operations"].pop(str(seq), None)
                result.update({"status": "duplicate_claim", "existing_claim": existing_claim})
                return
            inserted = self.ledger.complete_event_once(
                connection, event=event, verdict=crash_verdict or "double_success"
            )
            if not inserted:
                result["status"] = "duplicate_completion"
                return

            doc.setdefault("cache", {})[event["key"]] = {
                "object_key": event["key"],
                "content_version": event["content_version"],
                "expected_signature": event["expected_signature"],
                "logical_clock": event["logical_clock"],
                "absolute_clock_ms": self._clock_ms(event["logical_clock"]),
                "cache_generation": 1,
                "policy": self.policy,
                "last_event_seq": seq,
                "state": "tombstone" if event["op"] == "DELETE" else "clean",
                "data_present": event["op"] != "DELETE",
                "tombstone": event["op"] == "DELETE",
                "data_path": self._relative(self._final_path(event)) if event["op"] != "DELETE" else None,
            }

            if crash_verdict == "orphan_object":
                record = self._orphan_record(event, pending, status="reconciled", recovery_action=recovery_action)
                if self._upsert_record(doc.setdefault("orphans", []), record, "orphan_id"):
                    doc["counts"]["orphan_object"] = int(doc["counts"].get("orphan_object", 0)) + 1
                else:
                    record["status"] = "reconciled"
                    record["recovery_action"] = recovery_action
            elif crash_verdict == "dirty_cache":
                record = self._dirty_record(
                    event, pending, status="reconciled",
                    reason="recovered_uncommitted_metadata_window", recovery_action=recovery_action,
                )
                if self._upsert_record(doc.setdefault("dirty", []), record, "dirty_id"):
                    doc["counts"]["dirty_cache"] = int(doc["counts"].get("dirty_cache", 0)) + 1
                else:
                    record["status"] = "reconciled"
                    record["recovery_action"] = recovery_action

            if log_outcome == "complete":
                doc["counts"]["log_complete"] = int(doc["counts"].get("log_complete", 0)) + 1
            elif log_outcome == "half_written":
                doc["counts"]["log_half_written"] = int(doc["counts"].get("log_half_written", 0)) + 1
            elif log_outcome == "lost":
                doc["counts"]["log_lost"] = int(doc["counts"].get("log_lost", 0)) + 1

            last_fault = pending.get("last_fault") or {}
            finding = {
                "event_seq": seq,
                "seq": seq,
                "object_key": event["key"],
                "op": event["op"],
                "logical_clock": event["logical_clock"],
                "absolute_clock_ms": self._clock_ms(event["logical_clock"]),
                "content_version": event["content_version"],
                "expected_signature": event["expected_signature"],
                "policy": self.policy,
                "order": self.order,
                "verdict": "double_success" if crash_verdict is None else "recovered_consistent",
                "crash_verdict": crash_verdict,
                "log_outcome": log_outcome,
                "recovery_action": recovery_action,
                "injection_id": pending.get("injection_id"),
                "fault": last_fault or None,
                "stage_history": pending.get("stage_history", []),
                "replay_summary": replay_summary,
                "worker_id": self.worker_id,
            }
            doc.setdefault("findings", []).append(finding)
            doc["completed_seqs"] = sorted(set(doc.get("completed_seqs", [])) | {seq})
            doc["seq_watermark"] = max(int(doc.get("seq_watermark", 0)), seq)
            doc["clock_watermark"] = max(int(doc.get("clock_watermark", 0)), int(event["logical_clock"]))
            doc["counts"]["events"] = len(doc["completed_seqs"])
            if crash_verdict is not None or recovery_action:
                doc["counts"]["recovered"] = int(doc["counts"].get("recovered", 0)) + 1
            else:
                doc["counts"]["double_success"] = int(doc["counts"].get("double_success", 0)) + 1
            if last_fault.get("code") == ErrorCode.WRITE_INTERRUPT:
                doc["counts"]["interrupted"] = int(doc["counts"].get("interrupted", 0)) + 1
            doc["pending_operations"].pop(str(seq), None)
            if not doc["pending_operations"]:
                doc["status"] = "consistent"
            result.update({"status": "completed", "finding": finding})

        self.ledger.update(mutate, logical_clock=event["logical_clock"])
        return result

    def _process_direct(self, event: dict[str, Any]) -> dict[str, Any]:
        self._start_event(event)
        if self.order == "metadata_then_data":
            self._commit_early_metadata(event)
            if event["op"] == "DELETE":
                self._perform_delete_data(event)
            else:
                self._atomic_write_data(event, "data_staging")
            return self._finalize_event(event)

        if event["op"] == "DELETE":
            self._perform_delete_data(event)
        else:
            self._atomic_write_data(event, "data_staging")
        self._update_pending(event["seq"], stage="metadata_commit", _clock=event["logical_clock"])
        fault = self._fault_matches("metadata_commit", event)
        self._raise_fault(
            "metadata_commit", event,
            mark_orphan=event["op"] != "DELETE",
            mark_dirty=event["op"] == "DELETE",
            dirty_reason="data_deleted_before_metadata_tombstone",
            extra_details={"current_generation": 1, "attempted_generation": 0}
            if fault and fault.get("fault_type") == "generation_rollback" else None,
        )
        return self._finalize_event(event)

    def process_event(self, event: dict[str, Any]) -> dict[str, Any]:
        self.clock.advance_to(int(event["logical_clock"]))
        existing = self.ledger.read()
        if int(event["seq"]) in existing.get("completed_seqs", []):
            return {"status": "already_completed", "event_seq": event["seq"]}
        if self.policy == "write_log":
            return self._process_write_log(event)
        return self._process_direct(event)

    @contextmanager
    def _log_file_lock(self) -> Iterator[None]:
        with self.ledger._file_lock(self.ledger.log_lock_path):
            yield

    def _append_log_entry(self, event: dict[str, Any]) -> dict[str, Any]:
        seq = event["seq"]
        fault = self._fault_matches("log_append", event)
        if fault and fault.get("fault_type") in {"generation_rollback", "log_lost"}:
            self._update_pending(seq, stage="log_append", _clock=event["logical_clock"])
            self._raise_fault("log_append", event)

        entry_body = {
            "entry_id": f"{self.run_id}:seq{seq}:v{event['content_version']}",
            "run_id": self.run_id,
            "event_seq": seq,
            "seq": seq,
            "op": event["op"],
            "object_key": event["key"],
            "content_version": event["content_version"],
            "expected_signature": event["expected_signature"],
            "logical_clock": event["logical_clock"],
            "absolute_clock_ms": self._clock_ms(event["logical_clock"]),
            "payload_size": event["payload_size"],
            "cache_generation": 1,
        }
        entry = {**entry_body, "checksum": sha256_hex(canonical_dumps(entry_body).encode("utf-8"))}
        line = (canonical_dumps(entry) + "\n").encode("utf-8")
        self._update_pending(
            seq, stage="log_append", log_path=self._relative(self.ledger.log_path),
            _clock=event["logical_clock"],
        )

        if fault and fault.get("fault_type") == "write_interrupt":
            partial = line[: max(1, len(line) // 2)]
            with self._log_file_lock(), self.ledger.log_path.open("ab") as handle:
                handle.write(partial)
                handle.flush()
                os.fsync(handle.fileno())
            self._update_pending(seq, log_half_written=True, log_bytes=len(partial),
                                _clock=event["logical_clock"])
            self._raise_fault("log_append", event, log_outcome="half_written")

        with self._log_file_lock(), self.ledger.log_path.open("ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        log_directory_fd = os.open(self.ledger.logs_dir, os.O_RDONLY)
        try:
            os.fsync(log_directory_fd)
        finally:
            os.close(log_directory_fd)
        self._update_pending(
            seq, log_entry_complete=True, log_entry_id=entry["entry_id"],
            log_bytes=len(line), _clock=event["logical_clock"],
        )
        if fault and fault.get("fault_type") == "force_kill":
            self._raise_fault("log_append", event)
        return entry

    def _append_log_entry_without_fault(self, event: dict[str, Any]) -> None:
        old_fault = self.fault_case
        self.fault_case = None
        try:
            self._append_log_entry(event)
        finally:
            self.fault_case = old_fault

    def read_log_entries(self) -> dict[str, Any]:
        if not self.ledger.log_path.exists():
            return {"outcome": "lost", "entries": [], "valid_offset": 0, "half_written": None}
        with self._log_file_lock():
            data = self.ledger.log_path.read_bytes()
        entries: list[dict[str, Any]] = []
        offset = 0
        valid_offset = 0
        half_written: dict[str, Any] | None = None
        for raw_line in data.splitlines(keepends=True):
            start = offset
            end = start + len(raw_line)
            offset = end
            has_newline = raw_line.endswith(b"\n")
            try:
                parsed = json.loads(raw_line.decode("utf-8"))
                checksum = parsed.pop("checksum")
                valid_checksum = sha256_hex(canonical_dumps(parsed).encode("utf-8")) == checksum
                parsed["checksum"] = checksum
            except Exception:
                parsed = None
                valid_checksum = False
            if not has_newline or parsed is None or not valid_checksum:
                half_written = {
                    "start_offset": start,
                    "end_offset": end,
                    "byte_length": end - start,
                    "reason": "truncated_or_checksum_mismatch",
                }
                break
            entries.append(parsed)
            valid_offset = end
        return {
            "outcome": "complete" if half_written is None else "half_written",
            "entries": entries,
            "valid_offset": valid_offset,
            "half_written": half_written,
            "file_size": len(data),
        }

    def _quarantine_half_log(self, log_read: dict[str, Any]) -> dict[str, Any]:
        half = log_read.get("half_written")
        valid_offset = int(log_read.get("valid_offset", 0))
        quarantine_path = self.ledger.logs_dir / f"quarantine-offset-{valid_offset}.part"
        with self._log_file_lock():
            if self.ledger.log_path.exists() and half:
                data = self.ledger.log_path.read_bytes()
                quarantine_path.write_bytes(data[valid_offset:])
                with self.ledger.log_path.open("r+b") as handle:
                    handle.truncate(valid_offset)
                    handle.flush()
                    os.fsync(handle.fileno())
        return {"quarantined_bytes_path": self._relative(quarantine_path),
                "truncated_to_offset": valid_offset}

    def replay_logs(self, event: dict[str, Any] | None = None, *,
                    trigger_injection: bool = True) -> dict[str, Any]:
        if trigger_injection and event is not None:
            fault = self._fault_matches("log_replay", event)
            if fault and fault.get("fault_type") == "log_lost":
                with self._log_file_lock():
                    if self.ledger.log_path.exists():
                        lost_path = self.ledger.logs_dir / f"lost-{fault.get('injection_id')}.jsonl"
                        shutil.move(str(self.ledger.log_path), str(lost_path))
                self._raise_fault("log_replay", event, log_outcome="lost")
            if fault:
                self._update_pending(event["seq"], stage="log_replay", _clock=event["logical_clock"])
                generation_fault = fault.get("fault_type") == "generation_rollback"
                self._raise_fault(
                    "log_replay", event,
                    extra_details={"current_generation": 1, "attempted_generation": 0}
                    if generation_fault else None,
                )

        log_read = self.read_log_entries()
        if log_read["outcome"] == "lost":
            return {"outcome": "lost", "applied_entry_ids": [], "watermark": None}
        if log_read["outcome"] == "half_written":
            quarantine = self._quarantine_half_log(log_read)
            return {
                "outcome": "half_written",
                "applied_entry_ids": [],
                "watermark": {"byte_offset": log_read["valid_offset"],
                             "entry_count": len(log_read["entries"])},
                "half_written": log_read["half_written"],
                "quarantine": quarantine,
            }

        entries = log_read["entries"]
        applied: list[str] = []

        def mutate(doc: dict[str, Any], connection: Any) -> None:
            clock = event["logical_clock"] if event else int(doc.get("clock_watermark", 0))
            self.ledger.acquire_replay_lease(
                connection, log_name=self.ledger.log_path.name,
                byte_offset=int(log_read["valid_offset"]), logical_clock=clock,
            )
            applied_set = set(doc.get("applied_log_entries", []))
            for entry in entries:
                entry_id = entry["entry_id"]
                if entry_id in applied_set:
                    continue
                key = entry["object_key"]
                version = int(entry["content_version"])
                final_path = self.ledger.objects_dir / key / f"v{version}.bin"
                is_delete = entry["op"] == "DELETE"
                data_present = final_path.exists()
                doc.setdefault("cache", {})[key] = {
                    "object_key": key,
                    "content_version": version,
                    "expected_signature": entry["expected_signature"],
                    "logical_clock": entry["logical_clock"],
                    "absolute_clock_ms": entry["absolute_clock_ms"],
                    "cache_generation": entry.get("cache_generation", 1),
                    "policy": self.policy,
                    "last_event_seq": entry["seq"],
                    "state": "tombstone" if is_delete else ("clean" if data_present else "dirty"),
                    "data_present": False if is_delete else data_present,
                    "tombstone": is_delete,
                    "replayed_from_log": entry_id,
                }
                applied_set.add(entry_id)
                applied.append(entry_id)
            doc["applied_log_entries"] = sorted(applied_set)
            doc["replay_watermark"] = {
                "log_name": self.ledger.log_path.name,
                "byte_offset": int(log_read["valid_offset"]),
                "entry_count": len(entries),
                "last_entry_id": entries[-1]["entry_id"] if entries else None,
                "last_committed_seq": max([int(item["seq"]) for item in entries], default=0),
            }

        clock = event["logical_clock"] if event else 0
        self.ledger.update(mutate, logical_clock=clock)
        return {
            "outcome": "complete",
            "applied_entry_ids": applied,
            "watermark": {
                "byte_offset": int(log_read["valid_offset"]),
                "entry_count": len(entries),
                "last_entry_id": entries[-1]["entry_id"] if entries else None,
            },
        }

    def _process_write_log(self, event: dict[str, Any]) -> dict[str, Any]:
        self._start_event(event)
        self._append_log_entry(event)
        if event["op"] == "DELETE":
            self._perform_delete_data(event)
        else:
            self._atomic_write_data(event, "data_write")
        replay_summary = self.replay_logs(event)
        return self._finalize_event(event, log_outcome="complete", replay_summary=replay_summary)

    def _ensure_event_data(self, event: dict[str, Any]) -> str:
        if event["op"] == "DELETE":
            return self._perform_delete_data(event)
        return self._ensure_write_data(event)

    def run_events(self, *, shard_index: int | None = None, shard_count: int | None = None,
                   stop_before_seq: int | None = None) -> dict[str, Any]:
        events = list(self.bundle.events)
        if stop_before_seq is not None:
            events = [event for event in events if int(event["seq"]) < stop_before_seq]
        if shard_count is not None:
            if shard_count <= 0 or shard_index is None or not (0 <= int(shard_index) < shard_count):
                raise DiagnosticError(ErrorCode.CLI_INVALID_ARGUMENT, "invalid shard index/count")
            selected: list[dict[str, Any]] = []
            for event in events:
                bucket = int(sha256_hex(event["key"].encode("utf-8"))[:16], 16) % shard_count
                if bucket == shard_index:
                    selected.append(event)
            events = selected
        results = [self.process_event(event) for event in events]
        return {"processed": len(results), "results": results, "ledger": self.ledger.read()}

    def resume(self, seq: int | None = None) -> dict[str, Any]:
        document = self.ledger.read()
        pending = document.get("pending_operations", {})
        if seq is not None:
            if str(seq) not in pending:
                raise DiagnosticError(
                    ErrorCode.STATE_NOT_FOUND,
                    "no uncommitted pending operation found for event seq",
                    details={"seq": seq, "pending_seqs": sorted(pending, key=int)},
                )
            sequences = [seq]
        else:
            sequences = sorted((int(value) for value in pending.keys()), key=int)
        results = []
        for event_seq in sequences:
            event = self.bundle.event_by_seq[event_seq]
            self.clock.advance_to(int(event["logical_clock"]))
            if self.policy == "write_log":
                results.append(self._recover_log_event(event))
            else:
                results.append(self._recover_direct_event(event))
        return {"resumed": len(results), "results": results, "ledger": self.ledger.read()}

    def _recover_direct_event(self, event: dict[str, Any]) -> dict[str, Any]:
        seq = int(event["seq"])
        document = self.ledger.read()
        pending = document["pending_operations"].get(str(seq), {})
        metadata_committed = bool(pending.get("metadata_committed"))
        data_finalized = bool(pending.get("data_finalized"))
        had_orphan = any(item.get("event_seq") == seq for item in document.get("orphans", []))
        had_dirty = any(item.get("event_seq") == seq for item in document.get("dirty", []))

        if event["op"] == "DELETE":
            if metadata_committed and self._final_path(event).exists():
                crash_verdict = "orphan_object"
                self._perform_delete_data(event)
                action = "removed_unreachable_data_after_tombstone_replay"
            else:
                self._perform_delete_data(event)
                crash_verdict = "dirty_cache" if (had_dirty or pending.get("data_removed") or pending.get("last_fault")) else None
                action = "committed_tombstone_to_clear_stale_pointer"
        else:
            self._ensure_write_data(event)
            if not metadata_committed and (data_finalized or had_orphan):
                crash_verdict = "orphan_object"
                action = "committed_metadata_rollforward_for_complete_data"
            elif metadata_committed and (had_dirty or not data_finalized):
                crash_verdict = "dirty_cache"
                action = "wrote_missing_data_to_match_metadata_pointer"
            elif had_orphan:
                crash_verdict = "orphan_object"
                action = "reconciled_orphan_record"
            elif had_dirty:
                crash_verdict = "dirty_cache"
                action = "reconciled_dirty_cache_record"
            else:
                crash_verdict = None
                action = "verified_and_completed_pending_write"
        result = self._finalize_event(event, crash_verdict=crash_verdict, recovery_action=action)
        return {"event_seq": seq, "crash_verdict": crash_verdict, "recovery_action": action, "result": result}

    def _recover_log_event(self, event: dict[str, Any]) -> dict[str, Any]:
        seq = int(event["seq"])
        first_replay = self.replay_logs(trigger_injection=False)
        first_outcome = first_replay.get("outcome")
        document = self.ledger.read()
        pending = document["pending_operations"].get(str(seq), {})
        last_fault = pending.get("last_fault") or {}

        if first_outcome == "half_written":
            self._append_log_entry_without_fault(event)
            final_replay = self.replay_logs(trigger_injection=False)
            self._ensure_event_data(event)
            final_replay = self.replay_logs(trigger_injection=False)
            result = self._finalize_event(
                event,
                crash_verdict=None,
                log_outcome="half_written",
                recovery_action="quarantined_partial_log_and_reappended_complete_entry",
                replay_summary={"first_replay": first_replay, "final_replay": final_replay},
            )
            return {"event_seq": seq, "log_outcome": "half_written", "result": result}

        if first_outcome == "lost":
            if event["op"] == "DELETE":
                crash_verdict = "orphan_object" if self._final_path(event).exists() else None
            else:
                crash_verdict = "dirty_cache" if not pending.get("data_finalized") else None
            self._ensure_event_data(event)
            result = self._finalize_event(
                event,
                crash_verdict=crash_verdict,
                log_outcome="lost",
                recovery_action="verified_data_and_rebuilt_metadata_without_log",
                replay_summary=first_replay,
            )
            return {"event_seq": seq, "log_outcome": "lost", "result": result}

        if event["op"] == "DELETE":
            delete_pending = not bool(pending.get("data_removed"))
            data_present_before_recovery = self._final_path(event).exists()
            self._ensure_event_data(event)
            if delete_pending and data_present_before_recovery:
                crash_verdict = "orphan_object"
            else:
                crash_verdict = None
        else:
            self._ensure_event_data(event)
            data_missing_at_crash = not bool(pending.get("data_finalized"))
            kill_at_replay = last_fault.get("stage") == "log_replay"
            crash_verdict = "dirty_cache" if data_missing_at_crash and not kill_at_replay else None
        final_replay = self.replay_logs(trigger_injection=False)
        result = self._finalize_event(
            event, crash_verdict=crash_verdict, log_outcome="complete",
            recovery_action="replayed_complete_log_and_verified_data",
            replay_summary={"first_replay": first_replay, "final_replay": final_replay},
        )
        return {"event_seq": seq, "log_outcome": "complete", "crash_verdict": crash_verdict, "result": result}
