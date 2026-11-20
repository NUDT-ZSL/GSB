from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from .canonical import canonical_dumps, sha256_bytes, sha256_file, strict_json_loads
from .errors import (
    E_DIGEST_MISMATCH,
    E_RECOVERY_FAILED,
    E_SPILL_READBACK_FAILED,
    E_VERIFY_FAILED,
    E_WINDOW_MISMATCH,
    EngineError,
)
from .models import (
    Batch,
    GroupState,
    Plan,
    StatState,
    aggregate_result_payload,
    group_hash,
    window_result_payload,
)
from .numeric import convert_fixed
from .spill import SpillManager
from .storage import Store, compute_state_digest
from .validation import parse_batch


def verify_workspace(store: Store, plan: Plan) -> dict[str, Any]:
    with store.transaction(immediate=False):
        return _verify_workspace(store, plan)


def _verify_workspace(store: Store, plan: Plan) -> dict[str, Any]:
    mode = plan.config.numeric_mode
    issues: list[dict[str, Any]] = []
    batch_rows = store.conn.execute(
        "SELECT * FROM batches WHERE mode = ? ORDER BY batch_seq",
        (mode,),
    ).fetchall()

    batches: dict[int, Batch] = {}
    for row in batch_rows:
        try:
            raw = strict_json_loads(row["payload_json"])
            batch = parse_batch(
                raw,
                plan.config,
                expected_seq=int(row["batch_seq"]),
            )
        except EngineError as exc:
            issues.append(
                {
                    "code": exc.code,
                    "batch_seq": int(row["batch_seq"]),
                    "message": exc.message,
                    "details": exc.details,
                }
            )
            continue
        if batch.digest != row["batch_digest"]:
            issues.append(
                {
                    "code": E_DIGEST_MISMATCH,
                    "batch_seq": batch.batch_seq,
                    "message": "stored batch digest is inconsistent",
                }
            )
        batches[batch.batch_seq] = batch

    expected_state_digest = compute_state_digest(store.conn)
    actual_state_digest = store.state_digest()
    if expected_state_digest != actual_state_digest:
        issues.append(
            {
                "code": E_VERIFY_FAILED,
                "message": "state_digest does not match persisted state rows",
                "expected": expected_state_digest,
                "actual": actual_state_digest,
            }
        )

    spill = SpillManager(store.root, plan.config.spill_dir)
    group_rows = store.conn.execute(
        "SELECT * FROM group_states WHERE mode = ? ORDER BY group_hash",
        (mode,),
    ).fetchall()
    for row in group_rows:
        try:
            if row["state_json"] is not None:
                state_json = row["state_json"]
                state = GroupState.from_payload(strict_json_loads(state_json), mode)
            else:
                spill_row = store.conn.execute(
                    "SELECT * FROM spill_files WHERE spill_id = ?",
                    (row["spill_id"],),
                ).fetchone()
                if spill_row is None:
                    raise EngineError(
                        E_RECOVERY_FAILED,
                        "group state references a missing spill file row",
                        group_hash=row["group_hash"],
                    )
                read = spill.read(
                    mode=mode,
                    spill_id=spill_row["spill_id"],
                    rel_path=spill_row["rel_path"],
                    expected_digest=spill_row["sha256"],
                )
                state = read.state
                state_json = canonical_dumps(state.as_payload(mode))
            state_digest = sha256_bytes(state_json.encode("utf-8"))
            if state_digest != row["state_digest"]:
                issues.append(
                    {
                        "code": E_RECOVERY_FAILED,
                        "message": "group state digest mismatch",
                        "group_hash": row["group_hash"],
                        "expected": row["state_digest"],
                        "actual": state_digest,
                    }
                )
            if state.group_hash != row["group_hash"]:
                issues.append(
                    {
                        "code": E_RECOVERY_FAILED,
                        "message": "group hash is inconsistent",
                        "group_hash": row["group_hash"],
                        "state_group_hash": state.group_hash,
                    }
                )
            if state.version != int(row["version"]):
                issues.append(
                    {
                        "code": E_RECOVERY_FAILED,
                        "message": "group version is inconsistent",
                        "group_hash": row["group_hash"],
                        "expected": int(row["version"]),
                        "actual": state.version,
                    }
                )
        except EngineError as exc:
            issues.append(
                {
                    "code": exc.code,
                    "group_hash": row["group_hash"],
                    "message": exc.message,
                    "details": exc.details,
                }
            )

    for row in store.conn.execute(
        "SELECT * FROM spill_files WHERE mode = ? ORDER BY spill_id",
        (mode,),
    ).fetchall():
        path = store.root / row["rel_path"]
        if not path.exists():
            issues.append(
                {
                    "code": E_SPILL_READBACK_FAILED,
                    "message": "spill file is missing",
                    "spill_id": row["spill_id"],
                }
            )
            continue
        actual = sha256_file(path)
        if actual != row["sha256"]:
            issues.append(
                {
                    "code": E_SPILL_READBACK_FAILED,
                    "message": "spill file checksum mismatch",
                    "spill_id": row["spill_id"],
                    "expected": row["sha256"],
                    "actual": actual,
                }
            )

    commits = store.conn.execute(
        """
        SELECT batch_seq, group_hash, version FROM group_commits
        WHERE mode = ? ORDER BY batch_seq, group_hash
        """,
        (mode,),
    ).fetchall()
    commit_keys: set[tuple[int, str]] = set()
    for row in commits:
        key = (int(row["batch_seq"]), str(row["group_hash"]))
        if key in commit_keys:
            issues.append(
                {
                    "code": E_VERIFY_FAILED,
                    "message": "duplicate group commit",
                    "batch_seq": key[0],
                    "group_hash": key[1],
                }
            )
        commit_keys.add(key)

    failed_group_codes = {
        str(row["group_hash"]): str(row["error_code"])
        for row in store.conn.execute(
            "SELECT group_hash, error_code FROM group_failures WHERE mode = ?",
            (mode,),
        ).fetchall()
    }
    window_check = _verify_windows(
        store,
        plan,
        batches,
        batch_rows,
        failed_group_codes,
    )
    aggregate_check = _verify_aggregates(
        store,
        plan,
        batches,
        batch_rows,
        failed_group_codes,
    )
    if window_check["divergences"]:
        issues.append(
            {
                "code": E_WINDOW_MISMATCH,
                "message": "window result diverges from full recomputation",
                "first_divergence": window_check["divergences"][0],
            }
        )
    if aggregate_check["mismatches"]:
        issues.append(
            {
                "code": E_VERIFY_FAILED,
                "message": "aggregate result diverges from full recomputation",
                "first_mismatch": aggregate_check["mismatches"][0],
                "mismatch_count": len(aggregate_check["mismatches"]),
            }
        )

    return {
        "ok": not issues,
        "mode": mode,
        "plan_digest": plan.plan_digest,
        "state_digest": actual_state_digest,
        "state_digest_matches": expected_state_digest == actual_state_digest,
        "batches_checked": len(batches),
        "groups_checked": len(group_rows),
        "spill_files_checked": len(
            store.conn.execute(
                "SELECT 1 FROM spill_files WHERE mode = ?",
                (mode,),
            ).fetchall()
        ),
        "windows": window_check,
        "aggregates": aggregate_check,
        "issues": issues,
    }


