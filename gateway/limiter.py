from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .config import BucketPolicy, GatewayConfig, Route

TOKEN_UNITS = 1000


@dataclass
class BucketEvaluation:
    kind: str
    key: str
    capacity_units: int
    refill_per_second: int
    tokens_before_units: int
    refill_units: int
    tokens_after_refill_units: int
    tokens_after_units: int
    last_refill_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "capacity_units": self.capacity_units,
            "refill_per_second": self.refill_per_second,
            "tokens_before_units": self.tokens_before_units,
            "refill_units": self.refill_units,
            "tokens_after_refill_units": self.tokens_after_refill_units,
            "tokens_after_units": self.tokens_after_units,
            "last_refill_ms": self.last_refill_ms,
        }


def _refill_bucket(
    conn: sqlite3.Connection,
    *,
    config_version: int,
    kind: str,
    key: str,
    policy: BucketPolicy,
    logical_time_ms: int,
) -> BucketEvaluation:
    row = conn.execute(
        "SELECT tokens_units, capacity_units, refill_per_second, last_refill_ms "
        "FROM token_buckets WHERE config_version = ? AND bucket_kind = ? AND bucket_key = ?",
        (config_version, kind, key),
    ).fetchone()
    if row is None:
        tokens_before = policy.capacity_units
        tokens_after_refill = policy.capacity_units
        refill_units = 0
        last_refill = logical_time_ms
        conn.execute(
            "INSERT INTO token_buckets("
            "config_version, bucket_kind, bucket_key, tokens_units, capacity_units, "
            "refill_per_second, last_refill_ms"
            ") VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                config_version,
                kind,
                key,
                tokens_after_refill,
                policy.capacity_units,
                policy.refill_per_second,
                last_refill,
            ),
        )
    else:
        tokens_before = int(row["tokens_units"])
        elapsed = max(0, logical_time_ms - int(row["last_refill_ms"]))
        refill_units = elapsed * policy.refill_per_second
        tokens_after_refill = min(policy.capacity_units, tokens_before + refill_units)
        last_refill = logical_time_ms
        conn.execute(
            "UPDATE token_buckets SET tokens_units = ?, capacity_units = ?, "
            "refill_per_second = ?, last_refill_ms = ? "
            "WHERE config_version = ? AND bucket_kind = ? AND bucket_key = ?",
            (
                tokens_after_refill,
                policy.capacity_units,
                policy.refill_per_second,
                last_refill,
                config_version,
                kind,
                key,
            ),
        )
    return BucketEvaluation(
        kind=kind,
        key=key,
        capacity_units=policy.capacity_units,
        refill_per_second=policy.refill_per_second,
        tokens_before_units=tokens_before,
        refill_units=refill_units,
        tokens_after_refill_units=tokens_after_refill,
        tokens_after_units=tokens_after_refill,
        last_refill_ms=last_refill,
    )


def _deduct(
    conn: sqlite3.Connection,
    config_version: int,
    evaluation: BucketEvaluation,
    cost_units: int,
) -> None:
    remaining = evaluation.tokens_after_refill_units - cost_units
    evaluation.tokens_after_units = remaining
    conn.execute(
        "UPDATE token_buckets SET tokens_units = ? "
        "WHERE config_version = ? AND bucket_kind = ? AND bucket_key = ?",
        (remaining, config_version, evaluation.kind, evaluation.key),
    )


