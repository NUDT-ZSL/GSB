from __future__ import annotations

import os
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Sequence

from .diagnosis import Diagnosis, diagnose_target
from .errors import BusinessError
from .locking import TargetFileLock
from .models import RoundSpec, Scenario
from .prober import HttpProber
from .scheduler import ProbeCallable, execute_round
from .scenario import canonical_json, digest_json, load_scenario, scenario_digests
from .stats import compute_windows
from .store import Store


class DiagnosticEngine:
    def __init__(self, store: Store, probe_fn: ProbeCallable | None = None) -> None:
        self.store = store
        self.probe_fn = probe_fn or HttpProber().probe

    def load(self, scenario_path: str | Path) -> dict[str, Any]:
        scenario = load_scenario(scenario_path)
        return self.load_scenario(scenario)

    def load_scenario(self, scenario: Scenario) -> dict[str, Any]:
        result = self.store.load_scenario(scenario)
        return {
            "ok": True,
            "scenario_id": result.scenario_id,
            "config_version": result.config_version,
            "changed": result.changed,
            "content_digest": result.content_digest,
            "config_digest": result.config_digest,
            "scenario_digest": result.scenario_digest,
            "targets": sorted(target.name for target in scenario.targets),
            "rounds": len(scenario.rounds),
            "probes": sum(len(round_spec.probes) for round_spec in scenario.rounds),
        }

    def run(
        self,
        *,
        round_no: int | None = None,
        target_names: Sequence[str] | None = None,
        all_rounds: bool = False,
        recover_first: bool = False,
    ) -> dict[str, Any]:
        config_version, scenario = self.store.current_scenario()
        selected = _select_targets(scenario, target_names)
        recovered = self.recover_pending() if recover_first else []
        processed: list[dict[str, Any]] = []
        if round_no is not None:
            processed.append(
                self._process_round(
                    config_version=config_version,
                    scenario=scenario,
                    round_no=round_no,
                    selected_targets=selected,
                    require_contiguous=True,
                )
            )
        else:
            limit = 10000
            for _ in range(limit):
                pending_round = self._next_pending_round(
                    config_version, scenario, selected
                )
                if pending_round is None:
                    break
                processed.append(
                    self._process_round(
                        config_version=config_version,
                        scenario=scenario,
                        round_no=pending_round,
                        selected_targets=selected,
                        require_contiguous=True,
                    )
                )
                if not all_rounds:
                    break
            else:
                raise BusinessError(
                    "ROUND_REPLAY_LIMIT",
                    "round replay limit exceeded",
                    {"limit": limit},
                )
        return {
            "ok": True,
            "config_version": config_version,
            "processed_rounds": processed,
            "recovered_attempts": recovered,
            "status": self.store.status(),
        }

    def recover_pending(self) -> list[dict[str, Any]]:
        recovered: list[dict[str, Any]] = []
        pending_groups: dict[tuple[int, int], set[str]] = {}
        for attempt in self.store.pending_attempts():
            key = (int(attempt["config_version"]), int(attempt["round_no"]))
            pending_groups.setdefault(key, set()).add(str(attempt["target_name"]))
        for (version, round_no), target_names in sorted(pending_groups.items()):
            scenario = self.store.scenario_for_version(version)
            result = self._process_round(
                config_version=version,
                scenario=scenario,
                round_no=round_no,
                selected_targets=set(target_names),
                require_contiguous=False,
                replay_existing=True,
            )
            if result["commits"] or result["recovered_attempts"]:
                recovered.append(result)
        return recovered

    def _next_pending_round(
        self,
        config_version: int,
        scenario: Scenario,
        selected_targets: set[str],
    ) -> int | None:
        for round_spec in scenario.rounds:
            for probe in round_spec.probes:
                if probe.target not in selected_targets:
                    continue
                if self.store.round_commit(
                    config_version, round_spec.round_no, probe.target
                ) is None:
                    return round_spec.round_no
        return None

    def _process_round(
        self,
        *,
        config_version: int,
        scenario: Scenario,
        round_no: int,
        selected_targets: set[str],
        require_contiguous: bool,
        replay_existing: bool = False,
    ) -> dict[str, Any]:
        round_spec = _find_round(scenario, round_no)
        target_map = scenario.target_map()
        active_targets = sorted(
            {
                probe.target
                for probe in round_spec.probes
                if probe.target in selected_targets
                and self.store.round_commit(config_version, round_no, probe.target)
                is None
            }
        )
        if not active_targets:
            return {
                "round_no": round_no,
                "config_version": config_version,
                "targets": [],
                "commits": [],
                "recovered_attempts": [],
                "abandoned": [],
                "clock_before_ms": None,
                "clock_after_ms": None,
                "deadline_ms": None,
                "skipped": "already_committed",
            }
        if require_contiguous:
            for target_name in active_targets:
                _assert_contiguous(
                    self.store, config_version, scenario, target_name, round_no
                )

        locks = ExitStack()
        try:
            for target_name in active_targets:
                locks.enter_context(
                    TargetFileLock(
                        self.store.lock_dir,
                        target_name,
                        timeout_s=self.store.lock_timeout_s,
                    )
                )
            still_active = [
                target_name
                for target_name in active_targets
                if self.store.round_commit(config_version, round_no, target_name)
                is None
            ]
            if not still_active:
                return {
                    "round_no": round_no,
                    "config_version": config_version,
                    "targets": [],
                    "commits": [],
                    "recovered_attempts": [],
                    "abandoned": [],
                    "clock_before_ms": None,
                    "clock_after_ms": None,
                    "deadline_ms": None,
                    "skipped": "committed_under_lock",
                }
            attempts: dict[str, str] = {}
            recovered_attempts: list[dict[str, Any]] = []
            start_clock = self.store.logical_clock_watermark()
            committed_targets = sorted(
                {
                    probe.target
                    for probe in round_spec.probes
                    if self.store.round_commit(
                        config_version, round_no, probe.target
                    )
                    is not None
                }
            )
            if replay_existing:
                committed_clock = self.store.round_clock_before(
                    config_version, round_no
                )
                if committed_clock is not None:
                    start_clock = committed_clock
            for target_name in still_active:
                claim = self.store.prepare_attempt(
                    config_version=config_version,
                    round_no=round_no,
                    target_name=target_name,
                    start_clock_ms=start_clock,
                )
                if claim is None:
                    continue
                attempts[target_name] = claim.attempt_id
                if claim.recovered:
                    recovered_attempts.append(
                        {
                            "target": target_name,
                            "round_no": round_no,
                            "config_version": config_version,
                            "attempt_id": claim.attempt_id,
                        }
                    )
            if not attempts:
                return {
                    "round_no": round_no,
                    "config_version": config_version,
                    "targets": [],
                    "commits": [],
                    "recovered_attempts": recovered_attempts,
                    "abandoned": [],
                    "clock_before_ms": None,
                    "clock_after_ms": None,
                    "deadline_ms": None,
                    "skipped": "no_attempt_claimed",
                }
            replay_existing = replay_existing or bool(recovered_attempts)
            _test_crash("prepared")
            scoped_targets = set(attempts)
            if replay_existing:
                scoped_targets |= set(committed_targets)
                committed_clock = self.store.round_clock_before(
                    config_version, round_no
                )
                if committed_clock is not None:
                    start_clock = committed_clock
            scoped_probes = tuple(
                probe
                for probe in round_spec.probes
                if probe.target in scoped_targets
            )
            precompleted = {}
            if replay_existing:
                for target_name in committed_targets:
                    for record in self.store.fetch_round_records(
                        config_version, round_no, target_name
                    ):
                        precompleted[record.seq] = record
            scoped_round = RoundSpec(
                round_no=round_spec.round_no,
                logical_tick=round_spec.logical_tick,
                probes=scoped_probes,
            )
            execution = execute_round(
                round_spec=scoped_round,
                targets=target_map,
                config=scenario.config,
                config_version=config_version,
                clock_watermark_ms=start_clock,
                probe_fn=self.probe_fn,
                precompleted=precompleted,
            )
            target_decisions: list[Diagnosis] = []
            commits: list[dict[str, Any]] = []
            for index, target_name in enumerate(sorted(attempts)):
                new_records = [
                    record
                    for record in execution.records
                    if record.target == target_name
                ]
                previous_records = self.store.fetch_records(
                    config_version,
                    target_name,
                    up_to_round=round_no,
                )
                all_records = previous_records + new_records
                windows = compute_windows(
                    all_records,
                    scenario.config,
                    config_version,
                    target_name,
                )
                diagnosis = diagnose_target(
                    target=target_map[target_name],
                    records=all_records,
                    config=scenario.config,
                    config_version=config_version,
                )
                target_decisions.append(diagnosis)
                commit = self.store.commit_round(
                    attempt_id=attempts[target_name],
                    config_version=config_version,
                    round_no=round_no,
                    target=target_map[target_name],
                    config=scenario.config,
                    records=new_records,
                    windows=windows,
                    diagnosis=diagnosis,
                    clock_before_ms=execution.clock_before_ms,
                    clock_after_ms=execution.clock_after_ms,
                )
                commits.append(
                    {
                        "target": target_name,
                        "commit_id": commit["commit_id"],
                        "payload_digest": commit["payload_digest"],
                        "state_after": commit["state_after"],
                        "loss_rate": commit["loss_rate"],
                        "abandoned_count": commit["abandoned_count"],
                    }
                )
                _test_crash("committed", index=index)
            return {
                "round_no": round_no,
                "config_version": config_version,
                "targets": active_targets,
                "commits": commits,
                "recovered_attempts": recovered_attempts,
                "abandoned": [
                    record.as_dict()
                    for record in execution.abandoned
                    if record.target in attempts
                ],
                "records": [
                    record.as_dict()
                    for record in execution.records
                    if record.target in attempts
                ],
                "window_summaries": [
                    window.as_dict()
                    for decision in target_decisions
                    for window in (
                        compute_windows(
                            self.store.fetch_records(
                                config_version,
                                decision.target,
                                up_to_round=round_no,
                            ),
                            scenario.config,
                            config_version,
                            decision.target,
                        )
                    )
                ],
                "diagnoses": [
                    decision.as_dict() for decision in target_decisions
                ],
                "clock_before_ms": execution.clock_before_ms,
                "clock_after_ms": execution.clock_after_ms,
                "deadline_ms": execution.deadline_ms,
            }
        finally:
            locks.close()

    def diagnose(self, target_names: Sequence[str] | None = None) -> dict[str, Any]:
        config_version, scenario = self.store.current_scenario()
        selected = _select_targets(scenario, target_names)
        diagnoses: list[dict[str, Any]] = []
        for target in sorted(scenario.targets, key=lambda item: item.name):
            if target.name not in selected:
                continue
            records = self.store.fetch_records(config_version, target.name)
            diagnosis = diagnose_target(
                target=target,
                records=records,
                config=scenario.config,
                config_version=config_version,
            )
            persisted = self.store.target_state(target.name)
            matches_persisted = (
                persisted is not None
                and int(persisted["evaluated_config_version"]) == config_version
                and canonical_json(json_loads(persisted["decision_chain_json"]))
                == canonical_json(diagnosis.decision_chain)
            )
            diagnoses.append(
                {
                    **diagnosis.as_dict(),
                    "records_count": len(records),
                    "matches_persisted": matches_persisted,
                }
            )
        return {
            "ok": True,
            "config_version": config_version,
            "diagnoses": diagnoses,
        }

    def verify(self) -> dict[str, Any]:
        mismatches: list[dict[str, Any]] = []
        checks = 0
        try:
            config_version, scenario = self.store.current_scenario()
            pointer_version, content_digest, config_digest, scenario_digest = (
                self.store.current_digests()
            )
            expected_digests = scenario_digests(scenario)
            checks += 1
            if pointer_version != config_version:
                mismatches.append(
                    {
                        "check": "config_pointer",
                        "detail": "pointer version differs from loaded revision",
                    }
                )
            checks += 1
            if expected_digests != (
                content_digest,
                config_digest,
                scenario_digest,
            ):
                mismatches.append(
                    {
                        "check": "config_digest",
                        "detail": "stored config digests differ from the loaded scenario",
                    }
                )

            pending = self.store.pending_attempts()
            checks += 1
            if pending:
                mismatches.append(
                    {
                        "check": "pending_attempts",
                        "detail": "uncommitted attempts remain; run resume",
                        "attempts": pending,
                    }
                )

            max_commit_clock = 0
            for target in sorted(scenario.targets, key=lambda item: item.name):
                records = self.store.fetch_records(config_version, target.name)
                if not records:
                    checks += 1
                    continue
                windows = compute_windows(
                    records,
                    scenario.config,
                    config_version,
                    target.name,
                )
                diagnosis = diagnose_target(
                    target=target,
                    records=records,
                    config=scenario.config,
                    config_version=config_version,
                )
                stored_windows = self.store.window_summaries(
                    config_version, target.name
                )
                checks += 1
                if canonical_json(stored_windows) != canonical_json(
                    [window.as_dict() for window in windows]
                ):
                    mismatches.append(
                        {
                            "check": "window_summary",
                            "target": target.name,
                            "detail": "stored windows do not match a deterministic recomputation",
                        }
                    )
                persisted = self.store.target_state(target.name)
                checks += 1
                if persisted is None or int(
                    persisted["evaluated_config_version"]
                ) != config_version:
                    mismatches.append(
                        {
                            "check": "target_state_version",
                            "target": target.name,
                            "detail": "target state is missing or belongs to an older config",
                        }
                    )
                else:
                    checks += 1
                    if canonical_json(
                        json_loads(persisted["decision_chain_json"])
                    ) != canonical_json(diagnosis.decision_chain):
                        mismatches.append(
                            {
                                "check": "decision_chain",
                                "target": target.name,
                               "detail": "persisted decision chain differs from recomputation",
                            }
                        )
                    if (
                        persisted["state"] != diagnosis.state
                        or _float_or_none(persisted["loss_rate"])
                        != diagnosis.loss_rate
                        or int(persisted["abandoned_count"])
                        != diagnosis.abandoned_count
                    ):
                        mismatches.append(
                            {
                                "check": "target_state_fields",
                                "target": target.name,
                                "detail": "persisted state fields differ from recomputation",
                            }
                        )

            for commit in self.store.all_round_commits(config_version):
                checks += 3
                record_dicts = json_loads(commit["samples_json"])
                window_dicts = json_loads(commit["window_summary_json"])
                decision_chain = json_loads(commit["decision_chain_json"])
                payload = {
                    "config_version": int(commit["config_version"]),
                    "round_no": int(commit["round_no"]),
                    "target": commit["target_name"],
                    "records": record_dicts,
                    "windows": window_dicts,
                    "decision_chain": decision_chain,
                }
                if digest_json(payload) != commit["payload_digest"]:
                    mismatches.append(
                        {
                            "check": "commit_payload_digest",
                            "target": commit["target_name"],
                            "round_no": commit["round_no"],
                        }
                    )
                expected_commit_id = digest_json(["commit", payload])
                if expected_commit_id != commit["commit_id"]:
                    mismatches.append(
                        {
                            "check": "commit_id",
                            "target": commit["target_name"],
                            "round_no": commit["round_no"],
                        }
                    )
                db_records = [
                    record.as_dict()
                    for record in self.store.fetch_records(
                        config_version, commit["target_name"]
                    )
                    if record.round_no == int(commit["round_no"])
                ]
                if canonical_json(record_dicts) != canonical_json(db_records):
                    mismatches.append(
                        {
                            "check": "sample_rows",
                            "target": commit["target_name"],
                            "round_no": commit["round_no"],
                            "detail": "sample table differs from committed sample snapshot",
                        }
                    )
                max_commit_clock = max(
                    max_commit_clock, int(commit["clock_after_ms"])
                )
            checks += 1
            if max_commit_clock != self.store.logical_clock_watermark():
                mismatches.append(
                    {
                        "check": "logical_clock_watermark",
                        "detail": "stored clock watermark is not the maximum committed clock",
                        "stored": self.store.logical_clock_watermark(),
                        "expected": max_commit_clock,
                    }
                )
            status = self.store.status()
            checks += 1
            if canonical_json(status) != canonical_json(self.store.status()):
                mismatches.append(
                    {
                        "check": "status_stability",
                        "detail": "status output is not byte-stable across repeated reads",
                    }
                )
            return {
                "ok": not mismatches,
                "checks": checks,
                "mismatches": mismatches,
                "config_version": config_version,
                "content_digest": content_digest,
                "config_digest": config_digest,
                "scenario_digest": scenario_digest,
                "status": status,
            }
        except BusinessError as exc:
            return {
                "ok": False,
                "checks": checks,
                "mismatches": [
                    {
                        "check": "business_error",
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    }
                ],
            }


