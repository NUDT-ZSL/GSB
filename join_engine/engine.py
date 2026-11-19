from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .canonical import LogicalClock, key_token, result_order_key, sha256_value
from .crash import CrashInjector
from .errors import EngineError, fail
from .files import count_jsonl_rows, digest_file, iter_jsonl
from .generator import build_table_stats, stats_summary
from .join_algorithms import (
    assemble_final_result,
    execute_join,
)
from .models import QuerySpec, SampleConfig, json_dumps_stable
from .partitioning import partition_for_key
from .result_sink import JoinMetrics, StoredResultSink
from .selection import decision_summary, select_algorithm
from .shuffle import (
    cleanup_dead_temps,
    committed_file_valid,
    shuffle_side,
    shuffle_side_local,
)
from .storage import StateStore
from .truth import compare_truth, compute_truth


def build_observations(
    config: SampleConfig,
    scale_name: str | None,
) -> dict[str, dict[str, Any]]:
    scale = config.scale(scale_name)
    left_table = config.table_map[config.query.left_table]
    right_table = config.table_map[config.query.right_table]
    left_stats = build_table_stats(
        left_table,
        config.seed,
        config.execution.partitions,
        rows_override=scale.left_rows,
        skew_threshold=config.execution.skew_threshold,
    )
    right_stats = build_table_stats(
        right_table,
        config.seed,
        config.execution.partitions,
        rows_override=scale.right_rows,
        skew_threshold=config.execution.skew_threshold,
    )
    return {"left": left_stats, "right": right_stats}


def plan_query(config: SampleConfig, scale_name: str | None = None) -> dict[str, Any]:
    scale = config.scale(scale_name)
    observations = build_observations(config, scale.name)
    decision = select_algorithm(
        observations["left"],
        observations["right"],
        config.execution,
        scale_name=scale.name,
    )
    return {
        "ok": True,
        "command": "plan",
        "query_id": config.query.query_id,
        "scale": scale.to_dict(),
        "fingerprint": config.fingerprint(scale.name),
        "observed_tables": {
            side: stats_summary(stats) for side, stats in observations.items()
        },
        "observation_source": {
            "kind": "deterministic_generator_observed",
            "note": "row counts, key counts, partition row counts and byte estimates come from a full deterministic generation pass",
        },
        "decision": decision,
        "persistent_state_written": False,
    }


def _completed_commits(
    commits: Iterable[dict[str, Any]],
    partitions: int,
) -> bool:
    identifiers = {int(commit["partition_id"]) for commit in commits}
    return identifiers == set(range(partitions))


def _actual_peak_partition_rows(commits: list[dict[str, Any]]) -> int:
    return max((int(commit["rows"]) for commit in commits), default=0)


def _result_is_complete(commits: list[dict[str, Any]], partitions: int) -> bool:
    return _completed_commits(commits, partitions)


def _partition_filter(
    text: str | None,
    partitions: int,
) -> set[int] | None:
    if text is None:
        return None
    selected: set[int] = set()
    for piece in text.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            value = int(piece)
        except ValueError:
            fail("E_BAD_CONFIG", f"invalid partition id: {piece!r}")
        if value < 0 or value >= partitions:
            fail(
                "E_BAD_CONFIG",
                "partition id is outside the configured partition range",
                details={"partition": value, "partitions": partitions},
            )
        selected.add(value)
    if not selected:
        fail("E_BAD_CONFIG", "partition filter cannot be empty")
    return selected


def _read_result_rows(path: Path) -> list[dict[str, Any]]:
    return list(iter_jsonl(path))


def _verification_summary(comparison: dict[str, Any]) -> dict[str, Any]:
    first = comparison.get("first_difference")
    if first is not None:
        first = {
            "row_offset": first["row_offset"],
            "result_source_partitions": first["result_source_partitions"],
            "truth_source_partitions": first["truth_source_partitions"],
            "result": first["result"],
            "truth": first["truth"],
        }
    return {
        "ok": comparison["ok"],
        "mismatch_count": comparison["mismatch_count"],
        "first_difference": first,
        "result_rows": comparison["result_rows"],
        "truth_rows": comparison["truth_rows"],
        "truth_digest": comparison["truth_digest"],
        "truth_source": comparison["truth_source"],
    }