def _window_digest(
    conn: sqlite3.Connection,
    config_version: int,
    tenant: str,
    route_id: str,
    min_bucket: int,
    max_bucket: int,
) -> str:
    rows = conn.execute(
        "SELECT bucket_index, attempts, rejections FROM window_buckets "
        "WHERE config_version = ? AND tenant = ? AND route_id = ? "
        "AND bucket_index >= ? AND bucket_index <= ? "
        "ORDER BY bucket_index",
        (config_version, tenant, route_id, min_bucket, max_bucket),
    ).fetchall()
    payload = [
        [int(row["bucket_index"]), int(row["attempts"]), int(row["rejections"])]
        for row in rows
    ]
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def evaluate_limits(
    conn: sqlite3.Connection,
    config: GatewayConfig,
    route: Route,
    tenant: str,
    logical_time_ms: int,
) -> dict[str, Any]:
    limits = config.limits
    window_policy = limits.window
    route_policy = limits.route_bucket(route.id)
    tenant_policy = limits.tenant_bucket(tenant)
    cost_units = TOKEN_UNITS

    current_bucket = logical_time_ms // window_policy.step_ms
    bucket_start = current_bucket * window_policy.step_ms
    bucket_end = bucket_start + window_policy.step_ms
    completed_window_start = bucket_start - window_policy.length_ms
    min_bucket = max(0, -(-completed_window_start // window_policy.step_ms))
    summary = conn.execute(
        "SELECT COALESCE(SUM(attempts), 0) AS attempts, "
        "COALESCE(SUM(rejections), 0) AS rejections "
        "FROM window_buckets WHERE config_version = ? AND tenant = ? AND route_id = ? "
        "AND bucket_index >= ? AND bucket_index < ?",
        (config.version, tenant, route.id, min_bucket, current_bucket),
    ).fetchone()
    completed_attempts = int(summary["attempts"])
    completed_rejections = int(summary["rejections"])
    digest_before = _window_digest(
        conn, config.version, tenant, route.id, min_bucket, current_bucket - 1
    )

    route_bucket = _refill_bucket(
        conn,
        config_version=config.version,
        kind="route",
        key=route.id,
        policy=route_policy,
        logical_time_ms=logical_time_ms,
    )
    tenant_bucket = _refill_bucket(
        conn,
        config_version=config.version,
        kind="tenant",
        key=tenant,
        policy=tenant_policy,
        logical_time_ms=logical_time_ms,
    )

    allowed = True
    code: str | None = None
    reason: str | None = None
    if completed_attempts >= window_policy.max_requests:
        allowed = False
        code = "E_WINDOW_REQUEST_LIMIT"
        reason = "completed sliding window request limit reached"
    elif completed_rejections >= window_policy.max_rejections:
        allowed = False
        code = "E_WINDOW_REJECTION_LIMIT"
        reason = "completed sliding window rejection limit reached"
    elif route_bucket.tokens_after_refill_units < cost_units:
        allowed = False
        code = "E_RATE_LIMITED"
        reason = "route token bucket exhausted"
    elif tenant_bucket.tokens_after_refill_units < cost_units:
        allowed = False
        code = "E_RATE_LIMITED"
        reason = "tenant token bucket exhausted"

    if allowed:
        _deduct(conn, config.version, route_bucket, cost_units)
        _deduct(conn, config.version, tenant_bucket, cost_units)

    rejection_increment = 0 if allowed else 1
    existing = conn.execute(
        "SELECT attempts, rejections, first_seen_ms FROM window_buckets "
        "WHERE config_version = ? AND tenant = ? AND route_id = ? AND bucket_index = ?",
        (config.version, tenant, route.id, current_bucket),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO window_buckets("
            "config_version, tenant, route_id, bucket_index, attempts, rejections, "
            "first_seen_ms, last_seen_ms"
            ") VALUES(?, ?, ?, ?, 1, ?, ?, ?)",
            (
                config.version,
                tenant,
                route.id,
                current_bucket,
                rejection_increment,
                logical_time_ms,
                logical_time_ms,
            ),
        )
    else:
        conn.execute(
            "UPDATE window_buckets SET attempts = attempts + 1, "
            "rejections = rejections + ?, last_seen_ms = ? "
            "WHERE config_version = ? AND tenant = ? AND route_id = ? AND bucket_index = ?",
            (
                rejection_increment,
                logical_time_ms,
                config.version,
                tenant,
                route.id,
                current_bucket,
            ),
        )

    digest_after = _window_digest(
        conn, config.version, tenant, route.id, min_bucket, current_bucket
    )
    conn.execute(
        "INSERT INTO sliding_states("
        "config_version, tenant, route_id, current_bucket, window_digest, updated_at_ms"
        ") VALUES(?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(config_version, tenant, route_id) DO UPDATE SET "
        "current_bucket = excluded.current_bucket, "
        "window_digest = excluded.window_digest, "
        "updated_at_ms = excluded.updated_at_ms",
        (
            config.version,
            tenant,
            route.id,
            current_bucket,
            digest_after,
            logical_time_ms,
        ),
    )

    return {
        "allowed": allowed,
        "code": code,
        "reason": reason,
        "route_bucket": route_bucket.as_dict(),
        "tenant_bucket": tenant_bucket.as_dict(),
        "window": {
            "length_ms": window_policy.length_ms,
            "step_ms": window_policy.step_ms,
            "max_requests": window_policy.max_requests,
            "max_rejections": window_policy.max_rejections,
            "bucket_index": current_bucket,
            "bucket_start_ms": bucket_start,
            "bucket_end_ms": bucket_end,
            "completed_window_start_ms": completed_window_start,
            "completed_attempts": completed_attempts,
            "completed_rejections": completed_rejections,
            "decision_digest": digest_before,
            "window_digest": digest_after,
            "current_bucket_attempts": 1
            if existing is None
            else int(existing["attempts"]) + 1,
            "current_bucket_rejections": rejection_increment
            if existing is None
            else int(existing["rejections"]) + rejection_increment,
        },
    }
