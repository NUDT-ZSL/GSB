from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .dataset import query_bundle
from .errors import conflict, fail
from .locking import lock_path
from .probe_plan import ProbePlan
from .schema import Sample, parse_sample
from .util import canonical_json_bytes, canonical_json_text, content_digest


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_versions (
    version INTEGER PRIMARY KEY,
    digest TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    sample_json TEXT NOT NULL,
    sample_digest TEXT NOT NULL,
    created_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    pointer_version INTEGER PRIMARY KEY,
    policy_version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    created_seq INTEGER NOT NULL,
    FOREIGN KEY(policy_version) REFERENCES policy_versions(version)
);

CREATE TABLE IF NOT EXISTS current_snapshot (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    pointer_version INTEGER NOT NULL,
    FOREIGN KEY(pointer_version) REFERENCES snapshots(pointer_version)
);

CREATE TABLE IF NOT EXISTS rounds (
    round_id INTEGER PRIMARY KEY,
    request_digest TEXT NOT NULL,
    request_json TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    sample_digest TEXT NOT NULL,
    base_pointer_version INTEGER NOT NULL,
    target_domains_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('prepared', 'committed')),
    result_json TEXT,
    result_digest TEXT,
    created_seq INTEGER NOT NULL,
    FOREIGN KEY(policy_version) REFERENCES policy_versions(version),
    FOREIGN KEY(base_pointer_version) REFERENCES snapshots(pointer_version)
);

CREATE TABLE IF NOT EXISTS round_domains (
    round_id INTEGER NOT NULL,
    domain TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('completed', 'failed', 'abandoned')),
    outcome_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    created_seq INTEGER NOT NULL,
    PRIMARY KEY(round_id, domain),
    FOREIGN KEY(round_id) REFERENCES rounds(round_id)
);

CREATE TABLE IF NOT EXISTS switch_ledger (
    round_id INTEGER NOT NULL,
    service TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    domain TEXT NOT NULL,
    from_instance TEXT NOT NULL,
    to_instance TEXT NOT NULL,
    reason TEXT NOT NULL,
    pointer_version_after INTEGER NOT NULL,
    logical_time_ms INTEGER NOT NULL,
    created_seq INTEGER NOT NULL,
    PRIMARY KEY(round_id, service, policy_version),
    FOREIGN KEY(round_id) REFERENCES rounds(round_id),
    FOREIGN KEY(policy_version) REFERENCES policy_versions(version),
    FOREIGN KEY(pointer_version_after) REFERENCES snapshots(pointer_version)
);

CREATE TABLE IF NOT EXISTS query_summaries (
    pointer_version INTEGER NOT NULL,
    service TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY(pointer_version, service),
    FOREIGN KEY(pointer_version) REFERENCES snapshots(pointer_version)
);

CREATE INDEX IF NOT EXISTS idx_rounds_status ON rounds(status);
CREATE INDEX IF NOT EXISTS idx_switch_ledger_service
    ON switch_ledger(service, policy_version, round_id);
