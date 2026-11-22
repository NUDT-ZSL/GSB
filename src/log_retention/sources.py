from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .config import Config
from .errors import ArchiveError, fail
from .models import (
    InstanceReplayState,
    NormalizedInput,
    Policy,
    ReplayEvent,
    Transaction,
)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _read_jsonl(path: Path, error_code: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ArchiveError(
            error_code, "cannot read input file", {"path": str(path)}
        ) from exc
    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ArchiveError(
                error_code,
                "input line is not valid JSON",
                {"path": str(path), "line": line_no, "column": exc.colno},
            ) from exc
        if not isinstance(value, dict):
            raise ArchiveError(
                error_code,
                "input line must be a JSON object",
                {"path": str(path), "line": line_no},
            )
        value["_line"] = line_no
        value["_source"] = str(path)
        records.append(value)
    return records


def _load_primary(path: Path) -> list[Transaction]:
    records = _read_jsonl(path, "E_PRIMARY_PARSE")
    if not records:
        fail("E_PRIMARY_EMPTY", "primary stream has no transactions", path=str(path))
    transactions: list[Transaction] = []
    previous_txn: int | None = None
    for record in records:
        txn_id = record.get("txn_id")
        commit_ms = record.get("commit_ms")
        byte_size = record.get("byte_size")
        segment_id = record.get("segment_id")
        line = record["_line"]
        if not _is_int(txn_id) or txn_id < 1:
            fail(
                "E_PRIMARY_FIELD",
                "txn_id must be a positive integer",
                line=line,
                txn_id=txn_id,
            )
        if previous_txn is not None and txn_id <= previous_txn:
            fail(
                "E_PRIMARY_TXN_ORDER",
                "primary transaction ids must be strictly increasing",
                line=line,
                previous=previous_txn,
                current=txn_id,
            )
        if not _is_int(commit_ms) or commit_ms < 0:
            fail(
                "E_PRIMARY_FIELD",
                "commit_ms must be a non-negative integer",
                line=line,
                txn_id=txn_id,
            )
        if not _is_int(byte_size) or byte_size < 0:
            fail(
                "E_PRIMARY_FIELD",
                "byte_size must be a non-negative integer",
                line=line,
                txn_id=txn_id,
            )
        if not _is_int(segment_id) or segment_id < 0:
            fail(
                "E_PRIMARY_FIELD",
                "segment_id must be a non-negative integer",
                line=line,
                txn_id=txn_id,
            )
        transactions.append(Transaction(txn_id, commit_ms, byte_size, segment_id))
        previous_txn = txn_id

    segments = sorted({txn.segment_id for txn in transactions})
    expected = list(range(min(segments), max(segments) + 1))
    if segments != expected:
        missing = sorted(set(expected) - set(segments))
        fail(
            "E_PRIMARY_SEGMENT_GAP",
            "log segment ids must be contiguous and cover every transaction",
            missing_segments=missing,
            observed_segments=segments,
        )
    by_segment: dict[int, list[Transaction]] = {}
    for txn in transactions:
        by_segment.setdefault(txn.segment_id, []).append(txn)
    for segment_id, items in by_segment.items():
        if not items:
            fail(
                "E_PRIMARY_SEGMENT_GAP",
                "log segment has no transaction",
                segment_id=segment_id,
            )
    return transactions


def _load_replay(
    path: Path, transactions: list[Transaction]
) -> tuple[list[ReplayEvent], dict[str, InstanceReplayState]]:
    records = _read_jsonl(path, "E_REPLAY_PARSE")
    if not records:
        fail("E_REPLAY_EMPTY", "replay stream has no events", path=str(path))
    txn_ids = {txn.txn_id for txn in transactions}
    previous_by_instance: dict[str, int] = {}
    events: list[ReplayEvent] = []
    states: dict[str, InstanceReplayState] = {}
    for sequence, record in enumerate(records, start=1):
        line = record["_line"]
        txn_id = record.get("txn_id")
        instance_id = record.get("instance_id")
        replay_ms = record.get("replay_ms")
        result = record.get("result")
        tombstone = record.get("tombstone")
        if not _is_int(txn_id) or txn_id < 1:
            fail("E_REPLAY_FIELD", "txn_id must be a positive integer", line=line)
        if txn_id not in txn_ids:
            fail(
                "E_REPLAY_UNKNOWN_TXN",
                "replay event references a transaction absent from the primary stream",
                line=line,
                txn_id=txn_id,
            )
        if not isinstance(instance_id, str) or not instance_id.strip():
            fail("E_REPLAY_FIELD", "instance_id must be a non-empty string", line=line)
        if any(ch.isspace() for ch in instance_id):
            fail("E_REPLAY_FIELD", "instance_id must not contain whitespace", line=line)
        previous = previous_by_instance.get(instance_id)
        if previous is not None and txn_id <= previous:
            fail(
                "E_REPLAY_TXN_ORDER",
                "replay transaction ids must be strictly increasing per instance",
                line=line,
                instance_id=instance_id,
                previous=previous,
                current=txn_id,
            )
        if not _is_int(replay_ms) or replay_ms < 0:
            fail(
                "E_REPLAY_FIELD", "replay_ms must be a non-negative integer", line=line
            )
        if result not in {"active", "invalidated"}:
            fail(
                "E_REPLAY_RESULT",
                "result must be 'active' or 'invalidated'",
                line=line,
                result=result,
            )
        if not isinstance(tombstone, bool):
            fail("E_REPLAY_FIELD", "tombstone must be a boolean", line=line)
        if tombstone != (result == "invalidated"):
            fail(
                "E_REPLAY_TOMBSTONE_MISMATCH",
                "replay result and tombstone flag disagree",
                line=line,
                instance_id=instance_id,
                result=result,
                tombstone=tombstone,
            )
        events.append(
            ReplayEvent(sequence, txn_id, instance_id, replay_ms, result, tombstone)
        )
        previous_by_instance[instance_id] = txn_id
        prior = states.get(instance_id)
        states[instance_id] = InstanceReplayState(
            instance_id=instance_id,
            latest_txn_id=txn_id,
            latest_replay_ms=replay_ms,
            result=result,
            tombstone=tombstone,
            event_count=1 if prior is None else prior.event_count + 1,
        )
    return events, states


def _policy_common_fields(
    record: dict[str, Any],
    *,
    source_version: str,
    source_ref: str,
    source_priority: int,
    default_policy_id: str,
) -> dict[str, Any]:
    policy_id = record.get("policy_id", default_policy_id)
    instance_id = record.get("instance_id")
    segment_id = record.get("segment_id")
    generation = record.get("retention_generation", 1)
    effective_at = record.get("effective_at_ms")
    parent = record.get("parent_policy_id")
    declared_margin = record.get("required_min_catchup_margin", 0)
    tombstone = record.get("policy_tombstone", False)
    if not isinstance(policy_id, str) or not policy_id.strip():
        fail("E_POLICY_ID", "policy_id must be a non-empty string", source=source_ref)
    if not isinstance(instance_id, str) or not instance_id.strip():
        fail(
            "E_POLICY_INSTANCE",
            "instance_id must be a non-empty string",
            policy_id=policy_id,
        )
    if any(ch.isspace() for ch in instance_id):
        fail(
            "E_POLICY_INSTANCE",
            "instance_id must not contain whitespace",
            policy_id=policy_id,
        )
    if segment_id is not None and (not _is_int(segment_id) or segment_id < 0):
        fail(
            "E_POLICY_SEGMENT",
            "segment_id must be null or a non-negative integer",
            policy_id=policy_id,
        )
    if not _is_int(generation) or generation < 0:
        fail(
            "E_POLICY_GENERATION",
            "retention_generation must be a non-negative integer",
            policy_id=policy_id,
        )
    if not _is_int(effective_at) or effective_at < 0:
        fail(
            "E_POLICY_EFFECTIVE_AT",
            "effective_at_ms must be a non-negative integer",
            policy_id=policy_id,
        )
    if parent is not None and (not isinstance(parent, str) or not parent):
        fail(
            "E_POLICY_PARENT",
            "parent_policy_id must be null or a non-empty string",
            policy_id=policy_id,
        )
    if not _is_int(declared_margin) or declared_margin < 0:
        fail(
            "E_POLICY_MARGIN",
            "required_min_catchup_margin must be a non-negative integer",
            policy_id=policy_id,
        )
    if not isinstance(tombstone, bool):
        fail(
            "E_POLICY_TOMBSTONE",
            "policy_tombstone must be a boolean",
            policy_id=policy_id,
        )
    return {
        "policy_id": policy_id,
        "source_version": source_version,
        "instance_id": instance_id,
        "segment_id": segment_id,
        "retention_generation": generation,
        "effective_at_ms": effective_at,
        "required_min_catchup_margin": declared_margin,
        "parent_policy_id": parent,
        "tombstone": tombstone,
        "source_ref": source_ref,
        "source_priority": source_priority,
    }


def _load_v1(path: Path, priority: int) -> list[Policy]:
    records = _read_jsonl(path, "E_POLICY_V1_PARSE")
    if not records:
        fail("E_POLICY_V1_EMPTY", "v1 policy file has no records", path=str(path))
    policies: list[Policy] = []
    seen_instances: set[str] = set()
    for record in records:
        line = record["_line"]
        common = _policy_common_fields(
            record,
            source_version="v1",
            source_ref=f"{path}:{line}",
            source_priority=priority,
            default_policy_id=f"v1-{record.get('instance_id', 'unknown')}",
        )
        if common["instance_id"] in seen_instances:
            fail(
                "E_POLICY_V1_DUPLICATE_INSTANCE",
                "v1 policy instance ids must be unique",
                line=line,
                instance_id=common["instance_id"],
            )
        seen_instances.add(common["instance_id"])
        relative = record.get("relative_retention_seconds")
        if not _is_int(relative) or relative < 0:
            fail(
                "E_POLICY_RELATIVE_SECONDS",
                "relative_retention_seconds must be a non-negative integer",
                line=line,
                policy_id=common["policy_id"],
            )
        window = record.get("retention_window", "default")
        if not isinstance(window, str) or not window:
            fail(
                "E_POLICY_WINDOW",
                "retention_window must be a non-empty string",
                line=line,
            )
        policies.append(
            Policy(
                **common,
                relative_seconds=relative,
                absolute_cutoff_ms=None,
                retention_window=window,
                raw={k: v for k, v in record.items() if not k.startswith("_")},
            )
        )
    return policies


def _read_v2_meta(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        fail(
            "E_POLICY_V2_META_MISSING",
            "v2 policy metadata database is missing",
            path=str(path),
        )
    uri = f"file:{path}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM policy_meta ORDER BY policy_id").fetchall()
    except sqlite3.Error as exc:
        raise ArchiveError(
            "E_POLICY_V2_META",
            "cannot read v2 policy metadata database",
            {"path": str(path), "reason": str(exc)},
        ) from exc
    finally:
        if "conn" in locals():
            conn.close()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = dict(row)
        policy_id = value.get("policy_id")
        if not isinstance(policy_id, str) or not policy_id:
            fail("E_POLICY_V2_META", "policy_meta.policy_id must be non-empty")
        if policy_id in result:
            fail(
                "E_POLICY_V2_META",
                "duplicate policy_meta.policy_id",
                policy_id=policy_id,
            )
        result[policy_id] = value
    if not result:
        fail("E_POLICY_V2_META_EMPTY", "v2 policy metadata database has no rows")
    return result


def _load_v2(
    shard_paths: Iterable[Path], meta_path: Path, priority: int
) -> list[Policy]:
    meta = _read_v2_meta(meta_path)
    shard_policies: dict[str, tuple[dict[str, Any], str]] = {}
    for shard_path in shard_paths:
        try:
            document = json.loads(shard_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ArchiveError(
                "E_POLICY_V2_SHARD_READ",
                "cannot read v2 shard",
                {"path": str(shard_path)},
            ) from exc
        except json.JSONDecodeError as exc:
            raise ArchiveError(
                "E_POLICY_V2_SHARD_JSON",
                "v2 shard is not valid JSON",
                {"path": str(shard_path), "line": exc.lineno, "column": exc.colno},
            ) from exc
        if not isinstance(document, dict) or not isinstance(
            document.get("policies"), list
        ):
            fail(
                "E_POLICY_V2_SHARD",
                "v2 shard must contain a policies list",
                path=str(shard_path),
            )
        for index, record in enumerate(document["policies"], start=1):
            if not isinstance(record, dict):
                fail(
                    "E_POLICY_V2_SHARD",
                    "v2 policy must be an object",
                    path=str(shard_path),
                    index=index,
                )
            policy_id = record.get("policy_id")
            if not isinstance(policy_id, str) or not policy_id:
                fail(
                    "E_POLICY_ID",
                    "v2 policy_id must be non-empty",
                    path=str(shard_path),
                    index=index,
                )
            if policy_id in shard_policies:
                fail(
                    "E_POLICY_V2_DUPLICATE_ID",
                    "v2 policy ids must be unique across shards",
                    policy_id=policy_id,
                    first=shard_policies[policy_id][1],
                    second=f"{shard_path}:{index}",
                )
            shard_policies[policy_id] = (record, f"{shard_path}:{index}")
    if set(shard_policies) != set(meta):
        fail(
            "E_POLICY_V2_SOURCE_MISMATCH",
            "v2 shard policy ids and SQLite metadata ids differ",
            only_shards=sorted(set(shard_policies) - set(meta)),
            only_metadata=sorted(set(meta) - set(shard_policies)),
        )

    policies: list[Policy] = []
    for policy_id in sorted(shard_policies):
        record, source_ref = shard_policies[policy_id]
        meta_record = meta[policy_id]
        for key in (
            "instance_id",
            "segment_id",
            "retention_generation",
            "effective_at_ms",
            "absolute_retention_cutoff_ms",
            "parent_policy_id",
            "required_min_catchup_margin",
            "policy_tombstone",
        ):
            if key in record and record[key] != meta_record.get(key):
                fail(
                    "E_POLICY_V2_SOURCE_MISMATCH",
                    "v2 shard and SQLite metadata disagree",
                    policy_id=policy_id,
                    field=key,
                    shard_value=record.get(key),
                    metadata_value=meta_record.get(key),
                )
        merged = dict(record)
        for key in (
            "instance_id",
            "segment_id",
            "retention_generation",
            "effective_at_ms",
            "absolute_retention_cutoff_ms",
            "parent_policy_id",
            "required_min_catchup_margin",
            "policy_tombstone",
        ):
            if key in meta_record:
                merged.setdefault(key, meta_record[key])
        tombstone_value = merged.get("policy_tombstone")
        if (
            isinstance(tombstone_value, int)
            and not isinstance(tombstone_value, bool)
            and tombstone_value in (0, 1)
        ):
            merged["policy_tombstone"] = bool(tombstone_value)
        common = _policy_common_fields(
            merged,
            source_version="v2",
            source_ref=source_ref,
            source_priority=priority,
            default_policy_id=policy_id,
        )
        absolute = merged.get("absolute_retention_cutoff_ms")
        if not _is_int(absolute):
            fail(
                "E_POLICY_ABSOLUTE_MS",
                "absolute_retention_cutoff_ms must be an integer",
                policy_id=policy_id,
            )
        policies.append(
            Policy(
                **common,
                relative_seconds=None,
                absolute_cutoff_ms=absolute,
                retention_window=merged.get("retention_window"),
                raw=merged,
            )
        )
    return policies


def _validate_policy_graph(
    config: Config,
    policies: list[Policy],
    replay_states: dict[str, InstanceReplayState],
    segment_ids: set[int],
) -> None:
    by_id: dict[str, list[Policy]] = {}
    by_key: dict[tuple[str, str], Policy] = {}
    for policy in policies:
        unique_key = (policy.source_version, policy.policy_id)
        if unique_key in by_key:
            fail(
                "E_POLICY_DUPLICATE_ID",
                "policy ids must be unique within a source format",
                policy_id=policy.policy_id,
                source_version=policy.source_version,
                first=by_key[unique_key].source_ref,
                second=policy.source_ref,
            )
        by_key[unique_key] = policy
        by_id.setdefault(policy.policy_id, []).append(policy)
        if policy.segment_id is not None and policy.segment_id not in segment_ids:
            fail(
                "E_POLICY_SEGMENT_UNKNOWN",
                "policy references an unknown log segment",
                policy_id=policy.policy_id,
                segment_id=policy.segment_id,
            )
        if policy.instance_id not in replay_states:
            fail(
                "E_POLICY_INSTANCE_UNKNOWN",
                "policy references an instance absent from the replay stream",
                policy_id=policy.policy_id,
                instance_id=policy.instance_id,
            )
        if policy.instance_id not in config.replay_rates_bytes_per_sec:
            fail(
                "E_CONFIG_REPLAY_RATE",
                "no replay rate configured for policy instance",
                instance_id=policy.instance_id,
            )

    children: dict[str, list[str]] = {policy.key: [] for policy in policies}
    for policy in policies:
        if policy.parent_policy_id is None:
            continue
        candidates = by_id.get(policy.parent_policy_id, [])
        if not candidates:
            fail(
                "E_POLICY_PARENT_MISSING",
                "policy parent does not exist",
                policy_id=policy.policy_id,
                parent_policy_id=policy.parent_policy_id,
            )
        same_source = [
            candidate
            for candidate in candidates
            if candidate.source_version == policy.source_version
        ]
        if len(same_source) == 1:
            parent = same_source[0]
        elif len(candidates) == 1:
            parent = candidates[0]
        else:
            fail(
                "E_POLICY_PARENT_AMBIGUOUS",
                "policy parent id matches multiple source formats",
                policy_id=policy.policy_id,
                parent_policy_id=policy.parent_policy_id,
                source_version=policy.source_version,
            )
        if parent.instance_id != policy.instance_id:
            fail(
                "E_POLICY_PARENT_INSTANCE",
                "policy parent belongs to a different replay instance",
                policy_id=policy.policy_id,
                parent_policy_id=policy.parent_policy_id,
            )
        if policy.retention_generation < parent.retention_generation:
            fail(
                "E_POLICY_GENERATION_ORDER",
                "retention generations must be monotonic along a policy lineage",
                policy_id=policy.policy_id,
                generation=policy.retention_generation,
                parent_generation=parent.retention_generation,
            )
        children[parent.key].append(policy.key)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(policy_id: str) -> None:
        if policy_id in visiting:
            fail(
                "E_POLICY_CYCLE",
                "policy parent graph contains a cycle",
                policy_id=policy_id,
            )
        if policy_id in visited:
            return
        visiting.add(policy_id)
        for child_id in children[policy_id]:
            visit(child_id)
        visiting.remove(policy_id)
        visited.add(policy_id)

    for policy_key in children:
        visit(policy_key)

    for instance_id, state in replay_states.items():
        instance_policies = [p for p in policies if p.instance_id == instance_id]
        if not instance_policies:
            fail(
                "E_POLICY_INSTANCE_MISSING",
                "replay instance has no retention policy",
                instance_id=instance_id,
            )
        tombstones = [p for p in instance_policies if p.tombstone]
        leaves = [p for p in instance_policies if not children[p.key]]
        if state.tombstone:
            if not tombstones or any(not p.tombstone for p in leaves):
                fail(
                    "E_POLICY_TOMBSTONE_REPLAY_MISMATCH",
                    "an invalidated replay instance must have tombstone leaf policies",
                    instance_id=instance_id,
                )
        elif tombstones:
            fail(
                "E_POLICY_TOMBSTONE_REPLAY_MISMATCH",
                "a tombstone policy requires an invalidated replay result",
                instance_id=instance_id,
                tombstone_policy_ids=sorted(p.policy_id for p in tombstones),
            )


def _fingerprint(
    transactions: list[Transaction],
    replay_events: list[ReplayEvent],
    policies: list[Policy],
) -> str:
    payload = {
        "transactions": [
            [t.txn_id, t.commit_ms, t.byte_size, t.segment_id] for t in transactions
        ],
        "replay": [
            [e.sequence, e.txn_id, e.instance_id, e.replay_ms, e.result, e.tombstone]
            for e in replay_events
        ],
        "policies": [
            [
                p.source_version,
                p.policy_id,
                p.instance_id,
                p.segment_id,
                p.retention_generation,
                p.effective_at_ms,
                p.relative_seconds,
                p.absolute_cutoff_ms,
                p.required_min_catchup_margin,
                p.parent_policy_id,
                p.tombstone,
            ]
            for p in sorted(policies, key=lambda p: (p.source_version, p.policy_id))
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_normalized_input(config: Config) -> NormalizedInput:
    transactions = _load_primary(config.primary_stream)
    replay_events, replay_states = _load_replay(config.replay_stream, transactions)
    policies = _load_v1(
        config.v1_policy, config.source_priority.get("v1", 10)
    ) + _load_v2(
        config.v2_shards,
        config.v2_meta_db,
        config.source_priority.get("v2", 20),
    )
    _validate_policy_graph(
        config,
        policies,
        replay_states,
        {txn.segment_id for txn in transactions},
    )
    return NormalizedInput(
        transactions=transactions,
        replay_events=replay_events,
        replay_states=replay_states,
        policies=sorted(policies, key=lambda p: (p.source_version, p.policy_id)),
        segment_ids=sorted({txn.segment_id for txn in transactions}),
        fingerprint=_fingerprint(transactions, replay_events, policies),
    )