def _metrics_payload(
    *,
    config: SampleConfig,
    scale_name: str,
    decision: dict[str, Any],
    join_metrics: JoinMetrics,
    clock: LogicalClock,
    round_start_clock: float,
    join_start_clock: float,
    join_end_clock: float,
    shuffle_commits: dict[str, list[dict[str, Any]]],
    result_commits: list[dict[str, Any]],
    assembly: dict[str, Any],
    truth: dict[str, Any],
    comparison: dict[str, Any],
    observation_digests: dict[str, str],
    truth_start_clock: float,
) -> dict[str, Any]:
    actual_peak = max(
        _actual_peak_partition_rows(shuffle_commits["left"]),
        _actual_peak_partition_rows(shuffle_commits["right"]),
        join_metrics.peak_partition_rows,
    )
    partition_clock_cost = sum(
        float(commit["clock_cost"])
        for commit in [*shuffle_commits["left"], *shuffle_commits["right"], *result_commits]
    )
    return {
        "measurement": {
            "duration_unit": "injected_logical_clock_tick",
            "logical_clock_step": config.execution.logical_clock_step,
            "round_start": round_start_clock,
            "round_end": clock.current,
            "duration_logical_ticks": clock.current - round_start_clock,
            "join_duration_logical_ticks": join_end_clock - join_start_clock,
            "verification_duration_logical_ticks": clock.current - truth_start_clock,
            "partition_clock_cost_total": partition_clock_cost,
            "wall_clock_used_for_decisions": False,
            "note": "all durations are injected logical ticks, not wall-clock time",
        },
        "scale": config.scale(scale_name).to_dict(),
        "requested_algorithm": decision["algorithm"],
        "algorithm_selection": decision_summary(decision),
        "join": join_metrics.to_dict(),
        "storage": {
            "partitions": config.execution.partitions,
            "left_partition_rows": [int(row["rows"]) for row in shuffle_commits["left"]],
            "right_partition_rows": [int(row["rows"]) for row in shuffle_commits["right"]],
            "left_partition_bytes": [int(row["bytes"]) for row in shuffle_commits["left"]],
            "right_partition_bytes": [int(row["bytes"]) for row in shuffle_commits["right"]],
            "result_partition_rows": [int(row["rows"]) for row in result_commits],
            "peak_partition_rows": actual_peak,
            "overflow_spill_bytes": join_metrics.spill_bytes,
        },
        "result": {
            "rows": assembly["rows"],
            "bytes": assembly["bytes"],
            "digest": assembly["digest"],
        },
        "observation_source": {
            "kind": "deterministic_generator_observed",
            "left_generated_rows_digest": observation_digests["left"],
            "right_generated_rows_digest": observation_digests["right"],
            "note": "selection uses observed key/partition distributions and serialized byte counts from deterministic generation",
        },
        "truth_source": {
            **truth["source"],
            "row_count": truth["row_count"],
            "digest": truth["digest"],
            "nested_loop_comparisons": truth["comparisons"],
            "kind": "fixed_order_nested_loop",
        },
        "verification": _verification_summary(comparison),
    }


