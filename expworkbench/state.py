from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Any, Iterable

from .canonical import canonical_json, parse_json_text
from .errors import E_STATE_CORRUPT, fail
from .validation import Experiment, MetricRecord


def experiment_from_row(row: sqlite3.Row) -> Experiment:
    try:
        hyperparameters = parse_json_text(row["hyperparameters_json"])
        baseline_raw = parse_json_text(row["baseline_metrics_json"])
        refs_raw = parse_json_text(row["metric_references_json"])
        aliases_raw = parse_json_text(row["aliases_json"])
        baseline_metrics = {
            str(key): value if isinstance(value, Decimal) else Decimal(str(value))
            for key, value in baseline_raw.items()
        }
        refs = {
            str(key): (str(value["experiment_id"]), str(value["metric"]))
            for key, value in refs_raw.items()
        }
        aliases = tuple(str(alias) for alias in aliases_raw)
        return Experiment(
            id=str(row["id"]),
            parent_id=None if row["parent_id"] is None else str(row["parent_id"]),
            hyperparameters=hyperparameters,
            dataset_version=str(row["dataset_version"]),
            code_commit=str(row["code_commit"]),
            baseline_metrics=baseline_metrics,
            logical_clock_ms=int(row["logical_clock_ms"]),
            metric_references=refs,
            aliases=aliases,
            fingerprint=str(row["fingerprint"]),
        )
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        fail(E_STATE_CORRUPT, "Stored experiment declaration is invalid", detail=str(exc))


def load_experiments(connection: sqlite3.Connection) -> dict[str, Experiment]:
    rows = connection.execute("SELECT * FROM experiments ORDER BY id").fetchall()
    return {row["id"]: experiment_from_row(row) for row in rows}


def insert_experiment(
    connection: sqlite3.Connection, experiment: Experiment, data_epoch: int
) -> None:
    connection.execute(
        """
        INSERT INTO experiments(
            id, parent_id, hyperparameters_json, dataset_version, code_commit,
            baseline_metrics_json, logical_clock_ms, metric_references_json,
            declaration_json, fingerprint, aliases_json, data_epoch
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            experiment.id,
            experiment.parent_id,
            canonical_json(experiment.hyperparameters),
            experiment.dataset_version,
            experiment.code_commit,
            canonical_json(experiment.baseline_metrics),
            experiment.logical_clock_ms,
            canonical_json(
                {
                    key: {"experiment_id": target[0], "metric": target[1]}
                    for key, target in sorted(experiment.metric_references.items())
                }
            ),
            canonical_json(experiment.declaration()),
            experiment.fingerprint,
            canonical_json(list(experiment.aliases)),
            data_epoch,
        ),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO experiment_fingerprint_history(
            experiment_id, fingerprint, declaration_json, first_data_epoch
        ) VALUES (?, ?, ?, ?)
        """,
        (
            experiment.id,
            experiment.fingerprint,
            canonical_json(experiment.declaration()),
            data_epoch,
        ),
    )