def _find_round(scenario: Scenario, round_no: int) -> RoundSpec:
    for round_spec in scenario.rounds:
        if round_spec.round_no == round_no:
            return round_spec
    raise BusinessError(
        "ROUND_NOT_FOUND",
        "requested round is not present in the loaded scenario",
        {"round_no": round_no},
    )


def _select_targets(
    scenario: Scenario, target_names: Sequence[str] | None
) -> set[str]:
    known = {target.name for target in scenario.targets}
    if target_names is None:
        return known
    requested = set(target_names)
    unknown = sorted(requested - known)
    if unknown:
        raise BusinessError(
            "TARGET_NOT_FOUND",
            "one or more requested targets are not in the current scenario",
            {"targets": unknown},
        )
    return requested


def _assert_contiguous(
    store: Store,
    config_version: int,
    scenario: Scenario,
    target_name: str,
    round_no: int,
) -> None:
    target_rounds = [
        round_spec.round_no
        for round_spec in scenario.rounds
        if any(probe.target == target_name for probe in round_spec.probes)
    ]
    position = target_rounds.index(round_no)
    committed = store.committed_rounds(config_version, target_name)
    expected_prior = set(target_rounds[:position])
    missing = sorted(expected_prior - committed)
    if missing:
        raise BusinessError(
            "ROUND_GAP",
            "prior rounds for this target must be committed first",
            {
                "target": target_name,
                "round_no": round_no,
                "missing_rounds": missing,
            },
        )


def _test_crash(point: str, *, index: int | None = None) -> None:
    requested = os.environ.get("NETDIAG_TEST_CRASH_AT")
    if not requested:
        return
    if requested == point:
        os._exit(75)
    if point == "committed" and requested == "after_commit_0" and index == 0:
        os._exit(75)


def _float_or_none(value: Any) -> float | None:
    return None if value is None else float(value)


def json_loads(text: str) -> Any:
    import json

    return json.loads(text)