def execute_query(
    config: SampleConfig,
    *,
    state_dir: str | Path,
    scale_name: str | None = None,
    partition_text: str | None = None,
    action: str = "run",
    crash_point: str | None = None,
) -> dict[str, Any]:
    scale = config.scale(scale_name)
    fingerprint = config.fingerprint(scale.name)
    partition_filter = _partition_filter(partition_text, config.execution.partitions)
    state = StateStore(state_dir)
    version_info = state.ensure_version(config.query.query_id, fingerprint)
    version = int(version_info["version"])
    current = state.get_current(config.query.query_id)
    assert current is not None
    if current["status"] == "completed":
        return {
            "ok": True,
            "command": action,
            "already_completed": True,
            "status": state.build_status(config.query.query_id),
        }
    clock = LogicalClock(
        config.execution.logical_clock_step,
        float(current["logical_clock"]),
    )
    round_start_clock = clock.current
    round_id = state.start_round(
        config.query.query_id, version, action, round_start_clock
    )
    crash = CrashInjector(crash_point)
    try:
        observations = build_observations(config, scale.name)
        clock.tick(scale.left_rows + scale.right_rows)
        for side, stats in observations.items():
            state.record_stats(
                config.query.query_id,
                version,
                side,
                stats["generated_rows_digest"],
                stats,
                clock.current,
            )
        decision = select_algorithm(
            observations["left"],
            observations["right"],
            config.execution,
            scale_name=scale.name,
        )
        state.record_decision(
            config.query.query_id,
            version,
            decision["algorithm"],
            decision,
            clock.current,
        )
        state.set_status(
            config.query.query_id, version, "shuffling", clock.current
        )
        left_commits = shuffle_side(
            state,
            query_id=config.query.query_id,
            version=version,
            side="left",
            table=config.table_map[config.query.left_table],
            seed=config.seed,
            rows_override=scale.left_rows,
            partitions=config.execution.partitions,
            input_digest=observations["left"]["generated_rows_digest"],
            partition_filter=partition_filter,
            crash=crash,
            clock=clock,
        )
        right_commits = shuffle_side(
            state,
            query_id=config.query.query_id,
            version=version,
            side="right",
            table=config.table_map[config.query.right_table],
            seed=config.seed,
            rows_override=scale.right_rows,
            partitions=config.execution.partitions,
            input_digest=observations["right"]["generated_rows_digest"],
            partition_filter=partition_filter,
            crash=crash,
            clock=clock,
        )
        del left_commits, right_commits
        all_left = state.get_partition_commits(
            config.query.query_id, version, "left"
        )
        all_right = state.get_partition_commits(
            config.query.query_id, version, "right"
        )
        if not _completed_commits(
            all_left, config.execution.partitions
        ) or not _completed_commits(all_right, config.execution.partitions):
            state.finish_round(
                round_id,
                status="waiting",
                finish_clock=clock.current,
                details={"reason": "shuffle partitions remain", "partitions": partition_text},
            )
            return {
                "ok": True,
                "command": action,
                "status": "waiting",
                "message": "selected work is committed; other shuffle partitions remain",
                "state": state.build_status(config.query.query_id),
            }
        left_row_sum = sum(int(commit["rows"]) for commit in all_left)
        right_row_sum = sum(int(commit["rows"]) for commit in all_right)
        if left_row_sum != int(observations["left"]["rows"]):
            fail(
                "E_STATE_CORRUPT",
                "left shuffle partition row sum does not equal generated input rows",
                details={
                    "expected": int(observations["left"]["rows"]),
                    "actual": left_row_sum,
                },
                exit_code=3,
            )
        if right_row_sum != int(observations["right"]["rows"]):
            fail(
                "E_STATE_CORRUPT",
                "right shuffle partition row sum does not equal generated input rows",
                details={
                    "expected": int(observations["right"]["rows"]),
                    "actual": right_row_sum,
                },
                exit_code=3,
            )
        join_start_clock = clock.current
        sink = StoredResultSink(
            state,
            query_id=config.query.query_id,
            version=version,
            requested_algorithm=decision["algorithm"],
            clock=clock,
            crash=crash,
            metrics=JoinMetrics(decision["algorithm"]),
        )
        join_metrics = execute_join(
            state=state,
            sink=sink,
            algorithm=decision["algorithm"],
            query=config.query,
            execution=config.execution,
            left_commits=all_left,
            right_commits=all_right,
            left_stats=observations["left"],
            right_stats=observations["right"],
            partition_filter=partition_filter,
            clock=clock,
        )
        join_end_clock = clock.current
        result_root = (
            state.root / "data" / f"v{version}" / "results"
        )
        if result_root.exists():
            for directory in result_root.glob("*"):
                if directory.is_dir():
                    cleanup_dead_temps(directory)
        result_commits = state.get_result_commits(config.query.query_id, version)
        if not _result_is_complete(result_commits, config.execution.partitions):
            state.finish_round(
                round_id,
                status="waiting",
                finish_clock=clock.current,
                details={"reason": "join result partitions remain", "partitions": partition_text},
            )
            return {
                "ok": True,
                "command": action,
                "status": "waiting",
                "message": "shuffle is complete; selected join partitions are committed",
                "state": state.build_status(config.query.query_id),
            }
        truth_start_clock = clock.current
        assembly = assemble_final_result(
            state=state,
            query_id=config.query.query_id,
            version=version,
            result_commits=result_commits,
            execution=config.execution,
            clock=clock,
        )
        truth = compute_truth(
            left_table=config.table_map[config.query.left_table],
            right_table=config.table_map[config.query.right_table],
            left_rows=scale.left_rows,
            right_rows=scale.right_rows,
            seed=config.seed,
            query=config.query,
            partitions=config.execution.partitions,
        )
        clock.tick(truth["comparisons"])
        final_path = state.path_for(assembly["path"])
        comparison = compare_truth(
            _read_result_rows(final_path),
            truth,
            tolerance_relative=config.execution.tolerance_relative,
            tolerance_absolute=config.execution.tolerance_absolute,
        )
        assembly["digest"] = digest_file(final_path)[0]
        if not comparison["ok"]:
            state.mark_failed(
                config.query.query_id,
                version,
                "result does not match nested-loop truth",
                logical_clock=clock.current,
            )
            fail(
                "E_VERIFY_FAILED",
                "selected algorithm result does not match nested-loop truth",
                details=_verification_summary(comparison),
                exit_code=3,
            )
        metrics = _metrics_payload(
            config=config,
            scale_name=scale.name,
            decision=decision,
            join_metrics=join_metrics,
            clock=clock,
            round_start_clock=round_start_clock,
            join_start_clock=join_start_clock,
            join_end_clock=join_end_clock,
            shuffle_commits={"left": all_left, "right": all_right},
            result_commits=result_commits,
            assembly=assembly,
            truth=truth,
            comparison=comparison,
            observation_digests={
                "left": observations["left"]["generated_rows_digest"],
                "right": observations["right"]["generated_rows_digest"],
            },
            truth_start_clock=truth_start_clock,
        )
        state.finalize_result(
            config.query.query_id,
            version,
            result_path=assembly["path"],
            result_digest=assembly["digest"],
            result_rows=assembly["rows"],
            result_bytes=assembly["bytes"],
            logical_clock=clock.current,
            metrics=metrics,
        )
        state.finish_round(
            round_id,
            status="completed",
            finish_clock=clock.current,
            details={
                "algorithm": decision["algorithm"],
                "result_digest": assembly["digest"],
                "result_rows": assembly["rows"],
            },
        )
        return {
            "ok": True,
            "command": action,
            "status": "completed",
            "metrics": metrics,
            "state": state.build_status(config.query.query_id),
        }
    except EngineError as exc:
        try:
            if exc.code not in {"E_VERSION_CONFLICT", "E_NOT_FOUND"}:
                try:
                    state.mark_failed(
                        config.query.query_id,
                        version,
                        f"{exc.code}: {exc.message}",
                        logical_clock=clock.current,
                    )
                except EngineError:
                    pass
            state.finish_round(
                round_id,
                status="failed",
                finish_clock=clock.current,
                details={"error_code": exc.code, "message": exc.message},
            )
        finally:
            raise