def _verify_windows(
    store: Store,
    plan: Plan,
    batches: dict[int, Batch],
    batch_rows: list[Any],
    failed_group_codes: dict[str, str],
) -> dict[str, Any]:
    mode = plan.config.numeric_mode
    committed = {
        int(row["batch_seq"]) for row in batch_rows if row["status"] == "committed"
    }
    sequences: dict[tuple[Any, ...], list[float]] = defaultdict(list)
    key_lookup: dict[str, tuple[Any, ...]] = {}
    group_columns = plan.query.group_by
    window_column = plan.query.window.column
    for batch_seq in sorted(committed):
        batch = batches.get(batch_seq)
        if batch is None:
            continue
        columns = batch.by_name
        group_cols = [columns[name] for name in group_columns]
        value_col = columns[window_column]
        for index in range(batch.row_count):
            key = tuple(column.values[index] for column in group_cols)
            key_hash_value = group_hash(key)
            key_lookup[key_hash_value] = key
            sequences[key].append(float(value_col.values[index]))

    expected: dict[tuple[str, int], dict[str, Any]] = {}
    length = plan.query.window.length
    stride = plan.query.window.stride
    for key, values in sequences.items():
        key_hash_value = group_hash(key)
        for start in range(0, len(values) - length + 1, stride):
            try:
                payload = window_result_payload(
                    mode,
                    values[start : start + length],
                    start,
                    plan.config.fixed_scale,
                    plan.config.fixed_min,
                    plan.config.fixed_max,
                )
            except EngineError:
                continue
            payload = _filter_window_payload(payload, plan.query.window.outputs)
            expected[(key_hash_value, start)] = payload

    actual: dict[tuple[str, int], dict[str, Any]] = {}
    actual_rows = store.conn.execute(
        """
        SELECT group_hash, group_key_json, window_start, value_json
        FROM results WHERE mode = ? AND kind = 'window'
        ORDER BY group_hash, window_start
        """,
        (mode,),
    ).fetchall()
    for row in actual_rows:
        key = (str(row["group_hash"]), int(row["window_start"]))
        actual[key] = strict_json_loads(row["value_json"])

    divergences: list[dict[str, Any]] = []
    all_keys = sorted(set(expected) | set(actual), key=lambda item: (item[0], item[1]))
    for key_hash_value, start in all_keys:
        failed_code = failed_group_codes.get(key_hash_value)
        if failed_code is not None and failed_code != E_WINDOW_MISMATCH:
            continue
        exp = expected.get((key_hash_value, start))
        act = actual.get((key_hash_value, start))
        if canonical_dumps(exp) != canonical_dumps(act):
            key = key_lookup.get(key_hash_value)
            divergences.append(
                {
                    "group": list(key) if key is not None else None,
                    "group_hash": key_hash_value,
                    "window_start": start,
                    "expected": exp,
                    "actual": act,
                    "intermediate": {
                        "expected_sum": exp.get("sum") if exp else None,
                        "actual_sum": act.get("sum") if act else None,
                    },
                }
            )
    return {
        "groups": len(sequences),
        "expected_windows": len(expected),
        "actual_windows": len(actual),
        "divergences": divergences,
    }


