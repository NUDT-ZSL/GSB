from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from .db import immediate
from .engine import ensure_block_task, get_case
from .hashing import canonical_digest
from .tensors import block_count


def _patch_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    patch = {key: row[key] for key in row.keys()}
    patch["block_range"] = [patch["block_start"], patch["block_end"]]
    patch["patch_content"] = json.loads(patch.pop("content_json") or "{}")
    patch["refs"] = json.loads(patch.pop("refs_json") or "[]")
    return patch


def adjudicate_case(
    case: Dict[str, Any], patches: List[Dict[str, Any]]
) -> Dict[str, Any]:
    count = block_count(int(case["data_size"]), case["block_plan"])
    groups: Dict[Tuple[int, str], List[Dict[str, Any]]] = defaultdict(list)
    for patch in patches:
        for block_index in range(int(patch["block_start"]), int(patch["block_end"])):
            groups[(block_index, patch["patch_kind"])].append(patch)

    winners: Dict[int, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    winner_ids: Set[str] = set()
    winner_report: Dict[str, Any] = {}
    for (block_index, kind), candidates in groups.items():
        ordered = sorted(
            candidates,
            key=lambda p: (
                int(p["patch_generation"]),
                int(p["logical_clock_ms"]),
                p["patch_id"],
            ),
        )
        winner = ordered[-1]
        winners[block_index][kind] = winner
        winner_ids.add(winner["patch_id"])
        key = f"b{block_index:04d}:{kind}"
        winner_report[key] = {
            "winner_patch_id": winner["patch_id"],
            "patch_generation": winner["patch_generation"],
            "logical_clock_ms": winner["logical_clock_ms"],
            "candidate_patch_ids": [p["patch_id"] for p in ordered],
            "conflict": len(ordered) > 1,
        }

    adjacency: Dict[int, List[int]] = {i: [] for i in range(count)}
    if case["block_plan"].get("carry_dependency", False):
        for i in range(count - 1):
            adjacency[i].append(i + 1)

    influenced: Dict[int, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    roots = sorted(winners.keys())
    for block_index, kind_map in winners.items():
        for winner in kind_map.values():
            seen: Set[int] = set()
            stack = [block_index]
            while stack:
                node = stack.pop()
                if node in seen:
                    continue
                seen.add(node)
                influenced[node][winner["patch_id"]] = winner
                stack.extend(adjacency.get(node, []))

    affected = sorted(influenced.keys())
    effective_by_block = {
        block_index: list(patch_map.values())
        for block_index, patch_map in influenced.items()
    }
    return {
        "case_id": case["case_id"],
        "block_count": count,
        "roots": roots,
        "affected_blocks": affected,
        "winners": winner_report,
        "winner_patch_ids": sorted(winner_ids),
        "effective_by_block": effective_by_block,
    }


def compute_closures(
    conn: sqlite3.Connection,
    clock_ms: int,
    case_id: Optional[str] = None,
) -> Dict[str, Any]:
    patch_rows = conn.execute(
        "SELECT * FROM patches ORDER BY logical_clock_ms ASC, patch_generation ASC, patch_id ASC"
    ).fetchall()
    patches_by_case: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in patch_rows:
        patches_by_case[row["case_id"]].append(_patch_row_to_dict(row))

    if case_id:
        cases = [get_case(conn, case_id)]
    else:
        cases = [get_case(conn, row["case_id"]) for row in conn.execute("SELECT case_id FROM cases ORDER BY case_id")]

    summaries = []
    planned_tasks = []
    with immediate(conn):
        from .engine import active_config_row

        config = active_config_row(conn)
        for case in cases:
            result = adjudicate_case(case, patches_by_case.get(case["case_id"], []))
            closure_digest = canonical_digest(
                {
                    "case_id": case["case_id"],
                    "roots": result["roots"],
                    "affected": result["affected_blocks"],
                    "winners": result["winners"],
                    "patch_ids": [p["patch_id"] for p in patches_by_case.get(case["case_id"], [])],
                }
            )
            closure_uid = f"closure:{case['case_id']}:{closure_digest.removeprefix('sha256:')[:16]}"
            conn.execute(
                "INSERT OR IGNORE INTO invalidation_closure("
                "closure_uid,case_id,closure_digest,roots_json,affected_json,"
                "winners_json,patch_ids_json,clock_ms,created_at_clock_ms) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    closure_uid,
                    case["case_id"],
                    closure_digest,
                    json.dumps(result["roots"]),
                    json.dumps(result["affected_blocks"]),
                    json.dumps(result["winners"], sort_keys=True),
                    json.dumps([p["patch_id"] for p in patches_by_case.get(case["case_id"], [])]),
                    clock_ms,
                    clock_ms,
                ),
            )
            for block_index in result["affected_blocks"]:
                planned = ensure_block_task(
                    conn,
                    case["case_id"],
                    block_index,
                    result["effective_by_block"][block_index],
                    clock_ms,
                    config,
                )
                planned_tasks.append(
                    {
                        "task_uid": planned["task"]["task_uid"],
                        "case_id": case["case_id"],
                        "block_index": block_index,
                        "created": planned["created"],
                        "state": planned["task"]["state"],
                    }
                )
            winner_ids = set(result["winner_patch_ids"])
            for patch in patches_by_case.get(case["case_id"], []):
                conn.execute(
                    "UPDATE patches SET applied=1, superseded=? WHERE patch_id=?",
                    (0 if patch["patch_id"] in winner_ids else 1, patch["patch_id"]),
                )
            summaries.append(
                {
                    "case_id": case["case_id"],
                    "roots": result["roots"],
                    "affected_blocks": result["affected_blocks"],
                    "winner_patch_ids": result["winner_patch_ids"],
                    "conflict_count": sum(
                        1 for item in result["winners"].values() if item["conflict"]
                    ),
                    "closure_uid": closure_uid,
                }
            )
    return {"closures": summaries, "planned_tasks": planned_tasks}


def assert_patch_generation_not_rollback(
    conn: sqlite3.Connection, patch: Dict[str, Any]
) -> None:
    row = conn.execute(
        "SELECT MAX(patch_generation) AS max_generation FROM patches "
        "WHERE case_id=? AND patch_kind=? AND block_start < ? AND block_end > ?",
        (
            patch["case_id"],
            patch["patch_kind"],
            int(patch["block_end"]),
            int(patch["block_start"]),
        ),
    ).fetchone()
    current_max = row["max_generation"] if row else None
    if current_max is not None and int(patch["patch_generation"]) < int(current_max):
        from .errors import BizError, ErrorCode

        raise BizError(
            ErrorCode.PATCH_GENERATION_ROLLBACK,
            "submitted patch generation is older than an overlapping committed patch generation",
            {
                "patch_id": patch.get("patch_id"),
                "case_id": patch["case_id"],
                "submitted_generation": patch["patch_generation"],
                "current_max_generation": current_max,
                "block_range": patch["block_range"],
            },
        )