def status_query(config: SampleConfig, state_dir: str | Path) -> dict[str, Any]:
    path = Path(state_dir)
    if not (path / "state.sqlite3").exists():
        return {
            "ok": True,
            "query_id": config.query.query_id,
            "state": "absent",
            "partitions": {"left": [], "right": []},
            "result_partitions": [],
        }
    return StateStore(path, read_only=True).build_status(config.query.query_id)


def _verify_partition_side(
    *,
    state: StateStore,
    query: QuerySpec,
    version: int,
    side: str,
    join_key: str,
    stats: dict[str, Any],
    partitions: int,
) -> dict[str, Any]:
    commits = state.get_partition_commits(query.query_id, version, side)
    checks: list[str] = []
    errors: list[dict[str, Any]] = []
    if not _completed_commits(commits, partitions):
        errors.append(
            {
                "check": "partition_set",
                "message": "committed partition set is incomplete",
                "committed": [int(commit["partition_id"]) for commit in commits],
            }
        )
        return {"ok": False, "checks": checks, "errors": errors}
    checks.append("partition_set")
    total_rows = 0
    key_partition_owner: dict[str, int] = {}
    for commit in sorted(commits, key=lambda item: int(item["partition_id"])):
        partition_id = int(commit["partition_id"])
        path = state.path_for(commit["relative_path"])
        if not committed_file_valid(state, commit):
            errors.append(
                {
                    "check": "partition_file",
                    "partition": partition_id,
                    "message": "file missing or digest/size mismatch",
                }
            )
            continue
        row_count = count_jsonl_rows(path)
        if row_count != int(commit["rows"]):
            errors.append(
                {
                    "check": "partition_row_count",
                    "partition": partition_id,
                    "expected": int(commit["rows"]),
                    "actual": row_count,
                }
            )
        total_rows += int(commit["rows"])
        key_hash = __import__("hashlib").sha256()
        for row in iter_jsonl(path):
            key = row["values"][join_key]
            token = key_token(key)
            key_hash.update(token.encode("utf-8"))
            key_hash.update(b"\n")
            owner = key_partition_owner.setdefault(token, partition_id)
            if owner != partition_id:
                errors.append(
                    {
                        "check": "key_partition_split",
                        "partition": partition_id,
                        "other_partition": owner,
                        "key": key,
                    }
                )
            expected = partition_for_key(key, partitions)
            if expected != partition_id:
                errors.append(
                    {
                        "check": "partition_hash",
                        "partition": partition_id,
                        "expected": expected,
                        "key": key,
                    }
                )
        if key_hash.hexdigest() != commit["key_digest"]:
            errors.append(
                {
                    "check": "key_digest",
                    "partition": partition_id,
                    "message": "key digest mismatch",
                }
            )
    checks.extend(["partition_file", "partition_row_count", "key_partition_split", "partition_hash", "key_digest"])
    for commit in commits:
        if commit["input_digest"] != stats["generated_rows_digest"]:
            errors.append(
                {
                    "check": "partition_input_digest",
                    "partition": int(commit["partition_id"]),
                    "expected": stats["generated_rows_digest"],
                    "actual": commit["input_digest"],
                }
            )
    checks.append("partition_input_digest")
    if total_rows != int(stats["rows"]):
        errors.append(
            {
                "check": "partition_row_sum",
                "expected": int(stats["rows"]),
                "actual": total_rows,
            }
        )
    checks.append("partition_row_sum")
    return {"ok": not errors, "checks": checks, "errors": errors, "rows": total_rows}


