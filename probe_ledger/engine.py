from __future__ import annotations

import os
import sys
from collections import defaultdict
from typing import Any

from .canonical import missing_digest
from .errors import EXIT_INTERRUPTED, diagnostic
from .merge import effective_event, same_version_conflict, version_conflict
from .store import LedgerStore


class Reconciler:
    def __init__(
        self,
        store: LedgerStore,
        *,
        fault_after_transitions: int | None = None,
        fault_before_commit: bool = False,
    ) -> None:
        self.store = store
        self.fault_after_transitions = fault_after_transitions
        self.fault_before_commit = fault_before_commit
        self.transitions_committed = 0

    def _fault(self, *, before: bool = False) -> None:
        if self.fault_after_transitions is None:
            return
        if before and self.fault_before_commit:
            if self.transitions_committed + 1 == self.fault_after_transitions:
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(EXIT_INTERRUPTED)
        if (
            not before
            and not self.fault_before_commit
            and self.transitions_committed >= self.fault_after_transitions
        ):
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(EXIT_INTERRUPTED)

    def _apply(self, instance_id: str, event: dict[str, Any]) -> dict[str, Any]:
        self._fault(before=True)
        result = self.store.apply_event(instance_id, event)
        self.transitions_committed += 1
        self._fault(before=False)
        return result

    @staticmethod
    def _groups(rows: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[int(row["obs_seq"])].append(row)
        for group in grouped.values():
            group.sort(
                key=lambda row: (
                    int(row["logical_clock_ms"]),
                    str(row["digest"]),
                    str(row["shard_id"]),
                )
            )
        return dict(sorted(grouped.items()))

    def _conflict_payload(
        self,
        instance_id: str,
        obs_seq: int,
        left: dict[str, Any],
        right: dict[str, Any],
        *,
        code: str,
        message: str,
        **extra: Any,
    ) -> dict[str, Any]:
        return diagnostic(
            code,
            message,
            instance_id=instance_id,
            obs_seq=obs_seq,
            shard_id=str(right["shard_id"]),
            digest_a=str(left["digest"]),
            digest_b=str(right["digest"]),
            arrival_a=int(left["arrival_order"]),
            arrival_b=int(right["arrival_order"]),
            **extra,
        )

    def _check_group(
        self,
        instance_id: str,
        obs_seq: int,
        group: list[dict[str, Any]],
        config: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        same_version = same_version_conflict(group)
        if same_version is not None:
            left, right = same_version
            payload = self._conflict_payload(
                instance_id,
                obs_seq,
                left,
                right,
                code="E_CONFLICT_OBS",
                message="同一观测序号在待裁决缓冲区中出现互斥内容",
                schema_versions=[
                    int(left["schema_version"]),
                    int(right["schema_version"]),
                ],
            )
            return None, payload
        cross_version = version_conflict(group, config)
        if cross_version is not None:
            left, right, fields = cross_version
            payload = self._conflict_payload(
                instance_id,
                obs_seq,
                left,
                right,
                code="E_VERSION_CONFLICT",
                message="同一观测的 v1/v2 表示语义不等价，实例待复核",
                schema_versions=[
                    int(left["schema_version"]),
                    int(right["schema_version"]),
                ],
                difference_fields=sorted(set(fields)),
            )
            return None, payload
        return effective_event(obs_seq, group), None

    def _missing_event(
        self,
        instance_id: str,
        obs_seq: int,
        logical_clock_ms: int,
    ) -> dict[str, Any]:
        return {
            "kind": "missing",
            "instance_id": instance_id,
            "obs_seq": obs_seq,
            "logical_clock_ms": logical_clock_ms,
            "outcome": "missing",
            "basis_digest": missing_digest(instance_id, obs_seq),
        }

    def _rollback_check(
        self,
        instance_id: str,
        obs_seq: int,
        group: list[dict[str, Any]],
        history_at_seq: dict[int, dict[str, Any]],
    ) -> dict[str, Any] | None:
        history = history_at_seq.get(obs_seq)
        if history is None:
            return None
        expected_digest = str(history["basis_digest"])
        observed_digests = sorted({str(row["semantic_digest"]) for row in group})
        if observed_digests == [expected_digest]:
            return None
        ordered = sorted(
            group,
            key=lambda row: (
                str(row["digest"]),
                str(row["shard_id"]),
                int(row["arrival_order"]),
            ),
        )
        left = ordered[0]
        right = ordered[-1]
        return diagnostic(
            "E_OBS_SEQ_ROLLBACK",
            "迟到观测落到已提交水位之下且与已提交语义不一致",
            instance_id=instance_id,
            obs_seq=obs_seq,
            shard_id=str(right["shard_id"]),
            digest_a=expected_digest,
            digest_b=str(right["semantic_digest"]),
            arrival_a=int(left["arrival_order"]),
            arrival_b=int(right["arrival_order"]),
            committed_basis_kind=str(history["basis_kind"]),
            raw_digests=sorted({str(row["digest"]) for row in group}),
        )

    def reconcile(self, *, seal: bool = False) -> dict[str, Any]:
        seal_info = self.store.seal_status(force=seal)
        all_diagnostics: list[dict[str, Any]] = []
        per_instance: list[dict[str, Any]] = []

        for instance_id in self.store.instance_ids():
            state = self.store.get_instance_state(instance_id)
            config = state["config"]
            rows = self.store.observation_rows(instance_id)
            groups = self._groups(rows)
            history_rows = self.store.history_rows(instance_id)
            history_at_seq = {
                int(row["obs_seq"]): row for row in history_rows
            }
            watermark = int(state["watermark"])
            last_clock = (
                None
                if state["last_basis"] is None
                else int(state["last_basis"]["logical_clock_ms"])
            )
            processed = 0
            blocked = False
            pending_payloads: list[dict[str, Any]] = []
            version_conflict_flag = False
            instance_new: list[dict[str, Any]] = []

            for obs_seq, group in groups.items():
                if obs_seq <= watermark:
                    payload = self._rollback_check(
                        instance_id,
                        obs_seq,
                        group,
                        history_at_seq,
                    )
                    if payload is not None:
                        instance_new.append(payload)
                        pending_payloads.append(payload)
                    continue

                if obs_seq > watermark + 1 and not seal_info["sealed"]:
                    blocked = True
                    break

                event, conflict = self._check_group(
                    instance_id,
                    obs_seq,
                    group,
                    config,
                )
                if conflict is not None:
                    instance_new.append(conflict)
                    pending_payloads.append(conflict)
                    version_conflict_flag = version_conflict_flag or (
                        conflict["code"] == "E_VERSION_CONFLICT"
                    )
                    blocked = True
                    break
                assert event is not None
                event_clock = int(event["logical_clock_ms"])
                if last_clock is not None and event_clock < (
                    last_clock - int(config["clock_rollback_tolerance_ms"])
                ):
                    payload = diagnostic(
                        "E_CLOCK_ROLLBACK",
                        "逻辑时钟回拨超过容差",
                        instance_id=instance_id,
                        obs_seq=obs_seq,
                        shard_id=event["shard_ids"][0],
                        logical_clock_ms=event_clock,
                        previous_logical_clock_ms=last_clock,
                        tolerance_ms=int(config["clock_rollback_tolerance_ms"]),
                    )
                    instance_new.append(payload)
                    pending_payloads.append(payload)
                    blocked = True
                    break

                if obs_seq > watermark + 1:
                    gap = obs_seq - watermark - 1
                    base_clock = 0 if last_clock is None else last_clock
                    span = max(0, event_clock - base_clock)
                    for offset in range(1, gap + 1):
                        missing_seq = watermark + 1
                        missing_clock = base_clock + (span * offset) // (gap + 1)
                        result = self._apply(
                            instance_id,
                            self._missing_event(
                                instance_id,
                                missing_seq,
                                missing_clock,
                            ),
                        )
                        processed += 1
                        watermark = missing_seq
                        last_clock = missing_clock
                        history_at_seq[missing_seq] = {
                            **result["transition"],
                            "basis_digest": result["transition"]["digest"],
                            "basis_kind": result["transition"]["basis_kind"],
                        }

                result = self._apply(instance_id, event)
                processed += 1
                watermark = obs_seq
                last_clock = event_clock
                history_at_seq[obs_seq] = {
                    **result["transition"],
                    "basis_digest": result["transition"]["digest"],
                    "basis_kind": result["transition"]["basis_kind"],
                }

            if pending_payloads:
                committed = self.store.mark_review(
                    instance_id,
                    pending_payloads,
                    version_conflict=version_conflict_flag,
                )
                if committed is not None:
                    all_diagnostics.extend(pending_payloads)
            all_diagnostics.extend(instance_new)
            per_instance.append(
                {
                    "instance_id": instance_id,
                    "transitions": processed,
                    "blocked": blocked,
                    "diagnostics": instance_new,
                }
            )

        snapshot_result = self.store.materialize()
        all_stored_diagnostics = self.store.diagnostics_rows()
        errors = [
            row
            for row in all_stored_diagnostics
            if str(row["code"]).startswith("E_")
        ]
        return {
            "ok": not errors,
            "sealed": bool(seal_info["sealed"]),
            "streams": seal_info["streams"],
            "transitions_committed": self.transitions_committed,
            "instances": per_instance,
            "diagnostics": errors,
            "pointer_version": snapshot_result["pointer_version"],
            "state_digest": snapshot_result["state_digest"],
        }
