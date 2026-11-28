"""SQLite persistence with one atomic transaction per completed request."""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from .decoder import aggregate_request_stats, aggregate_results
from .loader import LoadedDataset, Policy


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_key TEXT PRIMARY KEY,
    dataset_fingerprint TEXT NOT NULL,
    seed INTEGER NOT NULL,
    tick_ms INTEGER NOT NULL,
    source_schema INTEGER,
    policy_id TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    total_requests INTEGER NOT NULL,
    completed_requests INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('running', 'complete')),
    aggregate_json TEXT
);

CREATE TABLE IF NOT EXISTS requests (
    run_key TEXT NOT NULL,
    request_seq INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status = 'committed'),
    result_json TEXT NOT NULL,
    stats_json TEXT NOT NULL,
    PRIMARY KEY (run_key, request_seq),
    UNIQUE (run_key, request_id),
    FOREIGN KEY (run_key) REFERENCES runs(run_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rounds (
    run_key TEXT NOT NULL,
    request_seq INTEGER NOT NULL,
    round_index INTEGER NOT NULL,
    draft_len INTEGER NOT NULL,
    accepted_count INTEGER NOT NULL,
    rejected_position INTEGER,
    draft_ticks INTEGER NOT NULL,
    verification_ticks INTEGER NOT NULL,
    resample_ticks INTEGER NOT NULL,
    total_ticks INTEGER NOT NULL,
    cumulative_ticks INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (run_key, request_seq, round_index),
    FOREIGN KEY (run_key, request_seq)
        REFERENCES requests(run_key, request_seq) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS decisions (
    run_key TEXT NOT NULL,
    request_seq INTEGER NOT NULL,
    round_index INTEGER NOT NULL,
    position INTEGER NOT NULL,
    draft_token_id INTEGER NOT NULL,
    target_token_id INTEGER NOT NULL,
    draft_prob_bps INTEGER NOT NULL,
    target_prob_bps INTEGER NOT NULL,
    ratio_bps INTEGER NOT NULL,
    threshold_bps INTEGER NOT NULL,
    distributions_equal INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    applied INTEGER NOT NULL,
    outcome TEXT,
    record_json TEXT NOT NULL,
    PRIMARY KEY (run_key, request_seq, round_index, position),
    FOREIGN KEY (run_key, request_seq, round_index)
        REFERENCES rounds(run_key, request_seq, round_index) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS fallbacks (
    run_key TEXT NOT NULL,
    request_seq INTEGER NOT NULL,
    round_index INTEGER NOT NULL,
    position INTEGER NOT NULL,
    rejected_draft_token_id INTEGER NOT NULL,
    resampled_internal_id INTEGER NOT NULL,
    resampled_target_token_id INTEGER NOT NULL,
    residual_mass_bps INTEGER NOT NULL,
    draw_u64 TEXT NOT NULL,
    method TEXT NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (run_key, request_seq, round_index),
    FOREIGN KEY (run_key, request_seq, round_index)
        REFERENCES rounds(run_key, request_seq, round_index) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS budget_breaches (
    run_key TEXT NOT NULL,
    request_seq INTEGER NOT NULL,
    round_index INTEGER NOT NULL,
    cumulative_before INTEGER NOT NULL,
    round_total_ticks INTEGER NOT NULL,
    cumulative_after INTEGER NOT NULL,
    basis_json TEXT NOT NULL,
    PRIMARY KEY (run_key, request_seq),
    FOREIGN KEY (run_key, request_seq)
        REFERENCES requests(run_key, request_seq) ON DELETE CASCADE
);
"""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def open_database(path: str) -> sqlite3.Connection:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
        ("storage_schema", "1"),
    )
    connection.commit()
    return connection


def ensure_run(
    connection: sqlite3.Connection,
    *,
    run_key: str,
    dataset: LoadedDataset,
    policy: Policy,
    source_schema: int | None,
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM runs WHERE run_key = ?", (run_key,)
    ).fetchone()
    policy_json = _json(policy.as_dict())
    if row is None:
        connection.execute(
            """
            INSERT INTO runs(
                run_key, dataset_fingerprint, seed, tick_ms, source_schema,
                policy_id, policy_json, total_requests, completed_requests,
                status, aggregate_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'running', NULL)
            """,
            (
                run_key,
                dataset.fingerprint,
                dataset.seed,
                dataset.tick_ms,
                source_schema,
                policy.policy_id,
                policy_json,
                len(dataset.requests),
            ),
        )
        connection.commit()
    else:
        if row["dataset_fingerprint"] != dataset.fingerprint:
            raise ValueError("run key collides with a different dataset fingerprint")
        if row["policy_json"] != policy_json:
            raise ValueError("run key collides with a different policy declaration")
        if int(row["total_requests"]) != len(dataset.requests):
            raise ValueError("stored run request count does not match dataset")
    return dict(
        connection.execute(
            "SELECT * FROM runs WHERE run_key = ?", (run_key,)
        ).fetchone()
    )


def _insert_round_records(
    connection: sqlite3.Connection,
    run_key: str,
    request_seq: int,
    result: Mapping[str, Any],
) -> None:
    for round_record in result["rounds"]:
        round_index = int(round_record["round_index"])
        connection.execute(
            """
            INSERT INTO rounds(
                run_key, request_seq, round_index, draft_len, accepted_count,
                rejected_position, draft_ticks, verification_ticks,
                resample_ticks, total_ticks, cumulative_ticks, record_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_key,
                request_seq,
                round_index,
                int(round_record["draft_len"]),
                int(round_record["accepted_count"]),
                round_record["rejected_position"],
                int(round_record["draft_ticks"]),
                int(round_record["verification_ticks"]),
                int(round_record["resample_ticks"]),
                int(round_record["total_ticks"]),
                int(round_record["cumulative_ticks"]),
                _json(round_record),
            ),
        )
        for decision in round_record["decisions"]:
            connection.execute(
                """
                INSERT INTO decisions(
                    run_key, request_seq, round_index, position,
                    draft_token_id, target_token_id, draft_prob_bps,
                    target_prob_bps, ratio_bps, threshold_bps,
                    distributions_equal, verdict, applied, outcome,
                    record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_key,
                    request_seq,
                    round_index,
                    int(decision["position"]),
                    int(decision["draft_token_id"]),
                    int(decision["target_token_id"]),
                    int(decision["draft_prob_bps"]),
                    int(decision["target_prob_bps"]),
                    int(decision["ratio_bps"]),
                    int(decision["threshold_bps"]),
                    1 if decision["distributions_equal"] else 0,
                    str(decision["verdict"]),
                    1 if decision["applied"] else 0,
                    decision["outcome"],
                    _json(decision),
                ),
            )
        fallback = round_record.get("fallback")
        if fallback is not None:
            connection.execute(
                """
                INSERT INTO fallbacks(
                    run_key, request_seq, round_index, position,
                    rejected_draft_token_id, resampled_internal_id,
                    resampled_target_token_id, residual_mass_bps,
                    draw_u64, method, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_key,
                    request_seq,
                    round_index,
                    int(fallback["position"]),
                    int(fallback["rejected_draft_token_id"]),
                    int(fallback["resampled_internal_id"]),
                    int(fallback["resampled_target_token_id"]),
                    int(fallback["residual_mass_bps"]),
                    str(fallback["draw_u64"]),
                    str(fallback["method"]),
                    _json(fallback),
                ),
            )


def commit_request(
    connection: sqlite3.Connection,
    *,
    run_key: str,
    request_seq: int,
    result: Mapping[str, Any],
    total_requests: int,
) -> None:
    """Atomically persist a fully decoded request and all of its children."""

    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO requests(
                run_key, request_seq, request_id, status,
                result_json, stats_json
            ) VALUES (?, ?, ?, 'committed', ?, ?)
            """,
            (
                run_key,
                request_seq,
                str(result["request_id"]),
                _json(result),
                _json(result["stats"]),
            ),
        )
        _insert_round_records(connection, run_key, request_seq, result)
        basis = result["stats"].get("budget_breach_basis")
        if basis is not None:
            connection.execute(
                """
                INSERT INTO budget_breaches(
                    run_key, request_seq, round_index, cumulative_before,
                    round_total_ticks, cumulative_after, basis_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_key,
                    request_seq,
                    int(basis["round_index"]),
                    int(basis["cumulative_before_round"]),
                    int(basis["round_total_ticks"]),
                    int(basis["cumulative_after_round"]),
                    _json(basis),
                ),
            )

        completed = request_seq + 1
        status = "complete" if completed == total_requests else "running"
        connection.execute(
            """
            UPDATE runs
               SET completed_requests = ?, status = ?
             WHERE run_key = ?
            """,
            (completed, status, run_key),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def stored_request_results(
    connection: sqlite3.Connection,
    run_key: str,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT result_json
          FROM requests
         WHERE run_key = ?
         ORDER BY request_seq
        """,
        (run_key,),
    ).fetchall()
    return [json.loads(row["result_json"]) for row in rows]


def finalize_run(
    connection: sqlite3.Connection,
    *,
    run_key: str,
    results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    aggregate = aggregate_results(results)
    connection.execute(
        """
        UPDATE runs
           SET status = 'complete', completed_requests = ?,
               aggregate_json = ?
         WHERE run_key = ?
        """,
        (len(results), _json(aggregate), run_key),
    )
    connection.commit()
    return aggregate


def stored_aggregate(
    connection: sqlite3.Connection,
    run_key: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT aggregate_json FROM runs WHERE run_key = ?", (run_key,)
    ).fetchone()
    if row is None or row["aggregate_json"] is None:
        return None
    return json.loads(row["aggregate_json"])


def run_metadata(connection: sqlite3.Connection, run_key: str) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT run_key, dataset_fingerprint, seed, tick_ms, source_schema,
               policy_id, policy_json, total_requests, completed_requests,
               status, aggregate_json
          FROM runs
         WHERE run_key = ?
        """,
        (run_key,),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown run key: {run_key}")
    result = dict(row)
    result["policy"] = json.loads(result.pop("policy_json"))
    aggregate_json = result.pop("aggregate_json")
    result["aggregate"] = (
        json.loads(aggregate_json) if aggregate_json is not None else None
    )
    return result


def stored_run_policies(
    connection: sqlite3.Connection,
    *,
    dataset_fingerprint: str | None = None,
) -> list[Policy]:
    """Return one policy declaration per persisted run, in stable order."""

    if dataset_fingerprint is None:
        rows = connection.execute(
            """
            SELECT policy_json
              FROM runs
             ORDER BY policy_id, run_key
            """
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT policy_json
              FROM runs
             WHERE dataset_fingerprint = ?
             ORDER BY policy_id, run_key
            """,
            (dataset_fingerprint,),
        ).fetchall()
    policies: list[Policy] = []
    seen: set[str] = set()
    for row in rows:
        policy = Policy.from_dict(json.loads(row["policy_json"]))
        if policy.policy_id in seen:
            continue
        seen.add(policy.policy_id)
        policies.append(policy)
    return policies


def _first_rejection(round_record: Mapping[str, Any]) -> int | None:
    for decision in round_record["decisions"]:
        if decision["verdict"] == "rejected":
            return int(decision["position"])
    return None


def audit_stored_run(
    connection: sqlite3.Connection,
    run_key: str,
    dataset: LoadedDataset,
) -> dict[str, Any]:
    """Independently re-aggregate persisted rounds and verify DB rows."""

    errors: list[str] = []
    metadata = run_metadata(connection, run_key)
    policy = Policy.from_dict(metadata["policy"])
    target_ticks = dataset.models.target.forward_ticks_per_token
    rows = connection.execute(
        """
        SELECT request_seq, request_id, result_json, stats_json
          FROM requests
         WHERE run_key = ?
         ORDER BY request_seq
        """,
        (run_key,),
    ).fetchall()

    if len(rows) != int(metadata["completed_requests"]):
        errors.append(
            "requests table count does not match runs.completed_requests"
        )

    reconstructed_results: list[dict[str, Any]] = []
    for expected_seq, row in enumerate(rows):
        if int(row["request_seq"]) != expected_seq:
            errors.append(
                f"non-contiguous committed prefix at sequence {expected_seq}"
            )
        try:
            result = json.loads(row["result_json"])
        except json.JSONDecodeError:
            errors.append(
                f"{row['request_id']}: request result is not valid JSON"
            )
            continue
        reconstructed_results.append(result)
        stats_from_json = json.loads(row["stats_json"])
        if stats_from_json != result["stats"]:
            errors.append(f"{row['request_id']}: stats_json differs from result")

        recomputed_stats = aggregate_request_stats(
            rounds=result["rounds"],
            fallbacks=result["fallbacks"],
            final_token_count=int(result["final_token_count"]),
            target_forward_ticks_per_token=target_ticks,
            latency_budget_ticks=policy.latency_budget_ticks,
        )
        if recomputed_stats != result["stats"]:
            errors.append(
                f"{row['request_id']}: request stats differ from round aggregation"
            )

        round_rows = connection.execute(
            """
            SELECT * FROM rounds
             WHERE run_key = ? AND request_seq = ?
             ORDER BY round_index
            """,
            (run_key, row["request_seq"]),
        ).fetchall()
        if len(round_rows) != len(result["rounds"]):
            errors.append(f"{row['request_id']}: persisted round count mismatch")

        reconstructed_tokens: list[int] = []
        persisted_fallback_count = 0
        for round_row in round_rows:
            round_index = int(round_row["round_index"])
            round_record = json.loads(round_row["record_json"])
            if round_record != result["rounds"][round_index]:
                errors.append(
                    f"{row['request_id']} round {round_index}: row JSON mismatch"
                )

            decision_rows = connection.execute(
                """
                SELECT * FROM decisions
                 WHERE run_key = ? AND request_seq = ? AND round_index = ?
                 ORDER BY position
                """,
                (run_key, row["request_seq"], round_index),
            ).fetchall()
            if len(decision_rows) != len(round_record["decisions"]):
                errors.append(
                    f"{row['request_id']} round {round_index}: decision count mismatch"
                )
            for position, decision_row in enumerate(decision_rows):
                decision = round_record["decisions"][position]
                equal = bool(decision["distributions_equal"])
                ratio = min(
                    10000,
                    int(decision["target_prob_bps"])
                    * 10000
                    // int(decision["draft_prob_bps"]),
                )
                expected_verdict = (
                    "accepted"
                    if equal or ratio >= int(decision["threshold_bps"])
                    else "rejected"
                )
                if equal != bool(decision_row["distributions_equal"]):
                    errors.append(
                        f"{row['request_id']} round {round_index} position {position}: "
                        "equality column mismatch"
                    )
                if int(decision_row["ratio_bps"]) != ratio:
                    errors.append(
                        f"{row['request_id']} round {round_index} position {position}: "
                        "ratio column mismatch"
                    )
                if decision_row["verdict"] != expected_verdict:
                    errors.append(
                        f"{row['request_id']} round {round_index} position {position}: "
                        "verdict does not satisfy acceptance rule"
                    )
                if decision["verdict"] != expected_verdict:
                    errors.append(
                        f"{row['request_id']} round {round_index} position {position}: "
                        "recorded verdict does not satisfy acceptance rule"
                    )
                if decision.get("applied"):
                    reconstructed_tokens.append(int(decision["draft_token_id"]))

            first_rejection = _first_rejection(round_record)
            if first_rejection != round_record["rejected_position"]:
                errors.append(
                    f"{row['request_id']} round {round_index}: first rejection mismatch"
                )
            if first_rejection is not None:
                for decision in round_record["decisions"]:
                    position = int(decision["position"])
                    if position < first_rejection and not decision["applied"]:
                        errors.append(
                            f"{row['request_id']} round {round_index}: "
                            "accepted prefix decision was not applied"
                        )
                    if position >= first_rejection and decision["applied"]:
                        errors.append(
                            f"{row['request_id']} round {round_index}: "
                            "decision after first rejection was applied"
                        )

            fallback_rows = connection.execute(
                """
                SELECT * FROM fallbacks
                 WHERE run_key = ? AND request_seq = ? AND round_index = ?
                """,
                (run_key, row["request_seq"], round_index),
            ).fetchall()
            if len(fallback_rows) != (1 if first_rejection is not None else 0):
                errors.append(
                    f"{row['request_id']} round {round_index}: fallback count mismatch"
                )
            for fallback_row in fallback_rows:
                persisted_fallback_count += 1
                fallback_record = json.loads(fallback_row["record_json"])
                if fallback_record != round_record["fallback"]:
                    errors.append(
                        f"{row['request_id']} round {round_index}: fallback JSON mismatch"
                    )
                reconstructed_tokens.append(
                    int(fallback_record["resampled_internal_id"])
                )

        if persisted_fallback_count != len(result["fallbacks"]):
            errors.append(
                f"{row['request_id']}: fallback table count differs from result"
            )
        if reconstructed_tokens != result["final_internal_ids"]:
            errors.append(
                f"{row['request_id']}: round replay does not reconstruct final tokens"
            )

        breach_rows = connection.execute(
            """
            SELECT basis_json FROM budget_breaches
             WHERE run_key = ? AND request_seq = ?
            """,
            (run_key, row["request_seq"]),
        ).fetchall()
        expected_breach = result["stats"]["budget_breach_basis"] is not None
        if len(breach_rows) != (1 if expected_breach else 0):
            errors.append(f"{row['request_id']}: budget breach row mismatch")
        elif expected_breach:
            if json.loads(breach_rows[0]["basis_json"]) != result["stats"][
                "budget_breach_basis"
            ]:
                errors.append(
                    f"{row['request_id']}: budget breach basis mismatch"
                )

    recomputed_aggregate = aggregate_results(reconstructed_results)
    stored = stored_aggregate(connection, run_key)
    if stored is not None and stored != recomputed_aggregate:
        errors.append("stored aggregate differs from per-request recomputation")

    return {
        "ok": not errors,
        "run_key": run_key,
        "committed_requests": len(rows),
        "status": metadata["status"],
        "errors": errors,
        "recomputed_aggregate": recomputed_aggregate,
    }


__all__ = [
    "SCHEMA_SQL",
    "audit_stored_run",
    "commit_request",
    "ensure_run",
    "finalize_run",
    "open_database",
    "run_metadata",
    "stored_aggregate",
    "stored_run_policies",
    "stored_request_results",
]