def verify_query(
    config: SampleConfig,
    *,
    state_dir: str | Path,
    scale_name: str | None = None,
) -> dict[str, Any]:
    scale = config.scale(scale_name)
    state = StateStore(state_dir, read_only=True)
    current = state.get_current(config.query.query_id)
    errors: list[dict[str, Any]] = []
    checks: list[str] = []
    if current is None:
        fail("E_NOT_FOUND", "query state does not exist", exit_code=3)
    version = int(current["current_version"])
    expected_fingerprint = config.fingerprint(scale.name)
    if current["fingerprint"] != expected_fingerprint:
        errors.append(
            {
                "check": "query_fingerprint",
                "expected": expected_fingerprint,
                "actual": current["fingerprint"],
                "message": "state was created for a different sample/scale",
            }
        )
    checks.append("query_fingerprint")
    if current["status"] != "completed":
        errors.append(
            {
                "check": "query_status",
                "expected": "completed",
                "actual": current["status"],
            }
        )
    checks.append("query_status")
    persisted_stats = state.get_stats(config.query.query_id, version)
    regenerated = build_observations(config, scale.name)
    for side in ("left", "right"):
        if side not in persisted_stats:
            errors.append({"check": "stats_present", "side": side})
            continue
        if (
            persisted_stats[side]["digest"]
            != regenerated[side]["generated_rows_digest"]
        ):
            errors.append(
                {
                    "check": "stats_digest",
                    "side": side,
                    "persisted": persisted_stats[side]["digest"],
                    "regenerated": regenerated[side]["generated_rows_digest"],
                }
            )
        if int(persisted_stats[side]["stats"]["rows"]) != int(
            regenerated[side]["rows"]
        ):
            errors.append(
                {
                    "check": "stats_rows",
                    "side": side,
                    "persisted": persisted_stats[side]["stats"]["rows"],
                    "regenerated": regenerated[side]["rows"],
                }
            )
    checks.append("stats_digest")
    decision = state.get_decision(config.query.query_id, version)
    if decision is None:
        errors.append({"check": "algorithm_decision_present"})
    elif decision["algorithm"] != current["selected_algorithm"]:
        errors.append(
            {
                "check": "decision_selected_algorithm",
                "decision": decision["algorithm"],
                "current": current["selected_algorithm"],
            }
        )
    checks.append("algorithm_decision_present")

    side_reports: dict[str, Any] = {}
    if persisted_stats:
        for side, join_key in (
            ("left", config.query.left_key),
            ("right", config.query.right_key),
        ):
            report = _verify_partition_side(
                state=state,
                query=config.query,
                version=version,
                side=side,
                join_key=join_key,
                stats=persisted_stats[side]["stats"],
                partitions=config.execution.partitions,
            )
            side_reports[side] = report
            errors.extend(
                {"side": side, **error} for error in report.get("errors", [])
            )
    result_commits = state.get_result_commits(config.query.query_id, version)
    if not _result_is_complete(result_commits, config.execution.partitions):
        errors.append(
            {
                "check": "result_partition_set",
                "committed": [int(commit["partition_id"]) for commit in result_commits],
            }
        )
    for commit in result_commits:
        path = state.path_for(commit["relative_path"])
        if not path.exists():
            errors.append(
                {
                    "check": "result_partition_file",
                    "partition": int(commit["partition_id"]),
                    "message": "missing",
                }
            )
            continue
        digest, size = digest_file(path)
        if digest != commit["digest"] or size != int(commit["bytes"]):
            errors.append(
                {
                    "check": "result_partition_file",
                    "partition": int(commit["partition_id"]),
                    "message": "digest or size mismatch",
                }
            )
            continue
        rows = count_jsonl_rows(path)
        if rows != int(commit["rows"]):
            errors.append(
                {
                    "check": "result_partition_rows",
                    "partition": int(commit["partition_id"]),
                    "expected": int(commit["rows"]),
                    "actual": rows,
                }
            )
        if commit["actual_algorithm"] not in {
            "broadcast_hash",
            "shuffled_hash",
            "sort_merge",
        }:
            errors.append(
                {
                    "check": "result_partition_algorithm",
                    "partition": int(commit["partition_id"]),
                    "actual": commit["actual_algorithm"],
                }
            )
    checks.append("result_partition_set")
    checks.append("result_partition_file")
    ledger = state.get_ledger(config.query.query_id, version)
    pointer = {
        "path": current["result_path"],
        "digest": current["result_digest"],
        "rows": current["result_rows"],
    }
    if ledger is None:
        errors.append({"check": "result_ledger_present"})
    else:
        if (
            ledger["result_path"] != pointer["path"]
            or ledger["result_digest"] != pointer["digest"]
            or int(ledger["result_rows"]) != int(pointer["rows"] or 0)
        ):
            errors.append(
                {
                    "check": "ledger_pointer",
                    "message": "ledger and current result pointer differ",
                }
            )
        if int(ledger["result_rows"]) != sum(
            int(commit["rows"]) for commit in result_commits
        ):
            errors.append(
                {
                    "check": "result_partition_row_sum",
                    "ledger": int(ledger["result_rows"]),
                    "partitions": sum(int(commit["rows"]) for commit in result_commits),
                }
            )
    checks.append("ledger_pointer")
    comparison = None
    final_rows = 0
    final_digest = None
    if pointer["path"]:
        final_path = state.path_for(pointer["path"])
        if not final_path.exists():
            errors.append({"check": "result_file", "message": "final result file missing"})
        else:
            final_digest, _ = digest_file(final_path)
            if final_digest != pointer["digest"]:
                errors.append(
                    {
                        "check": "result_file_digest",
                        "expected": pointer["digest"],
                        "actual": final_digest,
                    }
                )
            rows = list(iter_jsonl(final_path))
            final_rows = len(rows)
            if final_rows != int(pointer["rows"] or 0):
                errors.append(
                    {
                        "check": "result_file_rows",
                        "expected": int(pointer["rows"] or 0),
                        "actual": final_rows,
                    }
                )
            if rows != sorted(rows, key=result_order_key):
                errors.append(
                    {
                        "check": "result_file_order",
                        "message": "final result is not in canonical order",
                    }
                )
            truth = compute_truth(
                left_table=config.table_map[config.query.left_table],
                right_table=config.table_map[config.query.right_table],
                left_rows=scale.left_rows,
                right_rows=scale.right_rows,
                seed=config.seed,
                query=config.query,
                partitions=config.execution.partitions,
            )
            comparison = compare_truth(rows, truth)
            if not comparison["ok"]:
                errors.append(
                    {
                        "check": "truth_comparison",
                        "mismatch_count": comparison["mismatch_count"],
                        "first_difference": comparison["first_difference"],
                    }
                )
    checks.extend(["result_file", "result_file_digest", "result_file_rows", "result_file_order", "truth_comparison"])
    report = {
        "ok": not errors,
        "command": "verify",
        "query_id": config.query.query_id,
        "version": version,
        "checks": checks,
        "errors": errors,
        "left": side_reports.get("left"),
        "right": side_reports.get("right"),
        "result": {
            "pointer": pointer,
            "ledger": ledger,
            "rows": final_rows,
            "digest": final_digest,
            "comparison": comparison,
        },
        "decision": decision["decision"] if decision else None,
    }
    if errors:
        fail(
            "E_VERIFY_FAILED",
            "persistent state and result verification failed",
            details=report,
            exit_code=3,
        )
    return report