def _verify_aggregates(
    store: Store,
    plan: Plan,
    batches: dict[int, Batch],
    batch_rows: list[Any],
    failed_group_codes: dict[str, str],
) -> dict[str, Any]:
    mode = plan.config.numeric_mode
    failed_groups = set(failed_group_codes)
    committed = [
        int(row["batch_seq"]) for row in batch_rows if row["status"] == "committed"
    ]
    states: dict[str, GroupState] = {}
    for batch_seq in sorted(committed):
        batch = batches.get(batch_seq)
        if batch is None:
            continue
        columns = batch.by_name
        group_cols = [columns[name] for name in plan.query.group_by]
        value_cols = {name: columns[name] for name in plan.query.value_columns}
        for index in range(batch.row_count):
            key = tuple(column.values[index] for column in group_cols)
            key_hash_value = group_hash(key)
            if key_hash_value in failed_groups:
                continue
            state = states.get(key_hash_value)
            if state is None:
                state = GroupState(
                    key=key,
                    group_hash=key_hash_value,
                    version=0,
                    row_count=0,
                    stats={name: StatState() for name in plan.query.value_columns},
                    window_values=[],
                    window_base=0,
                    last_touch=0,
                )
                states[key_hash_value] = state
            _truth_accumulate(plan, state, value_cols, index)

    expected: dict[tuple[str, str], dict[str, Any]] = {}
    for key_hash_value, state in states.items():
        for aggregate in plan.query.aggregates:
            stat = state.stats.get(aggregate.column, StatState())
            expected[(key_hash_value, aggregate.name)] = aggregate_result_payload(
                mode,
                aggregate.op,
                stat,
                state.row_count,
                plan.config.fixed_scale,
            )
    actual: dict[tuple[str, str], dict[str, Any]] = {}
    for row in store.conn.execute(
        """
        SELECT group_hash, item_id, value_json FROM results
        WHERE mode = ? AND kind = 'aggregate'
        ORDER BY group_hash, item_id
        """,
        (mode,),
    ).fetchall():
        actual[(str(row["group_hash"]), str(row["item_id"]))] = strict_json_loads(
            row["value_json"]
        )

    mismatches: list[dict[str, Any]] = []
    for key_hash_value, item_id in sorted(set(expected) | set(actual)):
        if key_hash_value in failed_groups:
            continue
        exp = expected.get((key_hash_value, item_id))
        act = actual.get((key_hash_value, item_id))
        if canonical_dumps(exp) != canonical_dumps(act):
            mismatches.append(
                {
                    "group_hash": key_hash_value,
                    "item_id": item_id,
                    "expected": exp,
                    "actual": act,
                }
            )
    return {
        "groups": len(states),
        "aggregates": len(expected),
        "mismatches": mismatches,
    }


def _truth_accumulate(
    plan: Plan,
    state: GroupState,
    value_cols: dict[str, Any],
    index: int,
) -> None:
    state.row_count += 1
    for column_name in plan.query.value_columns:
        column = value_cols[column_name]
        raw = column.values[index]
        stat = state.stats.setdefault(column_name, StatState())
        stat.total_count += 1
        if column.type == "float" and math.isnan(float(raw)):
            stat.nan_count += 1
            continue
        if column.type == "float" and math.isinf(float(raw)):
            if float(raw) > 0:
                stat.pos_inf_count += 1
            else:
                stat.neg_inf_count += 1
            continue
        stat.finite_count += 1
        if plan.config.numeric_mode == "fixed":
            converted = convert_fixed(
                raw,
                plan.config.fixed_scale,
                plan.config.fixed_min,
                plan.config.fixed_max,
            )
            scaled = converted.scaled
            if scaled is None:
                continue
            stat.total = int(stat.total) + scaled
            stat.square_total = int(stat.square_total) + scaled * scaled
        else:
            value = float(raw)
            stat.total = float(stat.total) + value
            stat.square_total = float(stat.square_total) + value * value


def _filter_window_payload(
    payload: dict[str, Any],
    outputs: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "window_start": payload["window_start"],
        "window_end": payload["window_end"],
        "count": payload["count"],
        "finite_count": payload["finite_count"],
        "nan_count": payload["nan_count"],
        "pos_inf_count": payload["pos_inf_count"],
        "neg_inf_count": payload["neg_inf_count"],
        **{op: payload[op] for op in ("count", "sum", "mean", "variance") if op in outputs},
    }