def update_experiment(
    connection: sqlite3.Connection, experiment: Experiment, data_epoch: int
) -> None:
    connection.execute(
        """
        UPDATE experiments SET
            parent_id = ?,
            hyperparameters_json = ?,
            dataset_version = ?,
            code_commit = ?,
            baseline_metrics_json = ?,
            logical_clock_ms = ?,
            metric_references_json = ?,
            declaration_json = ?,
            fingerprint = ?,
            aliases_json = ?,
            data_epoch = ?
        WHERE id = ?
        """,
        (
            experiment.parent_id,
            canonical_json(experiment.hyperparameters),
            experiment.dataset_version,
            experiment.code_commit,
            canonical_json(experiment.baseline_metrics),
            experiment.logical_clock_ms,
            canonical_json(
                {
                    key: {"experiment_id": target[0], "metric": target[1]}
                    for key, target in sorted(experiment.metric_references.items())
                }
            ),
            canonical_json(experiment.declaration()),
            experiment.fingerprint,
            canonical_json(list(experiment.aliases)),
            data_epoch,
            experiment.id,
        ),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO experiment_fingerprint_history(
            experiment_id, fingerprint, declaration_json, first_data_epoch
        ) VALUES (?, ?, ?, ?)
        """,
        (
            experiment.id,
            experiment.fingerprint,
            canonical_json(experiment.declaration()),
            data_epoch,
        ),
    )


def load_fingerprint_history(
    connection: sqlite3.Connection,
) -> dict[str, set[str]]:
    history: dict[str, set[str]] = {}
    for row in connection.execute(
        "SELECT experiment_id, fingerprint FROM experiment_fingerprint_history"
    ):
        history.setdefault(str(row["experiment_id"]), set()).add(
            str(row["fingerprint"])
        )
    return history


def insert_metric_record(connection: sqlite3.Connection, record: MetricRecord) -> None:
    connection.execute(
        """
        INSERT INTO metric_records(
            record_id, seq, experiment_id, metric, step, value_text,
            logical_clock_ms, record_digest, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.record_id,
            record.seq,
            record.experiment_id,
            record.metric,
            record.step,
            record.value_text,
            record.logical_clock_ms,
            record.record_digest,
            record.raw_json,
        ),
    )


def metric_record_from_row(row: sqlite3.Row) -> MetricRecord:
    return MetricRecord(
        record_id=str(row["record_id"]),
        seq=int(row["seq"]),
        experiment_id=str(row["experiment_id"]),
        metric=str(row["metric"]),
        step=int(row["step"]),
        value_text=str(row["value_text"]),
        logical_clock_ms=int(row["logical_clock_ms"]),
        record_digest=str(row["record_digest"]),
        raw_json=str(row["raw_json"]),
    )


def load_metric_records(connection: sqlite3.Connection) -> list[MetricRecord]:
    rows = connection.execute("SELECT * FROM metric_records ORDER BY seq").fetchall()
    return [metric_record_from_row(row) for row in rows]


def record_signature(records: Iterable[MetricRecord]) -> list[dict[str, Any]]:
    return [
        {
            "record_id": record.record_id,
            "seq": record.seq,
            "experiment_id": record.experiment_id,
            "metric": record.metric,
            "step": record.step,
            "value": record.value_text,
            "logical_clock_ms": record.logical_clock_ms,
            "digest": record.record_digest,
        }
        for record in records
    ]


def snapshot_view_rows(
    connection: sqlite3.Connection, snapshot_version: str
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT * FROM snapshot_view_entries
        WHERE snapshot_version = ?
        ORDER BY experiment_id, metric
        """,
        (snapshot_version,),
    ).fetchall()


def known_metrics(
    connection: sqlite3.Connection,
    snapshot_version: str,
    experiments: dict[str, Experiment],
) -> dict[tuple[str, str], None]:
    metrics: dict[tuple[str, str], None] = {}
    for row in connection.execute(
        "SELECT DISTINCT experiment_id, metric FROM metric_records"
    ):
        metrics[(row["experiment_id"], row["metric"])] = None
    for row in snapshot_view_rows(connection, snapshot_version):
        metrics[(row["experiment_id"], row["metric"])] = None
    for experiment in experiments.values():
        for metric in experiment.baseline_metrics:
            metrics[(experiment.id, metric)] = None
        for metric in experiment.metric_references:
            metrics[(experiment.id, metric)] = None
    return metrics


def active_metrics(
    connection: sqlite3.Connection,
    snapshot_version: str,
    experiments: dict[str, Experiment],
) -> dict[tuple[str, str], None]:
    active = known_metrics(connection, snapshot_version, experiments)
    tombstones = {
        (str(row["experiment_id"]), str(row["metric"]))
        for row in connection.execute(
            """
            SELECT experiment_id, metric FROM snapshot_view_entries
            WHERE snapshot_version = ? AND tombstone = 1
            """,
            (snapshot_version,),
        )
    }
    return {key: None for key in active if key not in tombstones}