def _local_algorithm_run(
    *,
    algorithm: str,
    config: SampleConfig,
    scale: Any,
    observations: dict[str, dict[str, Any]],
    left_commits: list[dict[str, Any]],
    right_commits: list[dict[str, Any]],
    truth: dict[str, Any],
    benchmark_root: Path,
) -> dict[str, Any]:
    from .result_sink import LocalResultSink

    state = StateStore(benchmark_root / "state")
    clock = LogicalClock(config.execution.logical_clock_step, 0.0)
    metrics = JoinMetrics(algorithm)
    sink = LocalResultSink(
        benchmark_root / "results",
        requested_algorithm=algorithm,
        clock=clock,
        metrics=metrics,
    )
    join_metrics = execute_join(
        state=state,
        sink=sink,
        algorithm=algorithm,
        query=config.query,
        execution=config.execution,
        left_commits=left_commits,
        right_commits=right_commits,
        left_stats=observations["left"],
        right_stats=observations["right"],
        partition_filter=None,
        clock=clock,
    )
    rows: list[dict[str, Any]] = []
    for commit in sorted(join_metrics.actual_algorithms.keys()):
        if commit not in sink.commits:
            continue
        rows.extend(iter_jsonl(Path(sink.commits[commit]["relative_path"])))
    rows.sort(key=result_order_key)
    comparison = compare_truth(rows, truth)
    peak = max(
        _actual_peak_partition_rows(left_commits),
        _actual_peak_partition_rows(right_commits),
        join_metrics.peak_partition_rows,
    )
    return {
        "algorithm": algorithm,
        "result_rows": len(rows),
        "result_digest": sha256_value(rows),
        "verification": _verification_summary(comparison),
        "peak_partition_rows": peak,
        "overflow_spill_bytes": join_metrics.spill_bytes,
        "duration_logical_ticks": clock.elapsed,
        "join": join_metrics.to_dict(),
        "measurement": {
            "duration_unit": "injected_logical_clock_tick",
            "logical_clock_step": config.execution.logical_clock_step,
            "wall_clock_used_for_decisions": False,
        },
    }


