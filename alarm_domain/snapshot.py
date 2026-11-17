"""SQLite snapshot persistence, whole-state replacement and replay checks."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .clustering import cluster_events
from .errors import DomainError
from .models import AlarmEvent, Edge, Rule, Topology
from .rootcause import analyze_clusters
from .session import (
    AlarmSession,
    _validate_edge_object,
    _validate_rule_object,
    first_analysis_difference,
    rules_version,
    topology_version,
)
from .util import canonical_json, display_file, is_finite_number, is_int, sha256_json


@dataclass(frozen=True)
class SnapshotInfo:
    snapshot_id: str
    sequence: int
    rules_version: str | None
    topology_version: str | None
    event_cursor: int
    label: str
    read_only: bool
    is_current: bool
    missing_versions: bool
    payload_sha: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "sequence": self.sequence,
            "rules_version": self.rules_version,
            "topology_version": self.topology_version,
            "event_cursor": self.event_cursor,
            "label": self.label,
            "read_only": self.read_only,
            "is_current": self.is_current,
            "missing_versions": self.missing_versions,
            "payload_sha": self.payload_sha,
        }


class SnapshotStore:
    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.connection = sqlite3.connect(str(self.path))
            self.connection.row_factory = sqlite3.Row
            self._initialize()
        except sqlite3.Error as exc:
            raise DomainError(
                "E_SNAP",
                f"cannot open snapshot database: {exc}",
                file=display_file(self.path),
            ) from exc

    def _initialize(self) -> None:
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in (0, self.SCHEMA_VERSION):
            raise DomainError(
                "E_SNAP",
                "unsupported snapshot schema version",
                file=display_file(self.path),
                detail={"found": version, "supported": self.SCHEMA_VERSION},
            )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id TEXT PRIMARY KEY,
                sequence INTEGER NOT NULL UNIQUE,
                rules_version TEXT,
                topology_version TEXT,
                event_cursor INTEGER NOT NULL,
                payload TEXT NOT NULL,
                payload_sha TEXT NOT NULL,
                label TEXT NOT NULL,
                read_only INTEGER NOT NULL,
                is_current INTEGER NOT NULL,
                missing_versions INTEGER NOT NULL
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_sequence ON snapshots(sequence)"
        )
        self.connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
        self.connection.commit()

    def __enter__(self) -> "SnapshotStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def save(
        self,
        payload: Mapping[str, Any],
        *,
        label: str = "",
        omit_versions: bool = False,
    ) -> SnapshotInfo:
        document = dict(payload)
        if document.get("schema") != "alarm-session-snapshot-v1":
            raise DomainError(
                "E_SNAP",
                "unsupported snapshot payload schema",
                file=display_file(self.path),
                detail={"schema": document.get("schema")},
            )
        cursor = document.get("event_cursor")
        if not is_int(cursor) or cursor < 0:
            raise DomainError(
                "E_SNAP",
                "snapshot event_cursor must be a non-negative integer",
                file=display_file(self.path),
            )
        if omit_versions:
            document["rules_version"] = None
            document["topology_version"] = None
            stored_rules_version = None
            stored_topology_version = None
        else:
            stored_rules_version = document.get("rules_version")
            stored_topology_version = document.get("topology_version")
            if not isinstance(stored_rules_version, str) or not isinstance(
                stored_topology_version, str
            ):
                raise DomainError(
                    "E_SNAP",
                    "snapshot payload is missing required versions",
                    file=display_file(self.path),
                )
        encoded = canonical_json(document)
        payload_sha = sha256_json(document)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            next_sequence = int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM snapshots"
                ).fetchone()[0]
            )
            snapshot_id = f"snap-{next_sequence:04d}-{payload_sha[:12]}"
            self.connection.execute(
                "UPDATE snapshots SET is_current = 0, read_only = 1"
            )
            self.connection.execute(
                """
                INSERT INTO snapshots(
                    snapshot_id, sequence, rules_version, topology_version,
                    event_cursor, payload, payload_sha, label, read_only,
                    is_current, missing_versions
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?)
                """,
                (
                    snapshot_id,
                    next_sequence,
                    stored_rules_version,
                    stored_topology_version,
                    cursor,
                    encoded,
                    payload_sha,
                    label,
                    1 if omit_versions else 0,
                ),
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise DomainError(
                "E_SNAP",
                f"cannot save snapshot: {exc}",
                file=display_file(self.path),
            ) from exc
        return self.get_info(snapshot_id)

    def _row_to_info(self, row: sqlite3.Row) -> SnapshotInfo:
        return SnapshotInfo(
            snapshot_id=str(row["snapshot_id"]),
            sequence=int(row["sequence"]),
            rules_version=row["rules_version"],
            topology_version=row["topology_version"],
            event_cursor=int(row["event_cursor"]),
            label=str(row["label"]),
            read_only=bool(row["read_only"]),
            is_current=bool(row["is_current"]),
            missing_versions=bool(row["missing_versions"]),
            payload_sha=str(row["payload_sha"]),
        )

    def get_info(self, snapshot_id: str) -> SnapshotInfo:
        row = self.connection.execute(
            "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise DomainError(
                "E_SNAP",
                f"snapshot {snapshot_id} does not exist",
                file=display_file(self.path),
                detail={"snapshot_id": snapshot_id},
            )
        return self._row_to_info(row)

    def list_snapshots(self) -> list[SnapshotInfo]:
        rows = self.connection.execute(
            "SELECT * FROM snapshots ORDER BY sequence ASC"
        ).fetchall()
        return [self._row_to_info(row) for row in rows]

    def load(
        self,
        snapshot_id: str | None = None,
        *,
        require_versions: bool = True,
    ) -> tuple[SnapshotInfo, dict[str, Any]]:
        if snapshot_id is None:
            row = self.connection.execute(
                "SELECT * FROM snapshots ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise DomainError(
                "E_SNAP",
                "snapshot does not exist",
                file=display_file(self.path),
                detail={"snapshot_id": snapshot_id},
            )
        info = self._row_to_info(row)
        missing_fields: list[str] = []
        if info.rules_version is None:
            missing_fields.append("rules_version")
        if info.topology_version is None:
            missing_fields.append("topology_version")
        if require_versions and missing_fields:
            raise DomainError(
                "E_SNAP",
                "snapshot is missing required version metadata",
                file=display_file(self.path),
                detail={"snapshot_id": info.snapshot_id, "missing_fields": missing_fields},
            )
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise DomainError(
                "E_SNAP",
                f"snapshot payload is invalid JSON: {exc}",
                file=display_file(self.path),
                detail={"snapshot_id": info.snapshot_id},
            ) from exc
        if not isinstance(payload, dict):
            raise DomainError(
                "E_SNAP",
                "snapshot payload must be an object",
                file=display_file(self.path),
                detail={"snapshot_id": info.snapshot_id},
            )
        if sha256_json(payload) != info.payload_sha:
            raise DomainError(
                "E_SNAP",
                "snapshot payload digest mismatch",
                file=display_file(self.path),
                detail={"snapshot_id": info.snapshot_id},
            )
        return info, payload

    def mark_current(self, snapshot_id: str) -> None:
        try:
            self.connection.execute(
                "UPDATE snapshots SET is_current = CASE WHEN snapshot_id = ? THEN 1 ELSE 0 END, "
                "read_only = 1",
                (snapshot_id,),
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise DomainError(
                "E_SNAP",
                f"cannot mark current snapshot: {exc}",
                file=display_file(self.path),
            ) from exc


def version_diff(
    current: Mapping[str, str | None],
    snapshot: Mapping[str, str | None],
) -> dict[str, Any]:
    per_dimension: dict[str, dict[str, Any]] = {}
    for name in ("rules", "topology"):
        current_value = current.get(name)
        snapshot_value = snapshot.get(name)
        per_dimension[name] = {
            "current": current_value,
            "snapshot": snapshot_value,
            "changed": current_value != snapshot_value,
        }
    return {
        "changed_any": any(item["changed"] for item in per_dimension.values()),
        "dimensions": per_dimension,
    }


def _parse_snapshot_payload(
    info: SnapshotInfo,
    payload: Mapping[str, Any],
) -> tuple[
    tuple[Rule, ...],
    Topology,
    tuple[AlarmEvent, ...],
    int,
    list[dict[str, Any]],
    dict[str, Any] | None,
    int,
]:
    if payload.get("schema") != "alarm-session-snapshot-v1":
        raise DomainError(
            "E_REPLAY",
            "snapshot payload schema prevents replay",
            detail={"snapshot_id": info.snapshot_id, "schema": payload.get("schema")},
        )
    raw_rules = payload.get("rules")
    raw_topology = payload.get("topology")
    raw_events = payload.get("events")
    raw_clusters = payload.get("clusters")
    raw_trace = payload.get("invalidation_trace", [])
    cursor = payload.get("event_cursor")
    if not isinstance(raw_rules, list) or not isinstance(raw_events, list):
        raise DomainError(
            "E_REPLAY",
            "snapshot rules or events collection is missing",
            detail={"snapshot_id": info.snapshot_id},
        )
    if not isinstance(raw_topology, dict) or not isinstance(raw_clusters, list):
        raise DomainError(
            "E_REPLAY",
            "snapshot topology or clusters collection is missing",
            detail={"snapshot_id": info.snapshot_id},
        )
    if not is_int(cursor) or cursor < 0:
        raise DomainError(
            "E_REPLAY",
            "snapshot event cursor is invalid",
            detail={"snapshot_id": info.snapshot_id, "event_cursor": cursor},
        )
    if cursor > len(raw_events):
        raise DomainError(
            "E_REPLAY",
            "snapshot event cursor is ahead of the retained event stream",
            detail={
                "snapshot_id": info.snapshot_id,
                "event_cursor": cursor,
                "event_count": len(raw_events),
            },
        )
    if cursor != info.event_cursor:
        raise DomainError(
            "E_REPLAY",
            "snapshot event cursor disagrees with recorded metadata",
            detail={
                "snapshot_id": info.snapshot_id,
                "payload_event_cursor": cursor,
                "metadata_event_cursor": info.event_cursor,
            },
        )
    if not isinstance(raw_trace, list):
        raise DomainError(
            "E_REPLAY",
            "snapshot invalidation trace is invalid",
            detail={"snapshot_id": info.snapshot_id},
        )

    rules: list[Rule] = []
    seen_rule_ids: set[str] = set()
    for raw_rule in raw_rules:
        rule = _validate_rule_object(raw_rule, source_file="<snapshot>", line=1)
        if rule.rule_id in seen_rule_ids:
            raise DomainError(
                "E_REPLAY",
                "snapshot contains duplicate rule ids",
                detail={"snapshot_id": info.snapshot_id, "rule_id": rule.rule_id},
            )
        seen_rule_ids.add(rule.rule_id)
        rules.append(rule)

    raw_services = raw_topology.get("services")
    raw_edges = raw_topology.get("edges")
    if not isinstance(raw_services, list) or not isinstance(raw_edges, list):
        raise DomainError(
            "E_REPLAY",
            "snapshot topology cannot be reconstructed",
            detail={"snapshot_id": info.snapshot_id},
        )
    services: list[str] = []
    for service in raw_services:
        if not isinstance(service, str) or not service:
            raise DomainError(
                "E_REPLAY",
                "snapshot topology service is invalid",
                detail={"snapshot_id": info.snapshot_id},
            )
        services.append(service)
    if len(set(services)) != len(services):
        raise DomainError(
            "E_REPLAY",
            "snapshot topology contains duplicate services",
            detail={"snapshot_id": info.snapshot_id},
        )
    edges: list[Edge] = []
    for raw_edge in raw_edges:
        edge = _validate_edge_object(raw_edge, source_file="<snapshot>", line=1)
        edges.append(edge)
    if len({(edge.upstream, edge.downstream) for edge in edges}) != len(edges):
        raise DomainError(
            "E_REPLAY",
            "snapshot topology contains duplicate edges",
            detail={"snapshot_id": info.snapshot_id},
        )
    topology = Topology(
        services=tuple(sorted(services)),
        edges=tuple(
            sorted(edges, key=lambda edge: (edge.upstream, edge.downstream))
        ),
    )
    if set(topology.services) != {rule.service for rule in rules}:
        raise DomainError(
            "E_REPLAY",
            "snapshot rule and topology service sets differ",
            detail={"snapshot_id": info.snapshot_id},
        )
    for edge in topology.edges:
        if edge.upstream not in topology.services or edge.downstream not in topology.services:
            raise DomainError(
                "E_REPLAY",
                "snapshot edge references an unknown service",
                detail={"snapshot_id": info.snapshot_id, "edge": edge.to_dict()},
            )

    events: list[AlarmEvent] = []
    seen_event_ids: set[str] = set()
    for raw_event in raw_events:
        if not isinstance(raw_event, dict):
            raise DomainError(
                "E_REPLAY",
                "snapshot event is invalid",
                detail={"snapshot_id": info.snapshot_id},
            )
        event_id = raw_event.get("event_id")
        rule_id = raw_event.get("rule_id")
        service = raw_event.get("service")
        timestamp = raw_event.get("timestamp")
        value = raw_event.get("value")
        if (
            not isinstance(event_id, str)
            or not isinstance(rule_id, str)
            or not isinstance(service, str)
            or not is_int(timestamp)
            or timestamp < 0
            or not is_finite_number(value)
        ):
            raise DomainError(
                "E_REPLAY",
                "snapshot event shape is invalid",
                detail={"snapshot_id": info.snapshot_id},
            )
        if event_id in seen_event_ids:
            raise DomainError(
                "E_REPLAY",
                "snapshot contains duplicate event ids",
                detail={"snapshot_id": info.snapshot_id, "event_id": event_id},
            )
        seen_event_ids.add(event_id)
        events.append(
            AlarmEvent(
                event_id=event_id,
                rule_id=rule_id,
                service=service,
                timestamp=timestamp,
                value=float(value),
            )
        )
    events.sort(key=lambda event: (event.timestamp, event.event_id))
    last_increment = payload.get("last_increment")
    if last_increment is not None and not isinstance(last_increment, dict):
        raise DomainError(
            "E_REPLAY",
            "snapshot last_increment is invalid",
            detail={"snapshot_id": info.snapshot_id},
        )
    return (
        tuple(rules),
        topology,
        tuple(events),
        cursor,
        [dict(item) for item in raw_trace if isinstance(item, dict)],
        dict(last_increment) if isinstance(last_increment, dict) else None,
        len(raw_clusters),
    )


def _replay_payload(
    info: SnapshotInfo,
    payload: Mapping[str, Any],
    *,
    require_payload_versions: bool = True,
) -> tuple[AlarmSession, dict[str, Any]]:
    missing: list[str] = []
    if not isinstance(payload.get("rules_version"), str):
        missing.append("payload.rules_version")
    if not isinstance(payload.get("topology_version"), str):
        missing.append("payload.topology_version")
    if require_payload_versions and missing:
        raise DomainError(
            "E_REPLAY",
            "snapshot version metadata is missing; replay basis is unavailable",
            detail={"snapshot_id": info.snapshot_id, "missing_fields": missing},
        )
    (
        rules,
        topology,
        events,
        cursor,
        trace,
        last_increment,
        saved_cluster_count,
    ) = _parse_snapshot_payload(info, payload)
    computed_rules_version = rules_version(rules)
    computed_topology_version = topology_version(topology)
    if computed_rules_version != payload.get("rules_version"):
        raise DomainError(
            "E_REPLAY",
            "snapshot rules do not match the recorded rules version",
            detail={
                "snapshot_id": info.snapshot_id,
                "recorded": payload.get("rules_version"),
                "computed": computed_rules_version,
            },
        )
    if computed_topology_version != payload.get("topology_version"):
        raise DomainError(
            "E_REPLAY",
            "snapshot topology does not match the recorded topology version",
            detail={
                "snapshot_id": info.snapshot_id,
                "recorded": payload.get("topology_version"),
                "computed": computed_topology_version,
            },
        )
    if info.rules_version is not None and computed_rules_version != info.rules_version:
        raise DomainError(
            "E_REPLAY",
            "snapshot row rules version disagrees with payload",
            detail={"snapshot_id": info.snapshot_id},
        )
    if info.topology_version is not None and computed_topology_version != info.topology_version:
        raise DomainError(
            "E_REPLAY",
            "snapshot row topology version disagrees with payload",
            detail={"snapshot_id": info.snapshot_id},
        )

    window_ms = payload.get("window_ms")
    if not is_int(window_ms) or window_ms <= 0:
        raise DomainError(
            "E_REPLAY",
            "snapshot window_ms is invalid",
            detail={"snapshot_id": info.snapshot_id, "window_ms": window_ms},
        )
    rules_by_id = {rule.rule_id: rule for rule in rules}
    active_events = tuple(
        event for event in events[:cursor] if event.rule_id in rules_by_id
    )
    clusters = cluster_events(active_events, topology, window_ms)
    replayed = analyze_clusters(clusters, active_events, rules_by_id, topology)
    saved = payload.get("clusters")
    if not isinstance(saved, list):
        raise DomainError(
            "E_REPLAY",
            "snapshot clusters collection is invalid",
            detail={"snapshot_id": info.snapshot_id},
        )
    saved_ids = [
        item.get("cluster_id") for item in saved if isinstance(item, dict)
    ]
    if len(saved_ids) != len(saved) or len(set(saved_ids)) != len(saved_ids):
        raise DomainError(
            "E_REPLAY",
            "snapshot cluster collection contains invalid or duplicate ids",
            detail={"snapshot_id": info.snapshot_id},
        )
    difference = first_analysis_difference(saved, replayed)
    if difference is not None:
        raise DomainError(
            "E_REPLAY",
            "snapshot conclusions are not reproducible from the recorded cursor and versions",
            detail={
                "snapshot_id": info.snapshot_id,
                "first_difference_cluster_id": difference,
                "event_cursor": cursor,
            },
        )
    session = AlarmSession.from_components(
        rules,
        topology,
        events,
        window_ms=window_ms,
        event_cursor=cursor,
        invalidation_trace=trace,
        last_increment=last_increment,
    )
    report = {
        "snapshot_id": info.snapshot_id,
        "rules_version": computed_rules_version,
        "topology_version": computed_topology_version,
        "event_cursor": cursor,
        "events_replayed": cursor,
        "clusters_compared": saved_cluster_count,
        "match": True,
        "first_difference_cluster_id": None,
    }
    return session, report


def replay_snapshot(store: SnapshotStore, snapshot_id: str | None = None) -> dict[str, Any]:
    info, payload = store.load(snapshot_id, require_versions=False)
    _, report = _replay_payload(info, payload)
    return report


def save_snapshot(
    session: AlarmSession,
    store: SnapshotStore,
    *,
    label: str = "",
    omit_versions: bool = False,
) -> SnapshotInfo:
    return store.save(
        session.snapshot_payload(), label=label, omit_versions=omit_versions
    )


def restore_snapshot(
    store: SnapshotStore,
    snapshot_id: str | None = None,
    *,
    current_session: AlarmSession | None = None,
) -> tuple[AlarmSession, dict[str, Any]]:
    info, payload = store.load(snapshot_id, require_versions=True)
    restored, replay_report = _replay_payload(info, payload)
    if current_session is None:
        current_versions = {"rules": None, "topology": None}
    else:
        current_versions = {
            "rules": current_session.rules_version,
            "topology": current_session.topology_version,
        }
    snapshot_versions = {
        "rules": info.rules_version,
        "topology": info.topology_version,
    }
    diff = version_diff(current_versions, snapshot_versions)
    store.mark_current(info.snapshot_id)
    current_info = store.get_info(info.snapshot_id)
    report = {
        "snapshot": current_info.to_dict(),
        "snapshot_versions": snapshot_versions,
        "current_versions_before_restore": current_versions,
        "version_diff": diff,
        "whole_state_replaced": bool(current_session is not None),
        "incremental_merge_used": False,
        "replay": replay_report,
    }
    return restored, report
