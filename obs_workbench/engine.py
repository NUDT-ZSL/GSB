from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_json, digest
from .errors import (
    E_EVENT_SEQUENCE_DUPLICATE,
    E_EVENT_SEQUENCE_CONFLICT,
    E_PATCH_PARENT_MISMATCH,
    E_RESUME_REQUIRED,
    E_STATE_CORRUPT,
    E_TOPOLOGY_CONFIG_MISMATCH,
    E_TOPOLOGY_VERSION_CONFLICT,
    E_TOPOLOGY_VERSION_ROLLBACK,
    E_WINDOW_DIGEST_MISMATCH,
    WorkbenchError,
)
from .faults import failpoint
from .samples import (
    Event,
    PatchPlan,
    inspect_patch_payload,
    load_json_file,
    parse_events_file,
    parse_patch_payload,
    parse_topology_file,
)
from .storage import SCHEMA_VERSION, Store
from .timewin import (
    build_evidence_chain,
    build_window_revision,
    closes_at,
    event_to_dict,
    is_late,
    parse_revision_jsons,
    window_bounds,
    window_key,
)
from .topology import Instance, SilenceWindow, Topology


@contextlib.contextmanager
def transaction(connection: Any) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _seq_db_key(event: Event) -> str:
    if event.event_seq.kind == 0:
        return f"i:{event.event_seq.number}"
    return f"s:{event.event_seq.text}"


