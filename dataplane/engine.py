from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .db import connect, get_meta, immediate
from .errors import BizError, ErrorCode
from .hashing import (
    canonical_digest,
    canonical_json,
    digest_object_path,
    sha256_bytes,
    sha256_file,
)
from .runtime import file_lock, inject_operator_failure, maybe_crash
from .tensors import block_bounds, generate_distribution_array


def row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def get_case(conn: sqlite3.Connection, case_id: str) -> Dict[str, Any]:
    row = conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if row is None:
        raise BizError(ErrorCode.BLOCK_NOT_FOUND, "case not found", {"case_id": case_id})
    result = row_to_dict(row)
    result["block_plan"] = json.loads(result["block_plan_json"])
    result["manifest"] = json.loads(result["manifest_json"])
    return result


def active_config_row(conn: sqlite3.Connection) -> Dict[str, Any]:
    version = get_meta(conn, "active_config_version")
    if version is None:
        row = conn.execute(
            "SELECT * FROM config_snapshot ORDER BY config_version DESC LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM config_snapshot WHERE config_version=?", (int(version),)
        ).fetchone()
    if row is None:
        raise BizError(ErrorCode.STATE_CORRUPT, "no active config snapshot is installed")
    result = row_to_dict(row)
    result["merged"] = json.loads(result["merged_json"])
    result["manifest"] = json.loads(result["manifest_json"])
    return result


def config_row_by_version(conn: sqlite3.Connection, config_version: int) -> Dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM config_snapshot WHERE config_version=?", (int(config_version),)
    ).fetchone()
    if row is None:
        raise BizError(
            ErrorCode.STATE_CORRUPT,
            "bound config version is missing",
            {"config_version": config_version},
        )
    result = row_to_dict(row)
    result["merged"] = json.loads(result["merged_json"])
    result["manifest"] = json.loads(result["manifest_json"])
    return result


def get_descriptors(
    conn: sqlite3.Connection, case_id: str
) -> Dict[Tuple[str, int], Dict[str, Any]]:
    rows = conn.execute(
        "SELECT descriptor_json FROM tensor_blocks WHERE case_id=?", (case_id,)
    ).fetchall()
    result: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for row in rows:
        descriptor = json.loads(row["descriptor_json"])
        result[(descriptor["role"], int(descriptor["block_index"]))] = descriptor
    return result


def block_cost_ms(case: Dict[str, Any], block_index: int) -> int:
    start, end = block_bounds(
        int(case["data_size"]), case["block_plan"], block_index
    )
    rows = max(1, end - start)
    elements = rows * (int(case["feature_dim"]) + int(case["label_dim"]))
    return max(1, math.ceil(elements / 256.0))


def logical_uid(case_id: str, block_index: int, config_version: int) -> str:
    return f"{case_id}|op|b{block_index:04d}|cfg{config_version}"