"""


@dataclass(frozen=True, slots=True)
class Snapshot:
    pointer_version: int
    policy_version: int
    digest: str
    state: dict[str, Any]
    sample: Sample
    summary_rows: dict[str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class RoundRecord:
    round_id: int
    request_digest: str
    request: dict[str, Any]
    plan: ProbePlan
    policy_version: int
    sample_digest: str
    base_pointer_version: int
    target_domains: tuple[str, ...]
    status: str
    result: dict[str, Any] | None
    result_digest: str | None


def build_initial_snapshot(sample: Sample, *, pointer_version: int) -> dict[str, Any]:
    service_states: dict[str, Any] = {}
    summaries: dict[str, Any] = {}
    for service in sample.services.values():
        service_states[service.name] = {
            "failback_step": -1,
            "initial_primary": service.initial_primary,
            "last_switch_clock_ms": None,
            "last_transition_round": 0,
            "primary": service.initial_primary,
            "primary_revision": 0,
            "switched_away": False,
        }
        bundle = query_bundle(
            service,
            service.initial_primary,
            policy_version=sample.policy.version,
            pointer_version=pointer_version,
        )
        summaries[service.name] = bundle
    return {
        "clock_ms": 0,
        "created_round": 0,
        "frozen_domains": [],
        "last_event_seq": 0,
        "policy_digest": sample.policy_digest,
        "policy_version": sample.policy.version,
        "pointer_version": pointer_version,
        "query_summaries": summaries,
        "round_watermark": 0,
        "sample_digest": sample.sample_digest,
        "sample_id": sample.sample_id,
        "schema_version": 1,
        "services": service_states,
    }


class Storage:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.db_path = self.state_dir / "workbench.sqlite3"
        self.snapshot_dir = self.state_dir / "snapshots"
        self.lock_dir = self.state_dir / "locks"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.db_path,
            timeout=20.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 20000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")

    def initialize(self) -> None:
        with lock_path(self.lock_dir / "schema.lock"):
            with self.connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.executescript(SCHEMA)
                connection.execute(
                    "INSERT OR IGNORE INTO meta(key, value) "
                    "VALUES ('commit_seq', '0')"
                )

    def _next_seq(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'commit_seq'"
        ).fetchone()
        current = int(row["value"])
        next_value = current + 1
        connection.execute(
            "UPDATE meta SET value = ? WHERE key = 'commit_seq'",
            (str(next_value),),
        )
        return next_value

    def write_snapshot_file(
        self,
        pointer_version: int,
        snapshot_json: str,
        digest: str,
    ) -> Path:
        path = self.snapshot_dir / f"pointer-{pointer_version:010d}.json"
        payload = canonical_json_bytes(
            {
                "digest": digest,
                "pointer_version": pointer_version,
                "snapshot": json.loads(snapshot_json),
            }
        )
        temp_path = path.with_suffix(".json.tmp")
        with open(temp_path, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        return path

    def publish_current(self, pointer_version: int) -> None:
        with lock_path(self.lock_dir / "snapshot-current.lock"):
            current = self.snapshot_dir / "CURRENT"
            current_temp = current.with_suffix(".tmp")
            with open(current_temp, "wb") as handle:
                handle.write(f"{pointer_version:010d}\n".encode("ascii"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(current_temp, current)

    def _snapshot_from_row(self, row: sqlite3.Row) -> Snapshot:
        state = json.loads(row["snapshot_json"])
        sample = parse_sample(json.loads(row["sample_json"]))
        with self.connect() as connection:
            summary_rows = {
                summary_row["service"]: json.loads(summary_row["summary_json"])
                for summary_row in connection.execute(
                    "SELECT service, summary_json FROM query_summaries "
                    "WHERE pointer_version = ? ORDER BY service",
                    (state["pointer_version"],),
                )
            }
        return Snapshot(
            pointer_version=state["pointer_version"],
            policy_version=state["policy_version"],
            digest=row["digest"],
            state=state,
            sample=sample,
            summary_rows=summary_rows,
        )

    def current_snapshot(self) -> Snapshot | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT s.pointer_version, s.digest, s.snapshot_json,
                       p.sample_json
                  FROM current_snapshot c
                  JOIN snapshots s ON s.pointer_version = c.pointer_version
                  JOIN policy_versions p ON p.version = s.policy_version
                 WHERE c.singleton = 1
                """
            ).fetchone()
        return None if row is None else self._snapshot_from_row(row)

    def snapshot_by_pointer(self, pointer_version: int) -> Snapshot | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT s.pointer_version, s.digest, s.snapshot_json,
                       p.sample_json
                  FROM snapshots s
                  JOIN policy_versions p ON p.version = s.policy_version
                 WHERE s.pointer_version = ?
                """,
                (pointer_version,),
            ).fetchone()
        return None if row is None else self._snapshot_from_row(row)

    def latest_policy(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT version, digest, sample_digest
                  FROM policy_versions
                 ORDER BY version DESC
                 LIMIT 1
                """
            ).fetchone()
        return None if row is None else dict(row)

    def load_sample(
        self,
        sample: Sample,
        *,
        expected_sample_digest: str | None,
    ) -> dict[str, Any]:
        if (
            expected_sample_digest is not None
            and expected_sample_digest != sample.sample_digest
        ):
            fail(
                "SAMPLE_DIGEST_MISMATCH",
                "Loaded sample digest does not match the expected digest.",
                field_path="sample",
                context={
                    "actual": sample.sample_digest,
                    "expected": expected_sample_digest,
                },
                http_status=409,
            )

        with lock_path(self.lock_dir / "load.lock"):
            response: dict[str, Any] | None = None
            pointer_to_publish: int | None = None
            with self.transaction() as connection:
                current_row = connection.execute(
                    """
                    SELECT s.pointer_version, s.snapshot_json, s.policy_version,
                           p.digest AS policy_digest, p.sample_digest
                      FROM current_snapshot c
                      JOIN snapshots s ON s.pointer_version = c.pointer_version
                      JOIN policy_versions p ON p.version = s.policy_version
                     WHERE c.singleton = 1
                    """
                ).fetchone()

                if current_row is not None and response is None:
                    if (
                        current_row["policy_digest"] == sample.policy_digest
                        and current_row["sample_digest"] == sample.sample_digest
                    ):
                        state = json.loads(current_row["snapshot_json"])
                        response = {
                            "idempotent": True,
                            "pointer_version": state["pointer_version"],
                            "policy_version": state["policy_version"],
                            "sample_digest": sample.sample_digest,
                            "policy_digest": sample.policy_digest,
                            "status": "loaded",
                        }
                        pointer_to_publish = state["pointer_version"]
                    latest = connection.execute(
                        "SELECT version, digest, sample_digest FROM policy_versions "
                        "ORDER BY version DESC LIMIT 1"
                    ).fetchone()
                    if sample.policy.version < latest["version"]:
                        fail(
                            "POLICY_VERSION_ROLLBACK",
                            "Policy version rollback is not allowed.",
                            field_path="policy.version",
                            context={
                                "current_policy_version": latest["version"],
                                "incoming_policy_version": sample.policy.version,
                            },
                            http_status=409,
                        )
                    if sample.policy.version == latest["version"]:
                        conflict(
                            "POLICY_VERSION_CONFLICT",
                            "A different policy document cannot replace an "
                            "existing policy version.",
                            field_path="policy",
                            context={
                                "incoming_policy_digest": sample.policy_digest,
                                "existing_policy_digest": latest["digest"],
                                "policy_version": sample.policy.version,
                            },
                        )
                    prepared = connection.execute(
                        "SELECT round_id FROM rounds WHERE status = 'prepared' "
                        "ORDER BY round_id LIMIT 1"
                    ).fetchone()
                    if prepared is not None:
                        conflict(
                            "STATE_CONFLICT",
                            "Cannot replace the policy while a round is prepared.",
                            context={"round_id": prepared["round_id"]},
                        )

                if response is None:
                    seq = self._next_seq(connection)
                    connection.execute(
                        """
                        INSERT INTO policy_versions(
                            version, digest, policy_json, sample_json,
                            sample_digest, created_seq
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sample.policy.version,
                            sample.policy_digest,
                            canonical_json_text(sample.policy_raw),
                            canonical_json_text(sample.raw),
                            sample.sample_digest,
                            seq,
                        ),
                    )
                    pointer_version = (
                        1
                        if current_row is None
                        else int(current_row["pointer_version"]) + 1
                    )
                    state = build_initial_snapshot(
                        sample,
                        pointer_version=pointer_version,
                    )
                    snapshot_json = canonical_json_text(state)
                    snapshot_digest = content_digest(state)
                    self.write_snapshot_file(
                        pointer_version,
                        snapshot_json,
                        snapshot_digest,
                    )
                    connection.execute(
                        """
                        INSERT INTO snapshots(
                            pointer_version, policy_version, snapshot_json,
                            digest, created_seq
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            pointer_version,
                            sample.policy.version,
                            snapshot_json,
                            snapshot_digest,
                            seq,
                        ),
                    )
                    connection.execute(
                        "INSERT OR REPLACE INTO current_snapshot"
                        "(singleton, pointer_version) VALUES (1, ?)",
                        (pointer_version,),
                    )
                    for service_name, summary in state["query_summaries"].items():
                        connection.execute(
                            """
                            INSERT INTO query_summaries(
                                pointer_version, service, summary_json, digest
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (
                                pointer_version,
                                service_name,
                                canonical_json_text(summary),
                                summary["query_digest"],
                            ),
                        )
                    response = {
                        "idempotent": False,
                        "pointer_version": pointer_version,
                        "policy_version": sample.policy.version,
                        "sample_digest": sample.sample_digest,
                        "policy_digest": sample.policy_digest,
                        "status": "loaded",
                        "snapshot_digest": snapshot_digest,
                    }
                    pointer_to_publish = pointer_version
            if pointer_to_publish is not None:
                self.publish_current(pointer_to_publish)
            assert response is not None
            return response

    def prepare_round(
        self,
        *,
        round_id: int,
        request: dict[str, Any],
        request_digest: str,
        plan: ProbePlan,
        sample: Sample,
        base_pointer_version: int,
        target_domains: tuple[str, ...],
        expected_policy_version: int | None,
        expected_pointer_version: int | None,
    ) -> RoundRecord:
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM rounds WHERE round_id = ?",
                (round_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    conflict(
                        "IDEMPOTENCY_CONFLICT",
                        "The same round_id was submitted with different input.",
                        context={
                            "existing_request_digest": existing["request_digest"],
                            "incoming_request_digest": request_digest,
                            "round_id": round_id,
                        },
                    )
                return self._round_from_row(existing)

            another_prepared = connection.execute(
                "SELECT round_id FROM rounds WHERE status = 'prepared' "
                "ORDER BY round_id LIMIT 1"
            ).fetchone()
            if another_prepared is not None:
                conflict(
                    "PREPARED_ROUND_EXISTS",
                    "Resume the prepared round before starting a new round.",
                    context={
                        "prepared_round_id": another_prepared["round_id"],
                        "round_id": round_id,
                    },
                )

            current = connection.execute(
                """
                SELECT s.pointer_version, s.policy_version, p.digest AS policy_digest,
                       s.policy_version AS snapshot_policy_version
                  FROM current_snapshot c
                  JOIN snapshots s ON s.pointer_version = c.pointer_version
                  JOIN policy_versions p ON p.version = s.policy_version
                 WHERE c.singleton = 1
                """
            ).fetchone()
            if current is None:
                fail(
                    "NOT_LOADED",
                    "No sample has been loaded.",
                    http_status=404,
                )
            if int(current["pointer_version"]) != base_pointer_version:
                conflict(
                    "STATE_CONFLICT",
                    "The committed pointer changed while planning the round.",
                    context={
                        "base_pointer_version": base_pointer_version,
                        "current_pointer_version": current["pointer_version"],
                        "round_id": round_id,
                    },
                )
            if expected_pointer_version is not None and (
                int(current["pointer_version"]) != expected_pointer_version
            ):
                conflict(
                    "POINTER_VERSION_CONFLICT",
                    "Expected primary pointer version does not match current state.",
                    context={
                        "current_pointer_version": current["pointer_version"],
                        "expected_pointer_version": expected_pointer_version,
                        "round_id": round_id,
                    },
                )
            if expected_policy_version is not None and (
                int(current["snapshot_policy_version"]) != expected_policy_version
            ):
                conflict(
                    "POLICY_VERSION_CONFLICT",
                    "Expected policy version does not match current state.",
                    context={
                        "current_policy_version": current["snapshot_policy_version"],
                        "expected_policy_version": expected_policy_version,
                        "round_id": round_id,
                    },
                )

            max_round = connection.execute(
                "SELECT COALESCE(MAX(round_id), 0) AS max_round FROM rounds"
            ).fetchone()["max_round"]
            if round_id <= int(max_round):
                conflict(
                    "ROUND_REGRESSION",
                    "A new round must have an id greater than every known round.",
                    context={
                        "max_round": max_round,
                        "round_id": round_id,
                    },
                )

            seq = self._next_seq(connection)
            connection.execute(
                """
                INSERT INTO rounds(
                    round_id, request_digest, request_json, plan_json,
                    policy_version, sample_digest, base_pointer_version,
                    target_domains_json, status, result_json, result_digest,
                    created_seq
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', NULL, NULL, ?)
                """,
                (
                    round_id,
                    request_digest,
                    canonical_json_text(request),
                    canonical_json_text(plan.as_dict()),
                    sample.policy.version,
                    sample.sample_digest,
                    base_pointer_version,
                    canonical_json_text(list(target_domains)),
                    seq,
                ),
            )
            row = connection.execute(
                "SELECT * FROM rounds WHERE round_id = ?",
                (round_id,),
            ).fetchone()
            return self._round_from_row(row)

    def _round_from_row(self, row: sqlite3.Row) -> RoundRecord:
        return RoundRecord(
            round_id=row["round_id"],
            request_digest=row["request_digest"],
            request=json.loads(row["request_json"]),
            plan=ProbePlan.from_dict(json.loads(row["plan_json"])),
            policy_version=row["policy_version"],
            sample_digest=row["sample_digest"],
            base_pointer_version=row["base_pointer_version"],
            target_domains=tuple(json.loads(row["target_domains_json"])),
            status=row["status"],
            result=(
                None if row["result_json"] is None else json.loads(row["result_json"])
            ),
            result_digest=row["result_digest"],
        )

    def get_round(self, round_id: int) -> RoundRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM rounds WHERE round_id = ?",
                (round_id,),
            ).fetchone()
        return None if row is None else self._round_from_row(row)

    def list_prepared_rounds(self) -> list[RoundRecord]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM rounds WHERE status = 'prepared' "
                "ORDER BY round_id"
            ).fetchall()
        return [self._round_from_row(row) for row in rows]

    def get_round_domains(self, round_id: int) -> dict[str, dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT domain, status, outcome_json, digest "
                "FROM round_domains WHERE round_id = ? ORDER BY domain",
                (round_id,),
            ).fetchall()
        return {
            row["domain"]: {
                "digest": row["digest"],
                "outcome": json.loads(row["outcome_json"]),
                "status": row["status"],
            }
            for row in rows
        }

    def save_domain_result(
        self,
        round_id: int,
        domain: str,
        status: str,
        outcome: dict[str, Any],
    ) -> dict[str, Any]:
        outcome_json = canonical_json_text(outcome)
        digest = content_digest(outcome)
        with self.transaction() as connection:
            round_row = connection.execute(
                "SELECT status FROM rounds WHERE round_id = ?",
                (round_id,),
            ).fetchone()
            if round_row is None:
                fail(
                    "ROUND_NOT_FOUND",
                    "Round does not exist.",
                    context={"round_id": round_id},
                    http_status=404,
                )
            existing = connection.execute(
                "SELECT status, outcome_json, digest FROM round_domains "
                "WHERE round_id = ? AND domain = ?",
                (round_id, domain),
            ).fetchone()
            if existing is not None:
                return {
                    "digest": existing["digest"],
                    "outcome": json.loads(existing["outcome_json"]),
                    "status": existing["status"],
                }
            if round_row["status"] != "prepared":
                conflict(
                    "ROUND_ALREADY_COMMITTED",
                    "Cannot add a domain result to a committed round.",
                    context={"round_id": round_id, "domain": domain},
                )
            seq = self._next_seq(connection)
            connection.execute(
                """
                INSERT INTO round_domains(
                    round_id, domain, status, outcome_json, digest, created_seq
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (round_id, domain, status, outcome_json, digest, seq),
            )
            return {
                "digest": digest,
                "outcome": outcome,
                "status": status,
            }

    def finalize_round(
        self,
        *,
        round_record: RoundRecord,
        final_state: dict[str, Any],
        query_summaries: dict[str, dict[str, Any]],
        switches: list[dict[str, Any]],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        snapshot_json = canonical_json_text(final_state)
        snapshot_digest = content_digest(final_state)
        result_json = canonical_json_text(result)
        result_digest = content_digest(result)
        pointer_version = int(final_state["pointer_version"])

        response: dict[str, Any] | None = None
        pointer_to_publish: int | None = None
        with self.transaction() as connection:
            current_round = connection.execute(
                "SELECT * FROM rounds WHERE round_id = ?",
                (round_record.round_id,),
            ).fetchone()
            if current_round is None:
                fail(
                    "ROUND_NOT_FOUND",
                    "Round does not exist.",
                    context={"round_id": round_record.round_id},
                    http_status=404,
                )
            if current_round["status"] == "committed":
                response = json.loads(current_round["result_json"])
                pointer_to_publish = response["pointer_version"]

            if response is None:
                domain_rows = connection.execute(
                    "SELECT domain, status FROM round_domains WHERE round_id = ?",
                    (round_record.round_id,),
                ).fetchall()
                known = {row["domain"] for row in domain_rows}
                missing = [
                    domain
                    for domain in round_record.target_domains
                    if domain not in known
                ]
                if missing:
                    conflict(
                        "ROUND_INCOMPLETE",
                        "Not all target domains have reached a terminal outcome.",
                        context={
                            "missing_domains": sorted(missing),
                            "round_id": round_record.round_id,
                        },
                    )

                current = connection.execute(
                    """
                    SELECT s.pointer_version
                      FROM current_snapshot c
                      JOIN snapshots s ON s.pointer_version = c.pointer_version
                     WHERE c.singleton = 1
                    """
                ).fetchone()
                if current is None:
                    fail("NOT_LOADED", "No sample has been loaded.", http_status=404)
                if (
                    int(current["pointer_version"])
                    != round_record.base_pointer_version
                ):
                    conflict(
                        "STATE_CONFLICT",
                        "Another round committed on top of this round's base snapshot.",
                        context={
                            "base_pointer_version": round_record.base_pointer_version,
                            "current_pointer_version": current["pointer_version"],
                            "round_id": round_record.round_id,
                        },
                    )

                seq = self._next_seq(connection)
                self.write_snapshot_file(
                    pointer_version,
                    snapshot_json,
                    snapshot_digest,
                )
                connection.execute(
                    """
                    INSERT INTO snapshots(
                        pointer_version, policy_version, snapshot_json,
                        digest, created_seq
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        pointer_version,
                        final_state["policy_version"],
                        snapshot_json,
                        snapshot_digest,
                        seq,
                    ),
                )
                for service_name, summary in query_summaries.items():
                    connection.execute(
                        """
                        INSERT INTO query_summaries(
                            pointer_version, service, summary_json, digest
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            pointer_version,
                            service_name,
                            canonical_json_text(summary),
                            summary["query_digest"],
                        ),
                    )
                for switch in switches:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO switch_ledger(
                            round_id, service, policy_version, domain,
                            from_instance, to_instance, reason,
                            pointer_version_after, logical_time_ms, created_seq
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            round_record.round_id,
                            switch["service"],
                            round_record.policy_version,
                            switch["domain"],
                            switch["from_instance"],
                            switch["to_instance"],
                            switch["reason"],
                            pointer_version,
                            switch["logical_time_ms"],
                            seq,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE rounds
                       SET status = 'committed', result_json = ?, result_digest = ?
                     WHERE round_id = ?
                    """,
                    (result_json, result_digest, round_record.round_id),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO current_snapshot"
                    "(singleton, pointer_version) VALUES (1, ?)",
                    (pointer_version,),
                )
                response = json.loads(result_json)
                pointer_to_publish = pointer_version
        if pointer_to_publish is not None:
            self.publish_current(pointer_to_publish)
        assert response is not None
        return response

    def status(self) -> dict[str, Any]:
        snapshot = self.current_snapshot()
        if snapshot is None:
            return {"loaded": False}
        state = snapshot.state
        sample = snapshot.sample
        prepared = self.list_prepared_rounds()
        services = []
        for service in sample.services.values():
            service_state = state["services"][service.name]
            policy = sample.policy.services[service.name]
            services.append(
                {
                    "cooldown_ms": policy.cooldown_ms,
                    "domain": service.domain,
                    "failback_ladder": list(policy.failback_ladder),
                    "failback_step": service_state["failback_step"],
                    "initial_primary": service_state["initial_primary"],
                    "last_switch_clock_ms": service_state["last_switch_clock_ms"],
                    "last_transition_round": service_state["last_transition_round"],
                    "mode": policy.mode,
                    "primary": service_state["primary"],
                    "primary_revision": service_state["primary_revision"],
                    "service": service.name,
                    "switched_away": service_state["switched_away"],
                }
            )
        services.sort(key=lambda item: item["service"])
        return {
            "clock_ms": state["clock_ms"],
            "frozen_domains": sorted(state["frozen_domains"]),
            "last_event_seq": state["last_event_seq"],
            "loaded": True,
            "locked_dimensions": {
                "frozen_domains": len(state["frozen_domains"]),
                "services": len(services),
            },
            "pointer_version": state["pointer_version"],
            "policy_digest": state["policy_digest"],
            "policy_version": state["policy_version"],
            "prepared_rounds": [
                {
                    "base_pointer_version": item.base_pointer_version,
                    "completed_domains": sorted(
                        self.get_round_domains(item.round_id)
                    ),
                    "round_id": item.round_id,
                    "status": item.status,
                    "target_domains": list(item.target_domains),
                }
                for item in prepared
            ],
            "round_watermark": state["round_watermark"],
            "sample_digest": state["sample_digest"],
            "sample_id": state["sample_id"],
            "services": services,
            "snapshot_digest": snapshot.digest,
        }

    def switch_ledger(self, round_id: int | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM switch_ledger"
        params: tuple[Any, ...] = ()
        if round_id is not None:
            query += " WHERE round_id = ?"
            params = (round_id,)
        query += " ORDER BY round_id, service"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def domain_outcome(
        self,
        round_id: int,
        domain: str,
    ) -> dict[str, Any] | None:
        domains = self.get_round_domains(round_id)
        item = domains.get(domain)
        return None if item is None else item["outcome"]

    def all_rounds(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT round_id, status, base_pointer_version, request_json, "
                "result_json FROM rounds ORDER BY round_id"
            ).fetchall()
        return [dict(row) for row in rows]