class Workbench:
    def __init__(self, db_path: Path):
        self.store = Store(db_path)

    # ------------------------------------------------------------------ load
    def load(self, topology_path: Path) -> dict[str, Any]:
        snapshot = parse_topology_file(topology_path)
        with self.store.writer(create=True) as connection:
            with transaction(connection):
                published_version = Store.published_version(connection)
                if published_version is None:
                    content_digest = snapshot.content_digest()
                    Store.store_topology(
                        connection,
                        snapshot,
                        parent_version=None,
                        content_digest=content_digest,
                        source_kind="snapshot",
                        source_digest=content_digest,
                        published=True,
                    )
                    self._initialize_config(connection, snapshot)
                    self._publish_pointer(connection, snapshot.version)
                    Store.set_state(connection, "max_seen_topology_version", str(snapshot.version))
                    Store.set_state(
                        connection,
                        "published_effective_clock_ms",
                        str(snapshot.effective_clock_ms),
                    )
                    result = {
                        "command": "load",
                        "idempotent": False,
                        "published_topology_version": snapshot.version,
                    }
                    failpoint("load_after_intent")
                    return result

                current = Store.load_topology(connection, published_version)
                self._assert_config_compatible(connection, snapshot)
                content_digest = snapshot.content_digest()
                current_digest = current.content_digest()
                if snapshot.version == published_version:
                    if content_digest != current_digest:
                        raise WorkbenchError(
                            E_TOPOLOGY_VERSION_CONFLICT,
                            "the published topology version was loaded with different content",
                            {
                                "existing_digest": current_digest,
                                "new_digest": content_digest,
                                "topology_version": snapshot.version,
                            },
                        )
                    return {
                        "command": "load",
                        "idempotent": True,
                        "published_topology_version": published_version,
                    }
                if snapshot.version < published_version:
                    raise WorkbenchError(
                        E_TOPOLOGY_VERSION_ROLLBACK,
                        "topology snapshot version is older than the published version",
                        {
                            "attempted_version": snapshot.version,
                            "published_topology_version": published_version,
                        },
                    )
                if Store.pending_jobs(connection):
                    raise WorkbenchError(
                        E_RESUME_REQUIRED,
                        "cannot load a new topology while propagation is incomplete",
                        {"published_topology_version": published_version},
                    )
                triggers = self._diff_triggers(current, snapshot)
                job_id = self._stage_transition(
                    connection,
                    current,
                    snapshot,
                    kind="load",
                    source_digest=content_digest,
                    payload_json=canonical_json(snapshot.canonical()),
                    triggers=triggers,
                )
                failpoint("load_after_intent")
                result = {
                    "command": "load",
                    "idempotent": False,
                    "job_id": job_id,
                    "parent_topology_version": published_version,
                    "published_topology_version": snapshot.version,
                }
        processed = self._process_all_pending(connection=None)
        result.update(processed)
        return result

    def _initialize_config(self, connection: Any, topology: Topology) -> None:
        Store.set_state(connection, "window_ms", str(topology.window_ms))
        Store.set_state(connection, "allowed_lateness_ms", str(topology.allowed_lateness_ms))

    def _assert_config_compatible(self, connection: Any, topology: Topology) -> None:
        window_ms = Store.get_state(connection, "window_ms")
        lateness = Store.get_state(connection, "allowed_lateness_ms")
        if window_ms is not None and int(window_ms) != topology.window_ms:
            raise WorkbenchError(
                E_TOPOLOGY_CONFIG_MISMATCH,
                "window_ms cannot change after ingestion has started",
                {"existing": int(window_ms), "provided": topology.window_ms},
            )
        if lateness is not None and int(lateness) != topology.allowed_lateness_ms:
            raise WorkbenchError(
                E_TOPOLOGY_CONFIG_MISMATCH,
                "allowed_lateness_ms cannot change after ingestion has started",
                {"existing": int(lateness), "provided": topology.allowed_lateness_ms},
            )

    # ---------------------------------------------------------------- ingest
    def ingest(self, events_path: Path, *, isolate_buckets: bool = False) -> dict[str, Any]:
        if isolate_buckets:
            return self._ingest_isolated(events_path)
        with self.store.writer(create=False) as connection:
            topology = Store.load_published_topology(connection)
            if topology is None:
                raise WorkbenchError(E_STATE_CORRUPT, "load a topology before ingesting events")
            Store.ensure_no_pending_jobs(connection)
            events = parse_events_file(events_path, topology)
            with transaction(connection):
                return self._ingest_events(connection, topology, events)

    def _ingest_isolated(self, events_path: Path) -> dict[str, Any]:
        # Parse the full file first so structural errors never create a partial
        # bucket. Bucket-level event-reference errors are isolated below.
        with self.store.writer(create=False) as connection:
            topology = Store.load_published_topology(connection)
            if topology is None:
                raise WorkbenchError(E_STATE_CORRUPT, "load a topology before ingesting events")
            Store.ensure_no_pending_jobs(connection)
            from .samples import _event_buckets, load_json_file, parse_events_payload

            payload = load_json_file(events_path)
            buckets = _event_buckets(payload)
            seen_sequences: dict[tuple[type, object], str] = {}
            for bucket_id, items in buckets:
                for index, item in enumerate(items):
                    if not isinstance(item, dict):
                        continue
                    raw_seq = item.get("event_seq")
                    if isinstance(raw_seq, bool) or not isinstance(raw_seq, (int, str)):
                        continue
                    sequence_key = (type(raw_seq), raw_seq)
                    if sequence_key in seen_sequences:
                        raise WorkbenchError(
                            E_EVENT_SEQUENCE_DUPLICATE,
                            "event_seq values must be unique across isolated buckets",
                            {
                                "bucket_id": bucket_id,
                                "event_seq": raw_seq,
                                "first_bucket_id": seen_sequences[sequence_key],
                                "index": index,
                            },
                        )
                    seen_sequences[sequence_key] = bucket_id
            results: list[dict[str, Any]] = []
            total_processed = 0
            total_skipped = 0
            for bucket_id, items in buckets:
                try:
                    events = parse_events_payload({"events": items, "bucket_id": bucket_id}, topology)
                    with transaction(connection):
                        result = self._ingest_events(connection, topology, events)
                    results.append(
                        {
                            "bucket_id": bucket_id,
                            "ok": True,
                            "processed": result["processed_events"],
                            "skipped": result["skipped_events"],
                        }
                    )
                    total_processed += result["processed_events"]
                    total_skipped += result["skipped_events"]
                except WorkbenchError as exc:
                    results.append(
                        {
                            "bucket_id": bucket_id,
                            "error": exc.to_dict(),
                            "ok": False,
                        }
                    )
            return {
                "command": "ingest",
                "command_ok": all(item["ok"] for item in results),
                "isolated_buckets": results,
                "processed_events": total_processed,
                "skipped_events": total_skipped,
            }

    def _ingest_events(
        self,
        connection: Any,
        topology: Topology,
        events: list[Event],
    ) -> dict[str, Any]:
        existing: dict[str, str] = {
            str(row["event_seq"]): str(row["payload_json"])
            for row in connection.execute("SELECT event_seq, payload_json FROM events")
        }
        pending: list[Event] = []
        skipped = 0
        for event in events:
            db_key = _seq_db_key(event)
            if db_key in existing:
                if existing[db_key] != event.payload_json:
                    raise WorkbenchError(
                        E_EVENT_SEQUENCE_CONFLICT,
                        "event_seq was already persisted with different content",
                        {"event_seq": event.event_seq.raw},
                    )
                skipped += 1
            else:
                pending.append(event)

        for event in pending:
            self._ingest_one(connection, topology, event)

        return {
            "command": "ingest",
            "processed_events": len(pending),
            "skipped_events": skipped,
            "topology_version": topology.version,
        }

    def _ingest_one(self, connection: Any, topology: Topology, event: Event) -> None:
        db_key = _seq_db_key(event)
        silence_matches_flag = any(
            silence.matches(
                event.instance_id,
                topology.instances[event.instance_id].service_name,
                event.rule_name,
                event.occurred_clock_ms,
            )
            for silence in topology.silences.values()
        )
        silenced_standalone = bool(event.silenced_maintenance and not silence_matches_flag)
        connection.execute(
            """
            INSERT INTO events(
                event_seq, seq_kind, seq_num, seq_text, instance_id, rule_name,
                severity, occurred_clock_ms, arrived_clock_ms, metric_value,
                silenced_maintenance, silenced_standalone, payload_json,
                ingested_topology_version
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                db_key,
                event.event_seq.kind,
                event.event_seq.number,
                event.event_seq.text,
                event.instance_id,
                event.rule_name,
                event.severity,
                event.occurred_clock_ms,
                event.arrived_clock_ms,
                event.metric_value,
                1 if event.silenced_maintenance else 0,
                1 if silenced_standalone else 0,
                event.payload_json,
                topology.version,
            ),
        )
        wm_row = connection.execute(
            "SELECT watermark_clock_ms FROM watermarks WHERE instance_id = ? AND rule_name = ?",
            (event.instance_id, event.rule_name),
        ).fetchone()
        watermark = int(wm_row["watermark_clock_ms"]) if wm_row is not None else None
        late = is_late(event.occurred_clock_ms, watermark)
        start_ms, end_ms = window_bounds(event.occurred_clock_ms, topology.window_ms)
        key = window_key(event.instance_id, event.rule_name, start_ms, end_ms)
        window = connection.execute(
            "SELECT * FROM windows WHERE window_key = ?",
            (key,),
        ).fetchone()

        close_new_window = False
        if window is not None and str(window["state"]) == "CLOSED":
            membership = "LATE_CLOSED"
        elif (
            window is None
            and late
            and watermark is not None
            and closes_at(watermark, end_ms, topology.allowed_lateness_ms)
        ):
            membership = "LATE_CLOSED"
            close_new_window = True
        elif late:
            membership = "LATE_OPEN"
        else:
            membership = "ON_TIME"

        if window is None:
            connection.execute(
                """
                INSERT INTO windows(
                    window_key, instance_id, rule_name, window_start_ms, window_end_ms,
                    state, closed_at_watermark_ms, closed_topology_version,
                    current_revision_id, created_topology_version
                ) VALUES(?, ?, ?, ?, ?, 'OPEN', NULL, NULL, NULL, ?)
                """,
                (
                    key,
                    event.instance_id,
                    event.rule_name,
                    start_ms,
                    end_ms,
                    topology.version,
                ),
            )
        connection.execute(
            """
            INSERT INTO window_events(window_key, event_seq, membership, discovered_topology_version)
            VALUES(?, ?, ?, ?)
            """,
            (key, db_key, membership, topology.version),
        )

        if late:
            connection.execute(
                """
                INSERT INTO late_ledger(
                    event_seq, window_key, instance_id, rule_name, occurred_clock_ms,
                    watermark_at_arrival, reason, first_seen_topology_version
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    db_key,
                    key,
                    event.instance_id,
                    event.rule_name,
                    event.occurred_clock_ms,
                    watermark if watermark is not None else 0,
                    "late_after_watermark",
                    topology.version,
                ),
            )

        new_watermark = watermark
        if not late:
            new_watermark = (
                event.occurred_clock_ms
                if watermark is None
                else max(watermark, event.occurred_clock_ms)
            )
            connection.execute(
                """
                INSERT INTO watermarks(instance_id, rule_name, watermark_clock_ms)
                VALUES(?, ?, ?)
                ON CONFLICT(instance_id, rule_name)
                DO UPDATE SET watermark_clock_ms = excluded.watermark_clock_ms
                """,
                (event.instance_id, event.rule_name, new_watermark),
            )
            self._close_eligible_windows(
                connection,
                topology,
                event.instance_id,
                event.rule_name,
                new_watermark,
            )
        elif close_new_window and watermark is not None:
            created = connection.execute(
                "SELECT * FROM windows WHERE window_key = ?",
                (key,),
            ).fetchone()
            if created is not None:
                self._close_window(connection, topology, created, watermark)

    def _close_eligible_windows(
        self,
        connection: Any,
        topology: Topology,
        instance_id: str,
        rule_name: str,
        watermark: int,
    ) -> None:
        rows = list(
            connection.execute(
                """
                SELECT * FROM windows
                WHERE instance_id = ? AND rule_name = ? AND state = 'OPEN'
                ORDER BY window_start_ms, window_key
                """,
                (instance_id, rule_name),
            )
        )
        for window in rows:
            if not closes_at(watermark, int(window["window_end_ms"]), topology.allowed_lateness_ms):
                continue
            self._close_window(connection, topology, window, watermark)

    def _close_window(
        self,
        connection: Any,
        topology: Topology,
        window: Any,
        watermark: int,
    ) -> None:
        members = list(
            connection.execute(
                """
                SELECT e.*, we.membership
                FROM window_events we
                JOIN events e ON e.event_seq = we.event_seq
                WHERE we.window_key = ? AND we.membership != 'LATE_CLOSED'
                ORDER BY e.occurred_clock_ms, e.seq_kind, e.seq_num, e.seq_text
                """,
                (str(window["window_key"]),),
            )
        )
        late_keys = {
            str(row["event_seq"])
            for row in connection.execute(
                "SELECT event_seq FROM late_ledger WHERE window_key = ?",
                (str(window["window_key"]),),
            )
        }
        result, evidence, revision_digest = build_window_revision(
            topology=topology,
            window=dict(window),
            events=[event_to_dict(row) for row in members],
            closed_at_watermark_ms=watermark,
            late_event_seqs=late_keys,
            include_late_closed=False,
        )
        revision_id = self._insert_revision(
            connection,
            window_key_value=str(window["window_key"]),
            topology_version=topology.version,
            result=result,
            evidence=evidence,
            revision_digest=revision_digest,
            staged=False,
            reason="watermark_close",
        )
        connection.execute(
            """
            UPDATE windows
            SET state = 'CLOSED', closed_at_watermark_ms = ?,
                closed_topology_version = ?, current_revision_id = ?
            WHERE window_key = ?
            """,
            (watermark, topology.version, revision_id, str(window["window_key"])),
        )
        failpoint("ingest_after_window_close")

    def _insert_revision(
        self,
        connection: Any,
        *,
        window_key_value: str,
        topology_version: int,
        result: dict[str, Any],
        evidence: list[dict[str, Any]],
        revision_digest: str,
        staged: bool,
        reason: str,
    ) -> int:
        existing = connection.execute(
            """
            SELECT revision_id, digest FROM window_revisions
            WHERE window_key = ? AND topology_version = ?
            """,
            (window_key_value, topology_version),
        ).fetchone()
        if existing is not None:
            if str(existing["digest"]) != revision_digest:
                raise WorkbenchError(
                    E_WINDOW_DIGEST_MISMATCH,
                    "window revision already exists with a different digest",
                    {
                        "existing_digest": str(existing["digest"]),
                        "new_digest": revision_digest,
                        "topology_version": topology_version,
                        "window_key": window_key_value,
                    },
                )
            return int(existing["revision_id"])
        cursor = connection.execute(
            """
            INSERT INTO window_revisions(
                window_key, topology_version, state, input_mode, result_json,
                evidence_json, digest, staged, reason
            ) VALUES(?, ?, 'CLOSED', ?, ?, ?, ?, ?, ?)
            """,
            (
                window_key_value,
                topology_version,
                str(result.get("input_mode", "closure")),
                canonical_json(result),
                canonical_json(evidence),
                revision_digest,
                1 if staged else 0,
                reason,
            ),
        )
        return int(cursor.lastrowid)

    # ----------------------------------------------------------------- apply
    def apply(self, patch_path: Path) -> dict[str, Any]:
        with self.store.writer(create=False) as connection:
            published_version = Store.published_version(connection)
            if published_version is None:
                raise WorkbenchError(E_STATE_CORRUPT, "load a topology before applying a patch")
            current = Store.load_topology(connection, published_version)
            patch_payload = load_json_file(patch_path)
            inspection = inspect_patch_payload(patch_payload)
            pending = Store.pending_jobs(connection)
            target_row = connection.execute(
                "SELECT * FROM topologies WHERE version = ?",
                (inspection.target_version,),
            ).fetchone()

            if pending:
                matching = [
                    row
                    for row in pending
                    if int(row["target_version"]) == inspection.target_version
                    and target_row is not None
                    and str(target_row["source_digest"]) == inspection.content_digest
                ]
                if not matching:
                    raise WorkbenchError(
                        E_RESUME_REQUIRED,
                        "another topology propagation job is incomplete",
                        {"pending_target_versions": sorted(int(row["target_version"]) for row in pending)},
                    )
                result = self._process_jobs_locked(connection)
                return {
                    "command": "apply",
                    "idempotent": False,
                    "parent_topology_version": inspection.parent_version,
                    "published_topology_version": inspection.target_version,
                    "resumed": True,
                    **result,
                }

            if inspection.target_version < published_version:
                raise WorkbenchError(
                    E_TOPOLOGY_VERSION_ROLLBACK,
                    "patch target version is older than the published topology",
                    {
                        "published_topology_version": published_version,
                        "target_topology_version": inspection.target_version,
                    },
                )
            if inspection.target_version == published_version:
                if (
                    target_row is not None
                    and str(target_row["source_digest"]) == inspection.content_digest
                ):
                    return {
                        "command": "apply",
                        "idempotent": True,
                        "parent_topology_version": inspection.parent_version,
                        "published_topology_version": inspection.target_version,
                    }
                raise WorkbenchError(
                    E_TOPOLOGY_VERSION_CONFLICT,
                    "topology version is already published with different patch content",
                    {
                        "published_topology_version": published_version,
                        "target_topology_version": inspection.target_version,
                    },
                )
            plan = parse_patch_payload(patch_payload, current)
            if plan.parent_version != published_version:
                raise WorkbenchError(
                    E_PATCH_PARENT_MISMATCH,
                    "patch parent version is not the published topology version",
                    {
                        "parent_topology_version": plan.parent_version,
                        "published_topology_version": published_version,
                    },
                )
            if target_row is not None:
                if (
                    str(target_row["content_digest"]) != plan.new_topology.content_digest()
                    or str(target_row["source_digest"]) != plan.content_digest
                ):
                    raise WorkbenchError(
                        E_TOPOLOGY_VERSION_CONFLICT,
                        "staged topology version has different content",
                        {"target_topology_version": plan.target_version},
                    )
                job = connection.execute(
                    "SELECT job_id FROM jobs WHERE target_version = ?",
                    (plan.target_version,),
                ).fetchone()
                if job is None:
                    raise WorkbenchError(
                        E_STATE_CORRUPT,
                        "staged topology exists without a propagation job",
                        {"target_topology_version": plan.target_version},
                    )
                job_id = str(job["job_id"])
            else:
                triggers = self._patch_triggers(plan, current)
                with transaction(connection):
                    job_id = self._stage_transition(
                        connection,
                        current,
                        plan.new_topology,
                        kind="apply",
                        source_digest=plan.content_digest,
                        payload_json=plan.payload_json,
                        triggers=triggers,
                    )
                failpoint("apply_after_intent")
            result = self._process_jobs_locked(connection)
            return {
                "command": "apply",
                "idempotent": False,
                "job_id": job_id,
                "parent_topology_version": plan.parent_version,
                "published_topology_version": plan.target_version,
                **result,
            }

    # ---------------------------------------------------------------- resume
    def resume(self) -> dict[str, Any]:
        with self.store.writer(create=False) as connection:
            jobs = Store.pending_jobs(connection)
            if not jobs:
                return {
                    "command": "resume",
                    "jobs_completed": [],
                    "ok": True,
                    "published_topology_version": Store.published_version(connection),
                }
        result = self._process_all_pending()
        return {"command": "resume", "ok": True, **result}

    def _process_all_pending(self, connection: Any | None = None) -> dict[str, Any]:
        if connection is not None:
            return self._process_jobs_locked(connection)
        with self.store.writer(create=False) as locked:
            return self._process_jobs_locked(locked)

    def _process_jobs_locked(self, connection: Any) -> dict[str, Any]:
        completed: list[str] = []
        invalidations_done = 0
        jobs = Store.pending_jobs(connection)
        for job in jobs:
            job_id = str(job["job_id"])
            invalidations_done += self._process_job_invalidations(connection, job)
            with transaction(connection):
                self._finalize_job(connection, job)
            completed.append(job_id)
            failpoint("apply_after_final_commit")
        return {
            "completed_jobs": completed,
            "invalidations_processed": invalidations_done,
            "published_topology_version": Store.published_version(connection),
        }

    def _process_job_invalidations(self, connection: Any, job: Any) -> int:
        job_id = str(job["job_id"])
        target_version = int(job["target_version"])
        topology = Store.load_topology(connection, target_version)
        processed = 0
        rows = list(
            connection.execute(
                """
                SELECT * FROM invalidations
                WHERE job_id = ? AND status = 'PENDING'
                ORDER BY invalidation_id
                """,
                (job_id,),
            )
        )
        for invalidation in rows:
            with transaction(connection):
                current = connection.execute(
                    "SELECT * FROM invalidations WHERE invalidation_id = ?",
                    (int(invalidation["invalidation_id"]),),
                ).fetchone()
                if current is None or str(current["status"]) != "PENDING":
                    continue
                window = connection.execute(
                    "SELECT * FROM windows WHERE window_key = ?",
                    (str(current["window_key"]),),
                ).fetchone()
                if window is None:
                    raise WorkbenchError(
                        E_STATE_CORRUPT,
                        "invalidation references a missing window",
                        {"window_key": str(current["window_key"])},
                    )
                actions = json.loads(str(current["actions_json"]))
                if str(window["state"]) == "CLOSED":
                    self._rebuild_invalidated_window(
                        connection,
                        topology,
                        dict(window),
                        actions,
                        target_version,
                    )
                connection.execute(
                    "UPDATE invalidations SET status = 'DONE' WHERE invalidation_id = ?",
                    (int(current["invalidation_id"]),),
                )
            processed += 1
            failpoint(f"apply_after_invalidation_{processed}")
            failpoint("apply_after_invalidation")
        return processed

    def _rebuild_invalidated_window(
        self,
        connection: Any,
        topology: Topology,
        window: dict[str, Any],
        actions: dict[str, str],
        target_version: int,
    ) -> None:
        prior = connection.execute(
            """
            SELECT * FROM window_revisions
            WHERE revision_id = ?
            """,
            (int(window["current_revision_id"]),),
        ).fetchone()
        if prior is None:
            raise WorkbenchError(
                E_STATE_CORRUPT,
                "closed window has no current revision",
                {"window_key": window["window_key"]},
            )
        rebuild_result = actions.get("conclusion") == "REBUILD"
        if rebuild_result:
            rows = list(
                connection.execute(
                    """
                    SELECT e.*, we.membership
                    FROM window_events we
                    JOIN events e ON e.event_seq = we.event_seq
                    WHERE we.window_key = ?
                    ORDER BY e.occurred_clock_ms, e.seq_kind, e.seq_num, e.seq_text
                    """,
                    (window["window_key"],),
                )
            )
            late_keys = {
                str(row["event_seq"])
                for row in connection.execute(
                    "SELECT event_seq FROM late_ledger WHERE window_key = ?",
                    (window["window_key"],),
                )
            }
            result, evidence, revision_digest = build_window_revision(
                topology=topology,
                window=window,
                events=[event_to_dict(row) for row in rows],
                closed_at_watermark_ms=int(window["closed_at_watermark_ms"]),
                late_event_seqs=late_keys,
                include_late_closed=True,
            )
        else:
            try:
                result = json.loads(str(prior["result_json"]))
            except json.JSONDecodeError as exc:
                raise WorkbenchError(
                    E_STATE_CORRUPT,
                    "prior window result JSON is invalid",
                    {"window_key": window["window_key"]},
                ) from exc
            representative = result.get("representative_event")
            evidence = build_evidence_chain(topology, representative) if representative else []
            revision_digest = digest({"evidence_chain": evidence, "result": result})
        self._insert_revision(
            connection,
            window_key_value=str(window["window_key"]),
            topology_version=target_version,
            result=result,
            evidence=evidence,
            revision_digest=revision_digest,
            staged=True,
            reason=f"patch:{target_version}",
        )

    def _finalize_job(self, connection: Any, job: Any) -> None:
        job_id = str(job["job_id"])
        target_version = int(job["target_version"])
        published_version = Store.published_version(connection)
        parent_version = int(job["parent_version"])
        if published_version != parent_version:
            raise WorkbenchError(
                E_STATE_CORRUPT,
                "job parent does not match the published topology pointer",
                {
                    "job_id": job_id,
                    "job_parent_version": parent_version,
                    "published_topology_version": published_version,
                },
            )
        pending_count = int(
            connection.execute(
                """
                SELECT COUNT(*) AS count FROM invalidations
                WHERE job_id = ? AND status = 'PENDING'
                """,
                (job_id,),
            ).fetchone()["count"]
        )
        if pending_count:
            raise WorkbenchError(
                E_STATE_CORRUPT,
                "cannot finalize a job with pending invalidations",
                {"job_id": job_id, "pending_count": pending_count},
            )
        self._verify_invalidation_outputs(connection, job_id, target_version)
        connection.execute(
            """
            UPDATE windows
            SET current_revision_id = (
                SELECT wr.revision_id FROM window_revisions wr
                WHERE wr.window_key = windows.window_key
                  AND wr.topology_version = ? AND wr.staged = 1
            )
            WHERE window_key IN (
                SELECT i.window_key FROM invalidations i
                JOIN windows w ON w.window_key = i.window_key
                WHERE i.job_id = ? AND w.state = 'CLOSED'
            )
            """,
            (target_version, job_id),
        )
        connection.execute(
            "UPDATE window_revisions SET staged = 0 WHERE topology_version = ? AND staged = 1",
            (target_version,),
        )
        connection.execute(
            "UPDATE topologies SET published = CASE WHEN version = ? THEN 1 ELSE 0 END",
            (target_version,),
        )
        self._publish_pointer(connection, target_version)
        Store.set_state(
            connection,
            "published_effective_clock_ms",
            str(Store.load_topology(connection, target_version).effective_clock_ms),
        )
        connection.execute("UPDATE jobs SET status = 'DONE' WHERE job_id = ?", (job_id,))
        failpoint("apply_before_final_commit")

    def _verify_invalidation_outputs(self, connection: Any, job_id: str, target_version: int) -> None:
        rows = list(
            connection.execute(
                """
                SELECT i.window_key, w.state,
                       (SELECT COUNT(*) FROM window_events we
                        WHERE we.window_key = i.window_key) AS event_count
                FROM invalidations i
                JOIN windows w ON w.window_key = i.window_key
                WHERE i.job_id = ? AND w.state = 'CLOSED'
                """,
                (job_id,),
            )
        )
        for row in rows:
            revision = connection.execute(
                """
                SELECT revision_id FROM window_revisions
                WHERE window_key = ? AND topology_version = ? AND staged = 1
                """,
                (str(row["window_key"]), target_version),
            ).fetchone()
            if revision is None:
                raise WorkbenchError(
                    E_STATE_CORRUPT,
                    "closed invalidated window has no staged revision",
                    {"window_key": str(row["window_key"])},
                )

    def _publish_pointer(self, connection: Any, version: int) -> None:
        connection.execute(
            "CREATE TEMP TABLE IF NOT EXISTS publish_pointer_stage(version INTEGER NOT NULL)"
        )
        connection.execute("DELETE FROM publish_pointer_stage")
        connection.execute("INSERT INTO publish_pointer_stage(version) VALUES(?)", (version,))
        connection.execute(
            """
            UPDATE state
            SET value = (SELECT CAST(version AS TEXT) FROM publish_pointer_stage)
            WHERE key = 'published_topology_version'
            """
        )
        if connection.execute("SELECT changes() AS changed").fetchone()["changed"] == 0:
            Store.set_state(connection, "published_topology_version", str(version))

    # ------------------------------------------------------- topology staging
    def _stage_transition(
        self,
        connection: Any,
        current: Topology,
        target: Topology,
        *,
        kind: str,
        source_digest: str,
        payload_json: str,
        triggers: list[dict[str, Any]],
    ) -> str:
        if Store.pending_jobs(connection):
            raise WorkbenchError(E_RESUME_REQUIRED, "a previous topology job is still pending")
        max_seen = Store.max_seen_version(connection)
        if target.version <= current.version:
            raise WorkbenchError(
                E_TOPOLOGY_VERSION_ROLLBACK,
                "target topology version is not newer than the published version",
                {
                    "published_topology_version": current.version,
                    "target_topology_version": target.version,
                },
            )
        if target.version <= max_seen:
            raise WorkbenchError(
                E_TOPOLOGY_VERSION_CONFLICT,
                "target topology version was already staged",
                {
                    "max_seen_topology_version": max_seen,
                    "published_topology_version": current.version,
                    "target_topology_version": target.version,
                },
            )
        content_digest = target.content_digest()
        Store.store_topology(
            connection,
            target,
            parent_version=current.version,
            content_digest=content_digest,
            source_kind=kind,
            source_digest=source_digest,
            published=False,
        )
        job_id = f"{kind}:v{target.version}:{source_digest.split(':')[-1][:16]}"
        connection.execute(
            """
            INSERT INTO jobs(job_id, kind, parent_version, target_version, status, payload_json)
            VALUES(?, ?, ?, ?, 'PENDING', ?)
            """,
            (job_id, kind, current.version, target.version, payload_json),
        )
        Store.set_state(connection, "max_seen_topology_version", str(target.version))
        self._materialize_invalidations(connection, current, target, job_id, triggers)
        return job_id

    def _diff_triggers(self, current: Topology, target: Topology) -> list[dict[str, Any]]:
        triggers: list[dict[str, Any]] = []
        old_edges = current.dependency_pairs()
        new_edges = target.dependency_pairs()
        for service, upstream in sorted(new_edges - old_edges):
            triggers.append(
                {
                    "kind": "dependency_add",
                    "service_name": service,
                    "upstream_service": upstream,
                }
            )
        for service, upstream in sorted(old_edges - new_edges):
            triggers.append(
                {
                    "kind": "dependency_remove",
                    "service_name": service,
                    "upstream_service": upstream,
                }
            )
        for instance_id in sorted(set(current.instances) | set(target.instances)):
            before = current.instances.get(instance_id)
            after = target.instances.get(instance_id)
            if before is not None and after is not None and before.weight != after.weight:
                triggers.append(
                    {
                        "instance_id": instance_id,
                        "kind": "weight_change",
                        "new_weight": after.weight,
                        "old_weight": before.weight,
                    }
                )
        for silence_id in sorted(set(current.silences) | set(target.silences)):
            before = current.silences.get(silence_id)
            after = target.silences.get(silence_id)
            if before != after:
                if before is not None:
                    triggers.append(
                        {
                            "kind": "silence_remove",
                            "silence": before.to_canonical(),
                        }
                    )
                if after is not None:
                    triggers.append(
                        {
                            "kind": "silence_add",
                            "silence": after.to_canonical(),
                        }
                    )
        return triggers

    def _patch_triggers(self, plan: PatchPlan, current: Topology) -> list[dict[str, Any]]:
        triggers: list[dict[str, Any]] = []
        for service, upstream in plan.dependency_add:
            triggers.append(
                {
                    "kind": "dependency_add",
                    "service_name": service,
                    "upstream_service": upstream,
                }
            )
        for service, upstream in plan.dependency_remove:
            triggers.append(
                {
                    "kind": "dependency_remove",
                    "service_name": service,
                    "upstream_service": upstream,
                }
            )
        for instance_id, weight in plan.weight_changes:
            triggers.append({"instance_id": instance_id, "kind": "weight_change", "new_weight": weight})
        for silence in plan.silence_add:
            triggers.append({"kind": "silence_add", "silence": silence.to_canonical()})
        for silence_id in plan.silence_remove:
            triggers.append(
                {
                    "kind": "silence_remove",
                    "silence": current.silences[silence_id].to_canonical(),
                    "silence_id": silence_id,
                }
            )
        return triggers

    def _materialize_invalidations(
        self,
        connection: Any,
        current: Topology,
        target: Topology,
        job_id: str,
        triggers: list[dict[str, Any]],
    ) -> None:
        windows = list(connection.execute("SELECT * FROM windows ORDER BY window_key"))
        dependency_closures: dict[str, dict[str, tuple[str, ...]]] = {}
        for trigger in triggers:
            if trigger["kind"] in {"dependency_add", "dependency_remove"}:
                service = str(trigger["service_name"])
                dependency_closures[service] = target.reverse_closure([service])

        hits: dict[str, dict[str, Any]] = {}
        for window in windows:
            window_dict = dict(window)
            instance = target.instances.get(str(window["instance_id"]))
            if instance is None:
                raise WorkbenchError(
                    E_STATE_CORRUPT,
                    "window instance is missing from target topology",
                    {"instance_id": str(window["instance_id"])},
                )
            for trigger in triggers:
                kind = str(trigger["kind"])
                path: list[str] | None = None
                if kind in {"dependency_add", "dependency_remove"}:
                    closure = dependency_closures[str(trigger["service_name"])]
                    if instance.service_name in closure:
                        path = list(closure[instance.service_name])
                elif kind == "weight_change":
                    if instance.instance_id == trigger["instance_id"]:
                        path = [instance.instance_id]
                elif kind in {"silence_add", "silence_remove"}:
                    silence = self._silence_from_trigger(connection, current, trigger)
                    if silence is not None and self._window_touches_silence(
                        connection, window_dict, instance, silence
                    ):
                        path = [instance.instance_id]
                if path is None:
                    continue
                entry = hits.setdefault(
                    str(window["window_key"]),
                    {"paths": [], "triggers": [], "kinds": set()},
                )
                public_trigger = {
                    key: value
                    for key, value in trigger.items()
                    if key not in {"silence", "new_weight", "old_weight"}
                }
                if "silence" in trigger:
                    public_trigger["silence"] = trigger["silence"]
                entry["triggers"].append(public_trigger)
                entry["paths"].append(path)
                entry["kinds"].add(kind)

        for key in sorted(hits):
            hit = hits[key]
            window = next(item for item in windows if str(item["window_key"]) == key)
            unique_triggers = sorted(
                {canonical_json(item): item for item in hit["triggers"]}.values(),
                key=canonical_json,
            )
            unique_paths = sorted({canonical_json(item): item for item in hit["paths"]}.values(), key=canonical_json)
            dependency_hit = bool(
                hit["kinds"] & {"dependency_add", "dependency_remove"}
            )
            reaudit_hit = bool(hit["kinds"] & {"weight_change", "silence_add", "silence_remove"})
            if str(window["state"]) == "CLOSED":
                actions = {
                    "conclusion": "REBUILD" if reaudit_hit else "REUSE",
                    "evidence_chain": "REBUILD",
                    "late_ledger": "RECHECK" if reaudit_hit else "REUSE",
                }
                status = "PENDING"
            else:
                actions = {
                    "conclusion": "DEFER_TO_CLOSE",
                    "evidence_chain": "DEFER_TO_CLOSE",
                    "late_ledger": "DEFER_TO_CLOSE",
                }
                status = "DONE"
            connection.execute(
                """
                INSERT INTO invalidations(
                    job_id, patch_version, window_key, triggers_json,
                    propagation_paths_json, actions_json, status
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    int(target.version),
                    key,
                    canonical_json(unique_triggers),
                    canonical_json(unique_paths),
                    canonical_json(actions),
                    status,
                ),
            )

    def _silence_from_trigger(
        self,
        connection: Any,
        current: Topology,
        trigger: dict[str, Any],
    ) -> SilenceWindow | None:
        if str(trigger["kind"]) == "silence_add":
            return _silence_from_dict(trigger["silence"])
        silence_id = str(trigger["silence_id"])
        return current.silences.get(silence_id)

    def _window_touches_silence(
        self,
        connection: Any,
        window: dict[str, Any],
        instance: Instance,
        silence: SilenceWindow,
    ) -> bool:
        if silence.end_clock_ms < int(window["window_start_ms"]):
            return False
        if silence.start_clock_ms > int(window["window_end_ms"]):
            return False
        if silence.rule_name is not None and silence.rule_name != window["rule_name"]:
            return False
        if silence.instance_id is not None and silence.instance_id != instance.instance_id:
            return False
        if silence.service_name is not None and silence.service_name != instance.service_name:
            return False
        rows = connection.execute(
            """
            SELECT e.occurred_clock_ms FROM window_events we
            JOIN events e ON e.event_seq = we.event_seq
            WHERE we.window_key = ?
            """,
            (str(window["window_key"]),),
        )
        return any(
            silence.start_clock_ms <= int(row["occurred_clock_ms"]) <= silence.end_clock_ms
            for row in rows
        )

    # ---------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        if not self.store.path.exists():
            return {
                "command": "status",
                "current_complete": True,
                "initialized": False,
                "schema_version": SCHEMA_VERSION,
            }
        with self.store.reader() as connection:
            published_version = Store.published_version(connection)
            if published_version is None:
                return {
                    "command": "status",
                    "current_complete": True,
                    "initialized": False,
                    "schema_version": SCHEMA_VERSION,
                }
            topology = Store.load_topology(connection, published_version)
            pending = Store.pending_jobs(connection)
            window_rows = list(
                connection.execute(
                    "SELECT * FROM windows ORDER BY instance_id, rule_name, window_start_ms, window_key"
                )
            )
            windows: list[dict[str, Any]] = []
            for row in window_rows:
                window = self._status_window(connection, row, published_version)
                windows.append(window)
            late_ledger = [
                {
                    "event_seq": self._raw_seq(row),
                    "instance_id": str(row["instance_id"]),
                    "occurred_clock_ms": int(row["occurred_clock_ms"]),
                    "reason": str(row["reason"]),
                    "rule_name": str(row["rule_name"]),
                    "watermark_at_arrival": int(row["watermark_at_arrival"]),
                    "window_key": str(row["window_key"]),
                }
                for row in connection.execute(
                    """
                    SELECT l.*, e.seq_kind, e.seq_num, e.seq_text
                    FROM late_ledger l JOIN events e ON e.event_seq = l.event_seq
                    ORDER BY e.seq_kind, e.seq_num, e.seq_text
                    """
                )
            ]
            invalidations = []
            for row in connection.execute(
                """
                SELECT * FROM invalidations
                WHERE patch_version <= ?
                ORDER BY patch_version, window_key, invalidation_id
                """,
                (published_version,),
            ):
                invalidations.append(
                    {
                        "actions": json.loads(str(row["actions_json"])),
                        "job_id": str(row["job_id"]),
                        "patch_version": int(row["patch_version"]),
                        "propagation_paths": json.loads(str(row["propagation_paths_json"])),
                        "status": str(row["status"]),
                        "triggers": json.loads(str(row["triggers_json"])),
                        "window_key": str(row["window_key"]),
                    }
                )
            watermarks = [
                {
                    "instance_id": str(row["instance_id"]),
                    "rule_name": str(row["rule_name"]),
                    "watermark_clock_ms": int(row["watermark_clock_ms"]),
                }
                for row in connection.execute(
                    "SELECT * FROM watermarks ORDER BY instance_id, rule_name"
                )
            ]
            pending_jobs = [
                {
                    "job_id": str(row["job_id"]),
                    "kind": str(row["kind"]),
                    "parent_version": int(row["parent_version"]),
                    "status": str(row["status"]),
                    "target_version": int(row["target_version"]),
                }
                for row in pending
            ]
            return {
                "command": "status",
                "config": {
                    "allowed_lateness_ms": topology.allowed_lateness_ms,
                    "window_ms": topology.window_ms,
                },
                "current_complete": not pending_jobs,
                "initialized": True,
                "invalidations": invalidations,
                "late_ledger": late_ledger,
                "pending_jobs": pending_jobs,
                "schema_version": SCHEMA_VERSION,
                "topology": {
                    "content_digest": topology.content_digest(),
                    "effective_clock_ms": topology.effective_clock_ms,
                    "parent_version": topology.parent_version,
                    "published_topology_version": published_version,
                    "source_digest": topology.source_digest,
                    "source_kind": topology.source_kind,
                },
                "watermarks": watermarks,
                "windows": windows,
            }

    def _status_window(self, connection: Any, row: Any, published_version: int) -> dict[str, Any]:
        current_revision = None
        if row["current_revision_id"] is not None:
            current_revision = connection.execute(
                "SELECT * FROM window_revisions WHERE revision_id = ? AND staged = 0",
                (int(row["current_revision_id"]),),
            ).fetchone()
        history = []
        for revision in connection.execute(
            """
            SELECT * FROM window_revisions
            WHERE window_key = ? AND staged = 0 AND topology_version <= ?
            ORDER BY topology_version, revision_id
            """,
            (str(row["window_key"]), published_version),
        ):
            result, evidence = parse_revision_jsons(
                str(revision["result_json"]), str(revision["evidence_json"])
            )
            history.append(
                {
                    "digest": str(revision["digest"]),
                    "evidence_chain": evidence,
                    "reason": str(revision["reason"]),
                    "result": result,
                    "revision_id": int(revision["revision_id"]),
                    "topology_version": int(revision["topology_version"]),
                }
            )
        result = None
        evidence = None
        revision_id = None
        conclusion_version = None
        revision_digest = None
        if current_revision is not None:
            result, evidence = parse_revision_jsons(
                str(current_revision["result_json"]), str(current_revision["evidence_json"])
            )
            revision_id = int(current_revision["revision_id"])
            conclusion_version = int(current_revision["topology_version"])
            revision_digest = str(current_revision["digest"])
        late_count = int(
            connection.execute(
                "SELECT COUNT(*) AS count FROM late_ledger WHERE window_key = ?",
                (str(row["window_key"]),),
            ).fetchone()["count"]
        )
        return {
            "closed_at_watermark_ms": (
                int(row["closed_at_watermark_ms"])
                if row["closed_at_watermark_ms"] is not None
                else None
            ),
            "conclusion_topology_version": conclusion_version,
            "current_revision_digest": revision_digest,
            "current_revision_id": revision_id,
            "evidence_chain": evidence,
            "history": history,
            "instance_id": str(row["instance_id"]),
            "late_ledger_count": late_count,
            "result": result,
            "rule_name": str(row["rule_name"]),
            "state": str(row["state"]),
            "window_end_ms": int(row["window_end_ms"]),
            "window_key": str(row["window_key"]),
            "window_start_ms": int(row["window_start_ms"]),
        }

    @staticmethod
    def _raw_seq(row: Any) -> int | str:
        return int(row["seq_num"]) if int(row["seq_kind"]) == 0 else str(row["seq_text"])

    # ---------------------------------------------------------------- verify
    def verify(self) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        checks: list[str] = []
        if not self.store.path.exists():
            return {
                "checks": [],
                "command": "verify",
                "errors": [],
                "initialized": False,
                "ok": False,
            }
        with self.store.reader() as connection:
            published_version = Store.published_version(connection)
            if published_version is None:
                return {
                    "command": "verify",
                    "errors": [],
                    "initialized": False,
                    "ok": False,
                }
            try:
                topology = Store.load_topology(connection, published_version)
                expected_topology_digest = topology.content_digest()
                row = connection.execute(
                    "SELECT content_digest FROM topologies WHERE version = ?",
                    (published_version,),
                ).fetchone()
                if row is None or str(row["content_digest"]) != expected_topology_digest:
                    errors.append(
                        {
                            "code": E_STATE_CORRUPT,
                            "message": "published topology digest mismatch",
                            "topology_version": published_version,
                        }
                    )
                checks.append("topology_digest")
            except WorkbenchError as exc:
                errors.append(
                    {
                        "code": exc.code,
                        "details": exc.details,
                        "message": exc.message,
                    }
                )
                topology = None
            if topology is not None:
                self._verify_windows(connection, published_version, errors)
                checks.extend(
                    [
                        "window_revision_digests",
                        "window_result_recompute",
                        "watermark_recompute",
                        "late_ledger",
                    ]
                )
                self._verify_events(connection, errors)
                checks.append("event_membership")
                pending = Store.pending_jobs(connection)
                staged = int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM window_revisions WHERE staged = 1"
                    ).fetchone()["count"]
                )
                if not pending and staged:
                    errors.append(
                        {
                            "code": E_STATE_CORRUPT,
                            "message": "staged revisions remain without a pending job",
                            "staged_revision_count": staged,
                        }
                    )
                if pending:
                    errors.append(
                        {
                            "code": E_RESUME_REQUIRED,
                            "message": "a propagation job has not been finalized",
                            "pending_jobs": [str(item["job_id"]) for item in pending],
                        }
                    )
            ok = not errors
            return {
                "checks": checks,
                "command": "verify",
                "errors": errors,
                "ok": ok,
                "published_topology_version": published_version,
            }

    def _verify_windows(self, connection: Any, published_version: int, errors: list[dict[str, Any]]) -> None:
        for window_row in connection.execute("SELECT * FROM windows ORDER BY window_key"):
            window = dict(window_row)
            if window["state"] == "CLOSED" and window["current_revision_id"] is None:
                errors.append(
                    {
                        "code": E_STATE_CORRUPT,
                        "message": "closed window has no current revision",
                        "window_key": window["window_key"],
                    }
                )
                continue
            current_revision = None
            if window["current_revision_id"] is not None:
                current_revision = connection.execute(
                    "SELECT * FROM window_revisions WHERE revision_id = ?",
                    (int(window["current_revision_id"]),),
                ).fetchone()
            if window["state"] == "CLOSED" and (
                current_revision is None or int(current_revision["staged"]) != 0
            ):
                errors.append(
                    {
                        "code": E_STATE_CORRUPT,
                        "message": "current revision is missing or staged",
                        "window_key": window["window_key"],
                    }
                )
                continue
            revisions = list(
                connection.execute(
                    """
                    SELECT * FROM window_revisions
                    WHERE window_key = ? AND staged = 0 AND topology_version <= ?
                    ORDER BY topology_version, revision_id
                    """,
                    (window["window_key"], published_version),
                )
            )
            for revision in revisions:
                self._verify_revision(connection, window, revision, errors)
            if window["state"] == "CLOSED" and current_revision is not None:
                if not revisions or int(revisions[-1]["revision_id"]) != int(
                    current_revision["revision_id"]
                ):
                    errors.append(
                        {
                            "code": E_STATE_CORRUPT,
                            "message": "current revision is not the newest persisted revision",
                            "window_key": window["window_key"],
                        }
                    )
        for mark in connection.execute(
            "SELECT * FROM watermarks ORDER BY instance_id, rule_name"
        ):
            maximum = connection.execute(
                """
                SELECT MAX(e.occurred_clock_ms) AS maximum
                FROM window_events we JOIN events e ON e.event_seq = we.event_seq
                WHERE e.instance_id = ? AND e.rule_name = ? AND we.membership = 'ON_TIME'
                """,
                (str(mark["instance_id"]), str(mark["rule_name"])),
            ).fetchone()["maximum"]
            if maximum is None or int(maximum) != int(mark["watermark_clock_ms"]):
                errors.append(
                    {
                        "code": E_STATE_CORRUPT,
                        "expected_watermark": int(maximum) if maximum is not None else None,
                        "instance_id": str(mark["instance_id"]),
                        "message": "watermark does not match accepted event history",
                        "rule_name": str(mark["rule_name"]),
                        "stored_watermark": int(mark["watermark_clock_ms"]),
                    }
                )

    def _verify_revision(
        self,
        connection: Any,
        window: dict[str, Any],
        revision: Any,
        errors: list[dict[str, Any]],
    ) -> None:
        try:
            result = json.loads(str(revision["result_json"]))
            evidence = json.loads(str(revision["evidence_json"]))
        except json.JSONDecodeError:
            errors.append(
                {
                    "code": E_STATE_CORRUPT,
                    "message": "window revision JSON is invalid",
                    "topology_version": int(revision["topology_version"]),
                    "window_key": window["window_key"],
                }
            )
            return
        computed_digest = digest({"evidence_chain": evidence, "result": result})
        if computed_digest != str(revision["digest"]):
            errors.append(
                {
                    "code": E_WINDOW_DIGEST_MISMATCH,
                    "computed_digest": computed_digest,
                    "message": "window revision digest does not match its content",
                    "stored_digest": str(revision["digest"]),
                    "topology_version": int(revision["topology_version"]),
                    "window_key": window["window_key"],
                }
            )
            return
        try:
            revision_topology = Store.load_topology(
                connection, int(revision["topology_version"])
            )
        except WorkbenchError as exc:
            errors.append(
                {
                    "code": E_STATE_CORRUPT,
                    "details": exc.details,
                    "message": "window revision topology is missing",
                    "window_key": window["window_key"],
                }
            )
            return
        rows = list(
            connection.execute(
                """
                SELECT e.*, we.membership FROM window_events we
                JOIN events e ON e.event_seq = we.event_seq
                WHERE we.window_key = ?
                ORDER BY e.occurred_clock_ms, e.seq_kind, e.seq_num, e.seq_text
                """,
                (window["window_key"],),
            )
        )
        if result.get("input_mode") == "reaudit_with_late_ledger":
            included_late = {
                ("i", int(value)) if isinstance(value, int) else ("s", str(value))
                for value in result.get("reevaluated_late_event_seqs", [])
            }
            rows = [
                row
                for row in rows
                if str(row["membership"]) != "LATE_CLOSED"
                or (
                    ("i", int(row["seq_num"]))
                    if int(row["seq_kind"]) == 0
                    else ("s", str(row["seq_text"]))
                )
                in included_late
            ]
        late_keys = {
            str(item["event_seq"])
            for item in connection.execute(
                "SELECT event_seq FROM late_ledger WHERE window_key = ?",
                (window["window_key"],),
            )
        }
        closed_at = result.get("closed_at_watermark_ms")
        if not isinstance(closed_at, int):
            errors.append(
                {
                    "code": E_STATE_CORRUPT,
                    "message": "window result has no integer closed watermark",
                    "window_key": window["window_key"],
                }
            )
            return
        recomputed_result, recomputed_evidence, _ = build_window_revision(
            topology=revision_topology,
            window=window,
            events=[event_to_dict(item) for item in rows],
            closed_at_watermark_ms=closed_at,
            late_event_seqs=late_keys,
            include_late_closed=result.get("input_mode") == "reaudit_with_late_ledger",
        )
        if canonical_json(recomputed_result) != canonical_json(result):
            errors.append(
                {
                    "code": E_STATE_CORRUPT,
                    "message": "window result does not recompute identically",
                    "topology_version": int(revision["topology_version"]),
                    "window_key": window["window_key"],
                }
            )
        if canonical_json(recomputed_evidence) != canonical_json(evidence):
            errors.append(
                {
                    "code": E_STATE_CORRUPT,
                    "message": "window evidence does not recompute identically",
                    "topology_version": int(revision["topology_version"]),
                    "window_key": window["window_key"],
                }
            )

    def _verify_events(self, connection: Any, errors: list[dict[str, Any]]) -> None:
        for ledger in connection.execute(
            """
            SELECT l.*, e.seq_kind, e.seq_num, e.seq_text, e.occurred_clock_ms AS event_occurred
            FROM late_ledger l JOIN events e ON e.event_seq = l.event_seq
            ORDER BY e.seq_kind, e.seq_num, e.seq_text
            """
        ):
            if int(ledger["event_occurred"]) >= int(ledger["watermark_at_arrival"]):
                errors.append(
                    {
                        "code": E_STATE_CORRUPT,
                        "event_seq": self._raw_seq(ledger),
                        "message": "late ledger entry was not behind its watermark",
                    }
                )
            membership = connection.execute(
                """
                SELECT membership, window_key FROM window_events
                WHERE event_seq = ?
                """,
                (str(ledger["event_seq"]),),
            ).fetchone()
            if membership is None or str(membership["window_key"]) != str(ledger["window_key"]):
                errors.append(
                    {
                        "code": E_STATE_CORRUPT,
                        "event_seq": self._raw_seq(ledger),
                        "message": "late ledger entry has inconsistent window membership",
                    }
                )
        for row in connection.execute(
            """
            SELECT we.window_key, we.event_seq, e.instance_id, e.rule_name,
                   e.occurred_clock_ms, e.seq_kind, e.seq_num, e.seq_text,
                   w.window_start_ms, w.window_end_ms
            FROM window_events we
            JOIN events e ON e.event_seq = we.event_seq
            JOIN windows w ON w.window_key = we.window_key
            """
        ):
            if not (
                int(row["window_start_ms"])
                <= int(row["occurred_clock_ms"])
                <= int(row["window_end_ms"])
            ):
                errors.append(
                    {
                        "code": E_STATE_CORRUPT,
                        "event_seq": self._raw_seq(row),
                        "message": "event membership falls outside its window",
                    }
                )


def _silence_from_dict(value: dict[str, Any]) -> SilenceWindow:
    return SilenceWindow(
        silence_id=str(value["silence_id"]),
        instance_id=value.get("instance_id"),
        service_name=value.get("service_name"),
        rule_name=value.get("rule_name"),
        start_clock_ms=int(value["start_clock_ms"]),
        end_clock_ms=int(value["end_clock_ms"]),
        kind=str(value.get("kind", "silence")),
    )