def benchmark_query(
    config: SampleConfig,
    *,
    state_dir: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(state_dir).resolve() / "benchmark"
    root.mkdir(parents=True, exist_ok=True)
    scale_reports = []
    for scale in config.execution.scale_samples:
        observations = build_observations(config, scale.name)
        decision = select_algorithm(
            observations["left"],
            observations["right"],
            config.execution,
            scale_name=scale.name,
        )
        scale_root = root / scale.name
        shuffle_root = scale_root / "shuffle"
        shuffle_clock = LogicalClock(config.execution.logical_clock_step, 0.0)
        left_commits = shuffle_side_local(
            table=config.table_map[config.query.left_table],
            seed=config.seed,
            rows_override=scale.left_rows,
            partitions=config.execution.partitions,
            directory=shuffle_root / "left",
            clock=shuffle_clock,
        )
        right_commits = shuffle_side_local(
            table=config.table_map[config.query.right_table],
            seed=config.seed,
            rows_override=scale.right_rows,
            partitions=config.execution.partitions,
            directory=shuffle_root / "right",
            clock=shuffle_clock,
        )
        truth = compute_truth(
            left_table=config.table_map[config.query.left_table],
            right_table=config.table_map[config.query.right_table],
            left_rows=scale.left_rows,
            right_rows=scale.right_rows,
            seed=config.seed,
            query=config.query,
            partitions=config.execution.partitions,
        )
        algorithm_reports = []
        for algorithm in ("broadcast_hash", "shuffled_hash", "sort_merge"):
            algorithm_root = scale_root / "algorithms" / algorithm
            if algorithm_root.exists():
                import shutil

                shutil.rmtree(algorithm_root)
            algorithm_reports.append(
                _local_algorithm_run(
                    algorithm=algorithm,
                    config=config,
                    scale=scale,
                    observations=observations,
                    left_commits=left_commits,
                    right_commits=right_commits,
                    truth=truth,
                    benchmark_root=algorithm_root,
                )
            )
        scale_reports.append(
            {
                "scale": scale.to_dict(),
                "observations": {
                    "source": "deterministic_generator_observed",
                    "left": stats_summary(observations["left"]),
                    "right": stats_summary(observations["right"]),
                },
                "selection": decision,
                "selected_algorithm": decision["algorithm"],
                "shuffle": {
                    "duration_logical_ticks": shuffle_clock.elapsed,
                    "left_partition_rows": [int(row["rows"]) for row in left_commits],
                    "right_partition_rows": [int(row["rows"]) for row in right_commits],
                },
                "truth": {
                    "rows": truth["row_count"],
                    "digest": truth["digest"],
                    "comparisons": truth["comparisons"],
                    "source": truth["source"],
                },
                "algorithms": algorithm_reports,
            }
        )
    report = {
        "ok": all(
            algorithm["verification"]["ok"]
            for scale in scale_reports
            for algorithm in scale["algorithms"]
        ),
        "command": "benchmark",
        "query_id": config.query.query_id,
        "seed": config.seed,
        "measurement_policy": {
            "duration_unit": "injected_logical_clock_tick",
            "logical_clock_step": config.execution.logical_clock_step,
            "wall_clock_used_for_decisions": False,
            "comparison_dimensions": [
                "duration_logical_ticks",
                "peak_partition_rows",
                "overflow_spill_bytes",
                "result_rows",
            ],
            "observation_source": "deterministic generator full-pass stats",
            "truth_source": "fixed-order nested-loop truth",
        },
        "scales": scale_reports,
    }
    if not report["ok"]:
        fail("E_VERIFY_FAILED", "benchmark algorithms disagree with truth", details=report, exit_code=3)
    if output_path is None:
        output_path = Path(state_dir) / "benchmark_report.json"
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json_dumps_stable(report) + "\n", encoding="utf-8")
    state = StateStore(state_dir)
    state.record_benchmark(report, sha256_value(report))
    report["output_path"] = str(output.resolve())
    return report