def build_input_chain(
    case: Dict[str, Any],
    descriptors: Dict[Tuple[str, int], Dict[str, Any]],
    block_index: int,
    config: Dict[str, Any],
    effective_patches: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    feature = descriptors[("feature", block_index)]
    label = descriptors[("label", block_index)]
    start, end = block_bounds(case["data_size"], case["block_plan"], block_index)
    patch_entries = []
    for patch in sorted(
        effective_patches,
        key=lambda p: (
            int(p.get("patch_generation", 1)),
            int(p.get("logical_clock_ms", 0)),
            p.get("patch_id", ""),
        ),
    ):
        patch_entries.append(
            {
                "patch_id": patch["patch_id"],
                "patch_generation": patch["patch_generation"],
                "logical_clock_ms": patch["logical_clock_ms"],
                "patch_kind": patch["patch_kind"],
                "new_digest": patch["new_digest"],
                "block_range": patch["block_range"],
            }
        )
    chain = [
        {"kind": "case", "case_id": case["case_id"], "digest": case["case_digest"]},
        {
            "kind": "block_bounds",
            "block_index": block_index,
            "row_range": [start, end],
        },
        {"kind": "tensor", "role": "feature", "digest": feature["digest"]},
        {"kind": "tensor", "role": "label", "digest": label["digest"]},
        {
            "kind": "config",
            "config_version": config["config_version"],
            "policy_generation": config["merged"]["policy_generation"],
            "digest": config["merged_digest"],
        },
        {"kind": "effective_patches", "items": patch_entries},
        {
            "kind": "reference_edges",
            "carry_dependency": bool(case["block_plan"].get("carry_dependency", False)),
            "edges": [[block_index - 1, block_index]] if block_index > 0 and case["block_plan"].get("carry_dependency") else [],
        },
    ]
    return chain


def ensure_block_task(
    conn: sqlite3.Connection,
    case_id: str,
    block_index: int,
    effective_patches: List[Dict[str, Any]],
    clock_ms: int,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    case = get_case(conn, case_id)
    if config is None:
        config = active_config_row(conn)
    descriptors = get_descriptors(conn, case_id)
    chain = build_input_chain(case, descriptors, block_index, config, effective_patches)
    chain_digest = canonical_digest(chain)
    config_version = int(config["config_version"])
    existing = conn.execute(
        "SELECT * FROM block_task WHERE case_id=? AND block_index=? AND block_kind='op' "
        "AND config_version=? AND input_chain_digest=?",
        (case_id, block_index, config_version, chain_digest),
    ).fetchone()
    if existing is not None:
        return {"task": row_to_dict(existing), "created": False}

    uid_logical = logical_uid(case_id, block_index, config_version)
    revision_row = conn.execute(
        "SELECT COALESCE(MAX(revision), 0) + 1 AS next_revision "
        "FROM block_task WHERE logical_uid=?",
        (uid_logical,),
    ).fetchone()
    revision = int(revision_row["next_revision"])
    task_uid = f"{uid_logical}|r{revision:03d}|{chain_digest.removeprefix('sha256:')[:12]}"
    cost = block_cost_ms(case, block_index)
    patches_json = json.dumps(effective_patches, sort_keys=True, separators=(",", ":"))
    conn.execute(
        "INSERT INTO block_task("
        "task_uid,logical_uid,case_id,block_index,block_kind,config_version,"
        "policy_generation,revision,input_chain_json,input_chain_digest,"
        "effective_patches_json,cost_ms,state,attempt,created_clock_ms,updated_clock_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pending',0,?,?)",
        (
            task_uid,
            uid_logical,
            case_id,
            block_index,
            "op",
            config_version,
            int(config["merged"]["policy_generation"]),
            revision,
            json.dumps(chain, sort_keys=True, separators=(",", ":")),
            chain_digest,
            patches_json,
            cost,
            clock_ms,
            clock_ms,
        ),
    )
    row = conn.execute("SELECT * FROM block_task WHERE task_uid=?", (task_uid,)).fetchone()
    return {"task": row_to_dict(row), "created": True}


def plan_baseline_blocks(
    conn: sqlite3.Connection, clock_ms: int, case_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    if case_id:
        cases = [get_case(conn, case_id)]
    else:
        case_ids = [
            r["case_id"]
            for r in conn.execute("SELECT case_id FROM cases ORDER BY case_id").fetchall()
        ]
        cases = [get_case(conn, case_id) for case_id in case_ids]
    planned = []
    with immediate(conn):
        config = active_config_row(conn)
        for case in cases:
            count = int(block_count_from_case(case))
            for index in range(count):
                result = ensure_block_task(conn, case["case_id"], index, [], clock_ms, config)
                planned.append(result)
    return planned


def block_count_from_case(case: Dict[str, Any]) -> int:
    from .tensors import block_count

    return block_count(int(case["data_size"]), case["block_plan"])


def _quantize_dequantize(
    values: np.ndarray, scale: float, zero_point: float
) -> Tuple[np.ndarray, Dict[str, Any]]:
    qmin = -1_000_000
    qmax = 1_000_000
    zero = int(round(float(zero_point)))
    codes = np.clip(np.rint(values / scale) + zero, qmin, qmax)
    restored = (codes - zero) * scale
    record = {
        "quant_scale": scale,
        "zero_point": zero,
        "qmin": qmin,
        "qmax": qmax,
        "max_abs_round_error": float(np.max(np.abs(values - restored))),
        "mean_abs_round_error": float(np.mean(np.abs(values - restored))),
    }
    return restored.astype(np.float64), record


def _build_materialization(
    conn: sqlite3.Connection, task: Dict[str, Any]
) -> Tuple[bytes, str, Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    case_id = task["case_id"]
    block_index = int(task["block_index"])
    case = get_case(conn, case_id)
    descriptors = get_descriptors(conn, case_id)
    config = config_row_by_version(conn, int(task["config_version"]))
    feature_desc = descriptors[("feature", block_index)]
    label_desc = descriptors[("label", block_index)]
    x = generate_distribution_array(
        feature_desc["shape"], feature_desc["dtype"], feature_desc["distribution"]
    ).astype(np.float64)
    y = generate_distribution_array(
        label_desc["shape"], label_desc["dtype"], label_desc["distribution"]
    ).astype(np.float64)
    effective_patches = json.loads(task["effective_patches_json"])
    chain = json.loads(task["input_chain_json"])
    chain_digest = task["input_chain_digest"]

    seed = int(chain_digest.removeprefix("sha256:")[:16], 16) % (2**32)
    rng = np.random.default_rng(seed)
    feature_dim = int(case["feature_dim"])
    label_dim = int(case["label_dim"])
    weights = rng.normal(0.0, 0.12, size=(feature_dim, label_dim))
    bias = np.zeros(label_dim, dtype=np.float64)
    gain = 1.0
    patch_trace = []
    for patch in sorted(
        effective_patches,
        key=lambda p: (int(p["patch_generation"]), int(p["logical_clock_ms"]), p["patch_id"]),
    ):
        content = patch.get("patch_content")
        if isinstance(content, str):
            content = json.loads(content)
        content = content or {}
        patch_rng = np.random.default_rng(int(content.get("weight_seed", 1)))
        if patch["patch_kind"] == "weight_update":
            weights = weights + float(content.get("weight_scale_delta", 0.05)) * patch_rng.normal(
                0.0, 0.12, size=weights.shape
            )
            bias = bias + float(content.get("bias", 0.0))
        elif patch["patch_kind"] == "activation_recompute":
            gain *= float(content.get("activation_gain", 1.05))
        patch_trace.append(
            {
                "patch_id": patch["patch_id"],
                "kind": patch["patch_kind"],
                "generation": patch["patch_generation"],
                "digest": patch["new_digest"],
            }
        )

    xq, quant_record = _quantize_dequantize(
        x, float(case["quant_scale"]), float(case["quant_zero_point"])
    )
    policies = config["merged"]["policies"]
    fixed_scale = float(policies.get("quant.fixed_point_scale", 256))
    tolerance = float(policies.get("inference.tolerance", 0.12))
    weights_q = np.round(weights * fixed_scale) / fixed_scale
    weight_round_bound = 1.0 / (2.0 * fixed_scale)

    exact_scores = gain * (x @ weights + bias)
    fixed_scores = gain * (xq @ weights_q + bias)
    exact_pred = np.mean(exact_scores, axis=0)
    fixed_pred = np.mean(fixed_scores, axis=0)
    candidate_exact = float(np.sum(exact_pred))
    candidate_fixed = float(np.sum(fixed_pred))
    difference = abs(candidate_exact - candidate_fixed)

    rows = max(1, x.shape[0])
    qx_bound = float(case["quant_scale"]) / 2.0
    error_bound = 0.0
    for j in range(label_dim):
        per_row = (
            np.abs(weights[:, j]) * qx_bound
            + np.abs(xq) * weight_round_bound
            + qx_bound * weight_round_bound
        )
        error_bound += float(gain) * float(np.sum(per_row)) / rows
        error_bound += float(gain) * feature_dim / (2.0 * fixed_scale * rows)
    error_bound = max(error_bound, difference)
    error_units = int(math.ceil(error_bound * fixed_scale))

    decision_id = "dec_" + sha256_bytes(
        b"decision:" + chain_digest.encode("ascii")
    ).removeprefix("sha256:")[:24]
    precision_sensitive = bool(difference < tolerance)
    if precision_sensitive:
        verdict = "precision_sensitive_reuse_with_error_chain"
    else:
        verdict = "candidate_divergence_requires_recompute"

    source_chain = [
        {"source": "case_digest", "digest": case["case_digest"]},
        {"source": "feature_tensor_digest", "digest": feature_desc["digest"]},
        {"source": "label_tensor_digest", "digest": label_desc["digest"]},
        {"source": "config_digest", "digest": config["merged_digest"]},
        {
            "source": "quantization_rounding",
            "bound_real": qx_bound,
            "quant_scale": float(case["quant_scale"]),
        },
        {
            "source": "fixed_weight_rounding",
            "bound_real": weight_round_bound,
            "fixed_point_scale": fixed_scale,
        },
        *[{"source": "patch", **item} for item in patch_trace],
    ]
    quant_record.update(
        {
            "fixed_point_scale": fixed_scale,
            "weight_max_abs_round_error": float(np.max(np.abs(weights - weights_q))),
            "activation_gain": gain,
            "tolerance": tolerance,
        }
    )
    decision_row = {
        "decision_id": decision_id,
        "task_uid": task["task_uid"],
        "case_id": case_id,
        "block_index": block_index,
        "config_version": int(config["config_version"]),
        "policy_generation": int(config["merged"]["policy_generation"]),
        "input_chain_digest": chain_digest,
        "input_chain_json": json.dumps(chain, sort_keys=True, separators=(",", ":")),
        "quantization_json": json.dumps(quant_record, sort_keys=True, separators=(",", ":")),
        "candidate_exact": candidate_exact,
        "candidate_fixed": candidate_fixed,
        "difference": difference,
        "tolerance": tolerance,
        "precision_sensitive": 1 if precision_sensitive else 0,
        "verdict": verdict,
        "error_source_chain_json": json.dumps(source_chain, sort_keys=True, separators=(",", ":")),
        "result_digest": "",
        "clock_ms": int(task.get("leased_clock_ms") or 0),
    }
    error_row = {
        "decision_id": decision_id,
        "case_id": case_id,
        "block_index": block_index,
        "config_version": int(config["config_version"]),
        "fixed_point_scale": fixed_scale,
        "local_error_bound": error_bound,
        "local_error_units": error_units,
        "source_chain_json": decision_row["error_source_chain_json"],
        "clock_ms": decision_row["clock_ms"],
    }
    artifact = {
        "schema": "dp.block_cache/v1",
        "task_uid": task["task_uid"],
        "logical_uid": task["logical_uid"],
        "case_id": case_id,
        "block_index": block_index,
        "config_version": int(config["config_version"]),
        "revision": int(task["revision"]),
        "decision_id": decision_id,
        "input_chain": chain,
        "quantization": quant_record,
        "result": {
            "prediction_exact": exact_pred.tolist(),
            "prediction_fixed": fixed_pred.tolist(),
            "candidate_exact": candidate_exact,
            "candidate_fixed": candidate_fixed,
            "difference": difference,
            "tolerance": tolerance,
            "precision_sensitive": precision_sensitive,
            "verdict": verdict,
            "error_bound": error_bound,
            "error_units": error_units,
            "label_mean_abs": float(np.mean(np.abs(y))),
        },
        "decision_record": {
            key: value
            for key, value in decision_row.items()
            if key not in {"clock_ms", "result_digest"}
        },
        "error_record": {
            key: value for key, value in error_row.items() if key != "clock_ms"
        },
        "error_source_chain": source_chain,
    }
    data = canonical_json(artifact) + b"\n"
    content_digest = sha256_bytes(data)
    decision_row["result_digest"] = content_digest
    return data, content_digest, decision_row, error_row, artifact


def block_lock_path(data_dir: Path, task_uid: str) -> Path:
    token = sha256_bytes(task_uid.encode("utf-8")).removeprefix("sha256:")[:32]
    return Path(data_dir) / "locks" / f"block-{token}.lock"


def execute_task(
    task_uid: str,
    db_path: Path | str,
    data_dir: Path | str,
    clock_ms: int,
    owner: Optional[str] = None,
) -> Dict[str, Any]:
    data_dir = Path(data_dir)
    db_path = Path(db_path)
    lock_path = block_lock_path(data_dir, task_uid)
    with file_lock(lock_path):
        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM block_task WHERE task_uid=?", (task_uid,)
            ).fetchone()
            if row is None:
                raise BizError(ErrorCode.BLOCK_NOT_FOUND, "task not found", {"task_uid": task_uid})
            task = row_to_dict(row)
            if task["state"] == "committed":
                return _task_summary(task, reused=True)

            if task["state"] == "materialized" and task.get("content_digest"):
                finalized = _finalize_materialized(conn, task, data_dir, clock_ms)
                if finalized:
                    return finalized

            import os
            import secrets

            with immediate(conn):
                conn.execute(
                    "UPDATE block_task SET state='running', attempt=attempt+1, "
                    "lease_owner=?, lease_pid=?, lease_nonce=?, leased_clock_ms=?, "
                    "started_clock_ms=?, failure_code=NULL, failure_message=NULL, "
                    "updated_clock_ms=? WHERE task_uid=?",
                    (
                        owner or f"pid-{os.getpid()}",
                        os.getpid(),
                        secrets.token_hex(8),
                        clock_ms,
                        clock_ms,
                        clock_ms,
                        task_uid,
                    ),
                )
            task = row_to_dict(
                conn.execute("SELECT * FROM block_task WHERE task_uid=?", (task_uid,)).fetchone()
            )
            fault_target = f"{task['case_id']}:{task['block_index']}"
            maybe_crash("lease_committed", fault_target)
            inject_operator_failure(fault_target)

            data, digest, decision_row, error_row, _artifact = _build_materialization(conn, task)
            object_path = digest_object_path(data_dir / "objects", digest)
            relative_path = object_path.relative_to(data_dir)
            if object_path.exists() and sha256_file(object_path) == digest:
                pass
            else:
                from .hashing import atomic_write_bytes

                atomic_write_bytes(object_path, data)
            maybe_crash("object_written", fault_target)

            with immediate(conn):
                conn.execute(
                    "UPDATE block_task SET state='materialized', content_digest=?, "
                    "object_path=?, decision_id=?, finished_clock_ms=?, updated_clock_ms=? "
                    "WHERE task_uid=?",
                    (
                        digest,
                        str(relative_path),
                        decision_row["decision_id"],
                        clock_ms + int(task["cost_ms"]),
                        clock_ms,
                        task_uid,
                    ),
                )
            maybe_crash("materialized_committed", fault_target)
            task = row_to_dict(
                conn.execute("SELECT * FROM block_task WHERE task_uid=?", (task_uid,)).fetchone()
            )
            result = _finalize_records(conn, task, decision_row, error_row, clock_ms)
            maybe_crash("final_committed", fault_target)
            return result
        except Exception as exc:  # noqa: BLE001 - boundary records failure for isolation
            code = getattr(exc, "code", ErrorCode.COMMAND_FAILED)
            message = getattr(exc, "message", str(exc))
            details = getattr(exc, "details", {})
            _mark_failed(db_path, task_uid, code, message, details, clock_ms)
            return {
                "task_uid": task_uid,
                "state": "failed",
                "error_code": code,
                "error": message,
                "details": details,
            }
        finally:
            conn.close()


def _task_summary(task: Dict[str, Any], reused: bool = False) -> Dict[str, Any]:
    return {
        "task_uid": task["task_uid"],
        "case_id": task["case_id"],
        "block_index": task["block_index"],
        "state": task["state"],
        "decision_id": task.get("decision_id"),
        "content_digest": task.get("content_digest"),
        "reused": reused,
    }


def _mark_failed(
    db_path: Path,
    task_uid: str,
    code: str,
    message: str,
    details: Dict[str, Any],
    clock_ms: int,
) -> None:
    conn = connect(db_path)
    try:
        with immediate(conn):
            conn.execute(
                "UPDATE block_task SET state='failed', failure_code=?, failure_message=?, "
                "finished_clock_ms=?, updated_clock_ms=? WHERE task_uid=? AND state!='committed'",
                (code, message[:500], clock_ms, clock_ms, task_uid),
            )
    finally:
        conn.close()


def _finalize_records(
    conn: sqlite3.Connection,
    task: Dict[str, Any],
    decision_row: Dict[str, Any],
    error_row: Dict[str, Any],
    clock_ms: int,
) -> Dict[str, Any]:
    decision_row["result_digest"] = task["content_digest"]
    with immediate(conn):
        existing = conn.execute(
            "SELECT decision_id FROM inference_decision WHERE decision_id=?",
            (decision_row["decision_id"],),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO inference_decision("
                "decision_id,task_uid,case_id,block_index,config_version,"
                "policy_generation,input_chain_digest,input_chain_json,"
                "quantization_json,candidate_exact,candidate_fixed,difference,"
                "tolerance,precision_sensitive,verdict,error_source_chain_json,"
                "result_digest,clock_ms) VALUES ("
                ":decision_id,:task_uid,:case_id,:block_index,:config_version,"
                ":policy_generation,:input_chain_digest,:input_chain_json,"
                ":quantization_json,:candidate_exact,:candidate_fixed,:difference,"
                ":tolerance,:precision_sensitive,:verdict,:error_source_chain_json,"
                ":result_digest,:clock_ms)",
                decision_row,
            )
        conn.execute(
            "INSERT OR IGNORE INTO error_ledger("
            "decision_id,case_id,block_index,config_version,is_current,"
            "fixed_point_scale,local_error_bound,local_error_units,"
            "cumulative_error_bound,cumulative_error_units,source_chain_json,clock_ms) "
            "VALUES (?,?,?,?,1,?,?,?,0,0,?,?)",
            (
                error_row["decision_id"],
                error_row["case_id"],
                error_row["block_index"],
                error_row["config_version"],
                error_row["fixed_point_scale"],
                error_row["local_error_bound"],
                error_row["local_error_units"],
                error_row["source_chain_json"],
                clock_ms,
            ),
        )
        conn.execute(
            "UPDATE error_ledger SET is_current=0 WHERE decision_id IN ("
            "SELECT d.decision_id FROM inference_decision d JOIN block_task t ON t.task_uid=d.task_uid "
            "WHERE t.logical_uid=? AND d.decision_id != ?)",
            (task["logical_uid"], decision_row["decision_id"]),
        )
        conn.execute("UPDATE error_ledger SET is_current=1 WHERE decision_id=?", (decision_row["decision_id"],))
        _rebuild_cumulative_errors(conn, int(task["config_version"]))
        conn.execute(
            "UPDATE block_task SET state='committed', content_digest=?, object_path=?, "
            "decision_id=?, finished_clock_ms=?, updated_clock_ms=?, failure_code=NULL, "
            "failure_message=NULL WHERE task_uid=?",
            (
                task["content_digest"],
                task.get("object_path"),
                decision_row["decision_id"],
                clock_ms + int(task["cost_ms"]),
                clock_ms,
                task["task_uid"],
            ),
        )
    return {
        "task_uid": task["task_uid"],
        "case_id": task["case_id"],
        "block_index": task["block_index"],
        "state": "committed",
        "decision_id": decision_row["decision_id"],
        "content_digest": decision_row["result_digest"],
        "reused": False,
    }


def _rebuild_cumulative_errors(conn: sqlite3.Connection, config_version: int) -> None:
    rows = conn.execute(
        "SELECT e.decision_id, e.local_error_bound, e.local_error_units "
        "FROM error_ledger e JOIN inference_decision d ON d.decision_id=e.decision_id "
        "WHERE e.config_version=? AND e.is_current=1 "
        "ORDER BY d.case_id ASC, d.block_index ASC, d.decision_id ASC",
        (config_version,),
    ).fetchall()
    cumulative_bound = 0.0
    cumulative_units = 0
    for row in rows:
        cumulative_bound += float(row["local_error_bound"])
        cumulative_units += int(row["local_error_units"])
        conn.execute(
            "UPDATE error_ledger SET cumulative_error_bound=?, cumulative_error_units=? "
            "WHERE decision_id=?",
            (cumulative_bound, cumulative_units, row["decision_id"]),
        )


def _finalize_materialized(
    conn: sqlite3.Connection,
    task: Dict[str, Any],
    data_dir: Path,
    clock_ms: int,
) -> Optional[Dict[str, Any]]:
    relative = task.get("object_path")
    if not relative:
        return None
    object_path = data_dir / relative
    if not object_path.exists() or sha256_file(object_path) != task["content_digest"]:
        with immediate(conn):
            conn.execute(
                "UPDATE block_task SET state='pending', content_digest=NULL, "
                "object_path=NULL, updated_clock_ms=? WHERE task_uid=?",
                (clock_ms, task["task_uid"]),
            )
        return None
    artifact = json.loads(object_path.read_text(encoding="utf-8"))
    decision_row = artifact["decision_record"]
    error_row = artifact["error_record"]
    decision_row["clock_ms"] = clock_ms
    error_row["clock_ms"] = clock_ms
    return _finalize_records(conn, task, decision_row, error_row, clock_ms)


def adjudicate_batch(
    conn: sqlite3.Connection,
    clock_ms: int,
    config_version: Optional[int] = None,
    tolerance: Optional[float] = None,
) -> Dict[str, Any]:
    if config_version is None:
        config = active_config_row(conn)
        config_version = int(config["config_version"])
        if tolerance is None:
            tolerance = float(config["merged"]["policies"].get("inference.tolerance", 0.12))
    if tolerance is None:
        tolerance = 0.12
    rows = conn.execute(
        "SELECT d.*, e.local_error_bound, e.local_error_units, e.cumulative_error_bound, "
        "e.cumulative_error_units, e.source_chain_json FROM inference_decision d "
        "JOIN error_ledger e ON e.decision_id=d.decision_id "
        "WHERE d.config_version=? AND e.is_current=1 "
        "ORDER BY d.case_id, d.block_index",
        (config_version,),
    ).fetchall()
    decision_ids = [row["decision_id"] for row in rows]
    total_bound = sum(float(row["local_error_bound"]) for row in rows)
    total_units = sum(int(row["local_error_units"]) for row in rows)
    sensitive_count = sum(int(row["precision_sensitive"]) for row in rows)
    items = []
    for row in rows:
        items.append(
            {
                "decision_id": row["decision_id"],
                "case_id": row["case_id"],
                "block_index": row["block_index"],
                "difference": row["difference"],
                "tolerance": row["tolerance"],
                "precision_sensitive": bool(row["precision_sensitive"]),
                "verdict": row["verdict"],
                "error_bound": row["local_error_bound"],
                "source_chain": json.loads(row["error_source_chain_json"]),
            }
        )
    batch_id = f"batch-cfg-{config_version}"
    report = {
        "batch_id": batch_id,
        "config_version": config_version,
        "decision_count": len(decision_ids),
        "decision_ids": decision_ids,
        "total_error_bound": total_bound,
        "total_error_units": total_units,
        "precision_sensitive_count": sensitive_count,
        "tolerance": tolerance,
        "items": items,
        "clock_ms": clock_ms,
    }
    with immediate(conn):
        conn.execute(
            "INSERT INTO batch_adjudication("
            "batch_id,clock_ms,tolerance,decision_ids_json,total_error_bound,"
            "total_error_units,precision_sensitive_count,report_json) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(batch_id) DO UPDATE SET clock_ms=excluded.clock_ms,"
            "tolerance=excluded.tolerance,decision_ids_json=excluded.decision_ids_json,"
            "total_error_bound=excluded.total_error_bound,"
            "total_error_units=excluded.total_error_units,"
            "precision_sensitive_count=excluded.precision_sensitive_count,"
            "report_json=excluded.report_json",
            (
                batch_id,
                clock_ms,
                tolerance,
                json.dumps(decision_ids),
                total_bound,
                total_units,
                sensitive_count,
                json.dumps(report, sort_keys=True, separators=(",", ":")),
            ),
        )
    return report
