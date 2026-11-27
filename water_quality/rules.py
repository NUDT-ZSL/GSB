"""Partition rules and persistent partition assignments."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .db import canonical_json, metadata_get, metadata_set


_SAFE_PART = re.compile(r"[^A-Za-z0-9_.-]+")


def _token(value: Any) -> str:
    if value is None or value == "":
        return "__null__"
    return _SAFE_PART.sub("_", str(value)).strip("_") or "__empty__"


@dataclass(frozen=True)
class PartitionRules:
    """Versioned, deterministic partitioning for pipe and point sides.

    Strategies are intentionally small and pure so a rule change can be diffed
    without consulting external state.
    """

    version: str = "partition-v1"
    pipe_strategy: str = "region"
    point_strategy: str = "location_zone"
    pipe_hash_buckets: int = 2
    point_hash_buckets: int = 2

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("partition rule version must not be empty")
        if self.pipe_strategy not in {"region", "hash"}:
            raise ValueError(f"unsupported pipe strategy: {self.pipe_strategy}")
        if self.point_strategy not in {"location_zone", "hash"}:
            raise ValueError(f"unsupported point strategy: {self.point_strategy}")
        if self.pipe_hash_buckets < 1 or self.point_hash_buckets < 1:
            raise ValueError("hash bucket counts must be positive")

    def pipe_partition(self, row: Mapping[str, Any]) -> str:
        if self.pipe_strategy == "region":
            return f"pipe:region:{_token(row.get('region'))}"
        raw = row.get("pipe_id")
        if raw is None:
            raw = f"row:{row.get('row_id', '')}"
        digest = hashlib.sha256(str(raw).encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) % self.pipe_hash_buckets
        return f"pipe:hash{self.pipe_hash_buckets}:{bucket}"

    def point_partition(self, row: Mapping[str, Any]) -> str:
        if self.point_strategy == "location_zone":
            location = str(row.get("location") or "")
            zone = location.split("-", 1)[0] if location else None
            return f"point:zone:{_token(zone)}"
        raw = row.get("point_id")
        if raw is None:
            raw = f"row:{row.get('row_id', '')}"
        digest = hashlib.sha256(str(raw).encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) % self.point_hash_buckets
        return f"point:hash{self.point_hash_buckets}:{bucket}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PartitionRules":
        allowed = {
            "version",
            "pipe_strategy",
            "point_strategy",
            "pipe_hash_buckets",
            "point_hash_buckets",
        }
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown partition rule fields: {sorted(unknown)}")
        return cls(**dict(data))

    @classmethod
    def from_json(cls, text: str) -> "PartitionRules":
        return cls.from_dict(json.loads(text))


DEFAULT_RULES = PartitionRules()


def activate_partition_rules(conn: sqlite3.Connection, rules: PartitionRules) -> None:
    """Persist rules and reassign every pipe and monitoring point."""

    now = datetime.now(timezone.utc).isoformat()
    rules_json = canonical_json(rules.to_dict())
    existing = conn.execute(
        "SELECT rules_json FROM partition_rules WHERE version = ?", (rules.version,)
    ).fetchone()
    if existing is not None and existing["rules_json"] != rules_json:
        raise ValueError(
            f"partition rule version {rules.version!r} already exists with "
            "different rules; assign a new version"
        )
    conn.execute(
        "INSERT OR IGNORE INTO partition_rules(version, rules_json, created_at) "
        "VALUES (?, ?, ?)",
        (rules.version, rules_json, now),
    )
    metadata_set(conn, "active_partition_rules_version", rules.version)
    metadata_set(conn, "active_partition_rules_json", rules_json)

    conn.execute("DELETE FROM partition_assignments")
    pipes = conn.execute(
        """
        SELECT row_id, pipe_id, upstream_node, downstream_node, region,
               diameter_mm, material
        FROM pipes
        ORDER BY row_id
        """
    ).fetchall()
    for row in pipes:
        data = dict(row)
        partition_id = rules.pipe_partition(data)
        conn.execute(
            "UPDATE pipes SET partition_id = ? WHERE row_id = ?",
            (partition_id, data["row_id"]),
        )
        conn.execute(
            """
            INSERT INTO partition_assignments(
                entity_type, entity_key, entity_id, partition_id, rule_version
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "pipe",
                f"pipe-row:{data['row_id']}",
                data.get("pipe_id"),
                partition_id,
                rules.version,
            ),
        )

    points = conn.execute(
        """
        SELECT point_id, pipe_id, location, metric_types
        FROM monitoring_points
        ORDER BY point_id
        """
    ).fetchall()
    for row in points:
        data = dict(row)
        partition_id = rules.point_partition(data)
        conn.execute(
            "UPDATE monitoring_points SET partition_id = ? WHERE point_id = ?",
            (partition_id, data["point_id"]),
        )
        conn.execute(
            """
            INSERT INTO partition_assignments(
                entity_type, entity_key, entity_id, partition_id, rule_version
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "point",
                f"point:{data['point_id']}",
                data["point_id"],
                partition_id,
                rules.version,
            ),
        )
    conn.commit()


def get_active_rules(conn: sqlite3.Connection) -> PartitionRules:
    version = metadata_get(conn, "active_partition_rules_version")
    text = metadata_get(conn, "active_partition_rules_json")
    if version is None or text is None:
        raise RuntimeError("partition rules are not initialized")
    rules = PartitionRules.from_json(text)
    if rules.version != version:
        raise RuntimeError(
            f"active partition rule metadata is inconsistent: {version} != {rules.version}"
        )
    return rules


def current_assignments(conn: sqlite3.Connection, entity_type: str) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT entity_key, partition_id
        FROM partition_assignments
        WHERE entity_type = ?
        """,
        (entity_type,),
    ).fetchall()
    return {row["entity_key"]: row["partition_id"] for row in rows}

