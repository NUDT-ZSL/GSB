from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path
from typing import Any, Optional

from .errors import CacheLedgerError

LAYERS = ("L1", "L2", "L3")
LAYER_RANK = {"L1": 0, "L2": 1, "L3": 2}
RANK_LAYER = {0: "L1", 1: "L2", 2: "L3"}


def business_error(code: str, message: str, *, field: Optional[str] = None, **details: Any) -> CacheLedgerError:
    return CacheLedgerError(code, message, field=field, details=details)


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def require_int(value: Any, field_name: str, *, minimum: Optional[int] = None) -> int:
    if not is_int(value):
        raise business_error("E_INVALID_INTEGER", f"{field_name} must be an integer", field=field_name, value=value)
    if minimum is not None and value < minimum:
        raise business_error(
            "E_INVALID_INTEGER",
            f"{field_name} must be >= {minimum}",
            field=field_name,
            value=value,
            minimum=minimum,
        )
    return value


def require_number(value: Any, field_name: str, *, positive: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise business_error("E_INVALID_NUMBER", f"{field_name} must be a number", field=field_name, value=value)
    result = float(value)
    if result != result or result in (float("inf"), float("-inf")):
        raise business_error("E_INVALID_NUMBER", f"{field_name} must be finite", field=field_name)
    if positive and result <= 0:
        raise business_error("E_INVALID_NUMBER", f"{field_name} must be positive", field=field_name, value=value)
    return result


def ratio_to_blocks(max_blocks: int, ratio: Any, field_name: str) -> int:
    require_number(ratio, field_name)
    try:
        decimal_ratio = Decimal(str(ratio))
    except (InvalidOperation, ValueError) as exc:
        raise business_error("E_INVALID_RATIO", f"{field_name} is invalid", field=field_name, value=ratio) from exc
    blocks = int((Decimal(max_blocks) * decimal_ratio).to_integral_value(rounding=ROUND_FLOOR))
    return blocks


def canonical_digest(seq: int, key: str, block_size: int) -> str:
    material = f"cache-ledger-block:v1:{seq}:{key}:{block_size}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise business_error("E_SAMPLE_FILE_MISSING", f"sample file not found: {path}", field=str(path)) from exc
    except json.JSONDecodeError as exc:
        raise business_error("E_INVALID_JSON", f"invalid JSON in {path}: {exc}", field=str(path)) from exc


@dataclass(frozen=True)
class Tenant:
    name: str
    max_blocks: int
    soft_ratio: float
    hard_ratio: float
    soft_quota_blocks: int
    hard_quota_blocks: int
    eviction_priority_weight: float


@dataclass(frozen=True)
class Entry:
    seq: int
    key: str
    tenant: str
    declared_layer: str
    block_size: int
    last_access_ms: int
    access_count: int
    digest: str


@dataclass(frozen=True)
class Capacity:
    layer_max_blocks: dict[str, int]
    block_bytes: int
    total_byte_budget: int
    rebalance_threshold: int
    threshold_unit: str
    cooldown_window_ms: int
    default_clock_ms: int
    clock_tolerance_ms: int
    merge_clock_ms: int = 18_000
    migration_base_cost_ms: int = 2
    migration_block_cost_ms: int = 1
    fixed_tick_ms: int = 64
    byte_pressure_pct: int = 90
    byte_target_pct: int = 80
    hot_window_ms: int = 12000
    hot_access_count: int = 18
    warm_access_count: int = 7
    rebalance_time_budget_ms: int = 18

    def threshold_percent(self) -> int:
        if self.threshold_unit == "percent":
            return self.rebalance_threshold
        max_limit = max(self.layer_max_blocks.values())
        return min(100, int(self.rebalance_threshold * 100 / max(1, max_limit)))


@dataclass(frozen=True)
class V2Record:
    key: str
    layer: Optional[str]
    effective_at_ms: int
    generation: int
    tombstone: bool
    invalid: bool
    shard: str


@dataclass(frozen=True)
class V2Snapshot:
    source_version: str
    snapshot_generation: int
    meta_db: Path
    shards: list[dict[str, Any]]
    records: dict[str, V2Record]


@dataclass(frozen=True)
class PolicyV1:
    path: Path
    source_version: str
    generation: int
    relative_effective_seconds: int
    mappings: dict[str, str]
    raw: dict[str, Any]


@dataclass
class SampleBundle:
    sample_dir: Path
    tenants: dict[str, Tenant]
    entries: list[Entry]
    capacity: Capacity
    policy_v1: PolicyV1
    policy_v2: V2Snapshot
    manifest: dict[str, Any] = field(default_factory=dict)


def parse_tenants(path: Path) -> dict[str, Tenant]:
    payload = read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("tenants"), list):
        raise business_error("E_INVALID_TENANT_DOCUMENT", "tenants.json must contain a tenants array")
    tenants: dict[str, Tenant] = {}
    for index, item in enumerate(payload["tenants"]):
        field_prefix = f"tenants[{index}]"
        if not isinstance(item, dict):
            raise business_error("E_INVALID_TENANT", f"{field_prefix} must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise business_error("E_INVALID_TENANT_NAME", f"{field_prefix}.name must be a non-empty string")
        if name in tenants:
            raise business_error("E_DUPLICATE_TENANT", f"duplicate tenant name: {name}", field="name", tenant=name)
        max_blocks = require_int(item.get("max_blocks"), f"{field_prefix}.max_blocks", minimum=1)
        soft_ratio = require_number(item.get("soft_quota_ratio"), f"{field_prefix}.soft_quota_ratio")
        hard_ratio = require_number(item.get("hard_quota_ratio"), f"{field_prefix}.hard_quota_ratio")
        if not (0 < soft_ratio <= hard_ratio):
            raise business_error(
                "E_INVALID_QUOTA_RATIO",
                "soft quota ratio must be positive and not exceed hard quota ratio",
                field="soft_quota_ratio",
                tenant=name,
            )
        if hard_ratio > 1:
            raise business_error(
                "E_INVALID_QUOTA_RATIO",
                "hard_quota_ratio must not exceed 1.0",
                field="hard_quota_ratio",
                tenant=name,
            )
        weight = require_number(
            item.get("eviction_priority_weight", 1.0),
            f"{field_prefix}.eviction_priority_weight",
            positive=False,
        )
        if weight < 0:
            raise business_error("E_INVALID_WEIGHT", "eviction weight must be non-negative", tenant=name)
        soft_blocks = ratio_to_blocks(max_blocks, item["soft_quota_ratio"], "soft_quota_ratio")
        hard_blocks = ratio_to_blocks(max_blocks, item["hard_quota_ratio"], "hard_quota_ratio")
        if soft_blocks < 1 or hard_blocks < soft_blocks:
            raise business_error("E_INVALID_QUOTA_BLOCKS", "computed quota blocks are invalid", tenant=name)
        tenants[name] = Tenant(
            name=name,
            max_blocks=max_blocks,
            soft_ratio=soft_ratio,
            hard_ratio=hard_ratio,
            soft_quota_blocks=soft_blocks,
            hard_quota_blocks=hard_blocks,
            eviction_priority_weight=weight,
        )
    if not tenants:
        raise business_error("E_INVALID_TENANT_DOCUMENT", "at least one tenant is required")
    return tenants


def parse_entries(path: Path, tenants: dict[str, Tenant]) -> list[Entry]:
    if not path.exists():
        raise business_error("E_SAMPLE_FILE_MISSING", f"entry stream not found: {path}", field=str(path))
    entries: list[Entry] = []
    seen_keys: set[str] = set()
    previous_seq: Optional[int] = None
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise business_error("E_INVALID_JSONL", f"invalid JSONL at line {line_number}", field=str(path)) from exc
            prefix = f"entries[{line_number}]"
            if not isinstance(item, dict):
                raise business_error("E_INVALID_ENTRY", f"{prefix} must be an object")
            seq = require_int(item.get("seq"), f"{prefix}.seq", minimum=0)
            if previous_seq is not None and seq <= previous_seq:
                raise business_error(
                    "E_ENTRY_SEQUENCE",
                    "entry seq must be strictly increasing",
                    field="seq",
                    line=line_number,
                    previous_seq=previous_seq,
                    seq=seq,
                )
            previous_seq = seq
            key = item.get("key")
            if not isinstance(key, str) or not key:
                raise business_error("E_INVALID_ENTRY_KEY", f"{prefix}.key must be a non-empty string")
            if key in seen_keys:
                raise business_error("E_DUPLICATE_ENTRY_KEY", f"duplicate cache key: {key}", field="key", key=key)
            seen_keys.add(key)
            tenant = item.get("tenant")
            if tenant not in tenants:
                raise business_error("E_UNDECLARED_TENANT", f"entry references unknown tenant: {tenant}", tenant=tenant)
            layer = item.get("layer")
            if layer not in LAYERS:
                raise business_error("E_INVALID_LAYER", f"layer must be one of {LAYERS}", field="layer", layer=layer)
            block_size = require_int(item.get("block_size", item.get("size_blocks")), f"{prefix}.block_size", minimum=1)
            last_access_ms = require_int(item.get("last_access_ms"), f"{prefix}.last_access_ms", minimum=0)
            access_count = require_int(item.get("access_count"), f"{prefix}.access_count", minimum=0)
            digest = item.get("digest", item.get("content_digest", item.get("sha256")))
            if not isinstance(digest, str) or not digest:
                raise business_error("E_INVALID_DIGEST", f"{prefix}.digest is required")
            expected = canonical_digest(seq, key, block_size)
            if digest != expected:
                raise business_error(
                    "E_DIGEST_MISMATCH",
                    "content digest is inconsistent with seq, key and block size",
                    field="digest",
                    key=key,
                    expected=expected,
                    actual=digest,
                )
            entries.append(Entry(seq, key, tenant, layer, block_size, last_access_ms, access_count, digest))
    if not entries:
        raise business_error("E_EMPTY_ENTRY_STREAM", "at least one cache entry is required")
    return entries


def parse_capacity(path: Path) -> Capacity:
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise business_error("E_INVALID_CAPACITY", "capacity.json must be an object")
    layers_payload = payload.get("layers")
    if not isinstance(layers_payload, dict):
        raise business_error("E_INVALID_CAPACITY", "capacity.layers must be an object")
    layer_max: dict[str, int] = {}
    for layer in LAYERS:
        layer_data = layers_payload.get(layer)
        if not isinstance(layer_data, dict):
            raise business_error("E_INVALID_CAPACITY", f"missing capacity.layers.{layer}")
        layer_max[layer] = require_int(layer_data.get("max_blocks"), f"layers.{layer}.max_blocks", minimum=1)
    block_bytes = require_int(payload.get("block_bytes", 4096), "block_bytes", minimum=1)
    total_byte_budget = require_int(payload.get("total_byte_budget"), "total_byte_budget", minimum=1)
    threshold = require_int(payload.get("rebalance_threshold"), "rebalance_threshold", minimum=0)
    threshold_unit = payload.get("threshold_unit", "percent")
    if threshold_unit not in ("percent", "blocks"):
        raise business_error("E_INVALID_THRESHOLD_UNIT", "threshold_unit must be percent or blocks")
    if threshold_unit == "percent" and threshold > 100:
        raise business_error("E_INVALID_REBALANCE_THRESHOLD", "percent threshold must be <= 100")
    cooldown = require_int(payload.get("cooldown_window_ms"), "cooldown_window_ms", minimum=0)
    default_clock = require_int(payload.get("default_clock_ms"), "default_clock_ms", minimum=0)
    tolerance = require_int(payload.get("clock_tolerance_ms", 0), "clock_tolerance_ms", minimum=0)
    merge_clock = require_int(payload.get("merge_clock_ms", default_clock), "merge_clock_ms", minimum=0)
    base_cost = require_int(payload.get("migration_base_cost_ms", 2), "migration_base_cost_ms", minimum=0)
    block_cost = require_int(payload.get("migration_block_cost_ms", 1), "migration_block_cost_ms", minimum=0)
    tick_ms = require_int(payload.get("fixed_tick_ms", 64), "fixed_tick_ms", minimum=1)
    byte_pressure = require_int(payload.get("byte_pressure_pct", 90), "byte_pressure_pct", minimum=0)
    byte_target = require_int(payload.get("byte_target_pct", 80), "byte_target_pct", minimum=0)
    hot_window = require_int(payload.get("hot_window_ms", 12000), "hot_window_ms", minimum=0)
    hot_count = require_int(payload.get("hot_access_count", 18), "hot_access_count", minimum=1)
    warm_count = require_int(payload.get("warm_access_count", 7), "warm_access_count", minimum=0)
    budget = require_int(payload.get("rebalance_time_budget_ms", 18), "rebalance_time_budget_ms", minimum=0)
    return Capacity(
        layer_max_blocks=layer_max,
        block_bytes=block_bytes,
        total_byte_budget=total_byte_budget,
        rebalance_threshold=threshold,
        threshold_unit=threshold_unit,
        cooldown_window_ms=cooldown,
        default_clock_ms=default_clock,
        clock_tolerance_ms=tolerance,
        merge_clock_ms=merge_clock,
        migration_base_cost_ms=base_cost,
        migration_block_cost_ms=block_cost,
        fixed_tick_ms=tick_ms,
        byte_pressure_pct=byte_pressure,
        byte_target_pct=byte_target,
        hot_window_ms=hot_window,
        hot_access_count=hot_count,
        warm_access_count=warm_count,
        rebalance_time_budget_ms=budget,
    )


def parse_v1(path: Path, entry_keys: set[str]) -> PolicyV1:
    payload = read_json(path)
    if not isinstance(payload, dict) or payload.get("format") != "v1":
        raise business_error("E_V1_POLICY_FORMAT", "policy_v1.json must declare format=v1")
    source_version = payload.get("source_version")
    if not isinstance(source_version, str) or not source_version:
        raise business_error("E_V1_POLICY_VERSION", "source_version must be a non-empty string")
    generation = require_int(payload.get("generation", 1), "generation", minimum=0)
    relative_seconds = require_int(
        payload.get("relative_effective_seconds"),
        "relative_effective_seconds",
        minimum=0,
    )
    mappings = payload.get("mappings")
    if not isinstance(mappings, dict) or not mappings:
        raise business_error("E_V1_POLICY_MAPPINGS", "mappings must be a flat key to layer object")
    normalized: dict[str, str] = {}
    for key, layer in mappings.items():
        if key not in entry_keys:
            raise business_error(
                "E_UNDECLARED_POLICY_KEY",
                "v1 policy key cannot be traced to a declared cache key",
                field="mappings",
                key=key,
            )
        if layer not in LAYERS:
            raise business_error("E_INVALID_LAYER", f"v1 mapping for {key} has invalid layer {layer}", key=key)
        normalized[key] = layer
    return PolicyV1(path, source_version, generation, relative_seconds, normalized, payload)


def parse_v2(sample_dir: Path, entry_keys: set[str]) -> V2Snapshot:
    meta_db = sample_dir / "policy_v2" / "policy_v2_meta.sqlite"
    shard_dir = sample_dir / "policy_v2" / "shards"
    if not meta_db.exists():
        raise business_error("E_V2_META_MISSING", f"v2 metadata SQLite database not found: {meta_db}")
    if not shard_dir.exists():
        raise business_error("E_V2_SHARD_DIR_MISSING", f"v2 shard directory not found: {shard_dir}")
    with sqlite3.connect(f"file:{meta_db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        try:
            meta_rows = conn.execute("SELECT * FROM shards ORDER BY shard_index").fetchall()
            policy_rows = conn.execute("SELECT * FROM policies").fetchall()
            snapshot_row = conn.execute("SELECT * FROM snapshot WHERE id=1").fetchone()
        except sqlite3.DatabaseError as exc:
            raise business_error("E_V2_META_INVALID", f"cannot read v2 metadata: {exc}") from exc
    if snapshot_row is None:
        raise business_error("E_V2_META_INVALID", "v2 snapshot row is missing")
    source_version = snapshot_row["source_version"]
    snapshot_generation = require_int(snapshot_row["generation"], "v2.snapshot.generation", minimum=1)
    shards: list[dict[str, Any]] = []
    previous_generation: Optional[int] = None
    for row in meta_rows:
        shard_name = row["shard"]
        shard_path = shard_dir / row["path"] if "path" in row.keys() else shard_dir / f"{shard_name}.json"
        if not shard_path.exists():
            raise business_error("E_V2_SHARD_MISSING", f"v2 shard file missing: {shard_path}", shard=shard_name)
        actual_hash = sha256_file(shard_path)
        if actual_hash != row["sha256"]:
            raise business_error(
                "E_V2_SHARD_HASH_MISMATCH",
                "v2 shard SHA-256 hash does not match metadata",
                field="sha256",
                shard=shard_name,
                expected=row["sha256"],
                actual=actual_hash,
            )
        generation = require_int(row["generation"], f"v2.shards.{shard_name}.generation", minimum=1)
        if previous_generation is not None and generation < previous_generation:
            raise business_error(
                "E_POLICY_GENERATION_NOT_MONOTONIC",
                "v2 shard generations must be non-decreasing",
                field="generation",
                shard=shard_name,
                previous_generation=previous_generation,
                generation=generation,
            )
        previous_generation = generation
        payload = read_json(shard_path)
        if not isinstance(payload, dict) or not isinstance(payload.get("policies"), dict):
            raise business_error("E_V2_SHARD_INVALID", f"shard {shard_name} must contain policies object")
        shards.append(
            {
                "shard": shard_name,
                "path": str(shard_path),
                "sha256": actual_hash,
                "generation": generation,
                "payload": payload,
            }
        )
    records: dict[str, V2Record] = {}
    for row in policy_rows:
        key = row["key"]
        if key not in entry_keys:
            raise business_error(
                "E_UNDECLARED_POLICY_KEY",
                "v2 policy key cannot be traced to a declared cache key",
                key=key,
            )
        layer = row["layer"]
        if layer is not None and layer not in LAYERS:
            raise business_error("E_INVALID_LAYER", f"v2 policy for {key} has invalid layer {layer}", key=key)
        effective_at_ms = require_int(row["effective_at_ms"], f"v2.policies.{key}.effective_at_ms", minimum=0)
        generation = require_int(row["generation"], f"v2.policies.{key}.generation", minimum=0)
        if generation > snapshot_generation:
            raise business_error(
                "E_POLICY_GENERATION_NOT_MONOTONIC",
                "policy generation cannot exceed snapshot generation",
                key=key,
                generation=generation,
                snapshot_generation=snapshot_generation,
            )
        record = V2Record(
            key=key,
            layer=layer,
            effective_at_ms=effective_at_ms,
            generation=generation,
            tombstone=bool(row["tombstone"]),
            invalid=bool(row["invalid"]),
            shard=row["shard"],
        )
        records[key] = record
    if not records:
        raise business_error("E_V2_POLICIES_EMPTY", "v2 metadata contains no policies")
    return V2Snapshot(str(source_version), snapshot_generation, meta_db, shards, records)


def load_bundle(sample_dir: str | Path) -> SampleBundle:
    directory = Path(sample_dir)
    manifest_path = directory / "manifest.json"
    manifest = read_json(manifest_path) if manifest_path.exists() else {}
    tenants = parse_tenants(directory / "tenants.json")
    entries = parse_entries(directory / "entries.jsonl", tenants)
    capacity = parse_capacity(directory / "capacity.json")
    entry_keys = {entry.key for entry in entries}
    policy_v1 = parse_v1(directory / "policy_v1.json", entry_keys)
    policy_v2 = parse_v2(directory, entry_keys)
    return SampleBundle(directory, tenants, entries, capacity, policy_v1, policy_v2, manifest)
