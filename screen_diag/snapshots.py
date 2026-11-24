from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .errors import SchemaError
from .hashing import sha256_hex
from .model import SnapshotRecord
from .validation import load_json_file, require_dict, require_int, require_nonempty_str


DEFAULT_MEMORY_BUDGET_BYTES = 32 * 1024 * 1024


class ShardInvalid(Exception):
    def __init__(self, reason: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


def _connect_metadata(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise SchemaError("v2 metadata database is missing", {"path": str(db_path)})
    uri = db_path.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("SELECT 1 FROM shards LIMIT 1")
        connection.execute("SELECT 1 FROM screen_records LIMIT 1")
        return connection
    except sqlite3.Error as exc:
        raise SchemaError("cannot open v2 SQLite metadata", {"path": str(db_path), "reason": str(exc)}) from exc


def _read_shard_descriptors(v2_dir: Path) -> List[Dict[str, Any]]:
    db_path = v2_dir / "metadata.sqlite"
    connection = _connect_metadata(db_path)
    try:
        shard_rows = connection.execute(
            """
            SELECT shard_id, relative_path, logic_ms, declared_screen_count, digest
            FROM shards
            ORDER BY shard_id
            """
        ).fetchall()
        screen_rows = connection.execute(
            """
            SELECT canonical_screen_id, logic_ms, generation, keyframe_seq,
                   decoded_frame_seq, play_position_ms, tombstone, shard_id
            FROM screen_records
            ORDER BY canonical_screen_id, logic_ms, shard_id
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise SchemaError("v2 SQLite metadata schema is invalid", {"reason": str(exc)}) from exc
    finally:
        connection.close()

    screens_by_shard: Dict[str, List[str]] = {}
    db_records_by_shard: Dict[str, List[Dict[str, Any]]] = {}
    for row in screen_rows:
        row_dict = dict(row)
        shard_id = row_dict["shard_id"]
        screens_by_shard.setdefault(shard_id, []).append(row_dict["canonical_screen_id"])
        db_records_by_shard.setdefault(shard_id, []).append(row_dict)

    descriptors: List[Dict[str, Any]] = []
    for row in shard_rows:
        item = dict(row)
        shard_id = item.get("shard_id")
        db_screen_ids = sorted(set(screens_by_shard.get(shard_id, [])))
        item["db_screen_ids"] = db_screen_ids
        item["db_records"] = db_records_by_shard.get(shard_id, [])
        item["order_key"] = (db_screen_ids[0] if db_screen_ids else "~~~", str(shard_id))
        relative_path = item.get("relative_path") or ""
        item["path"] = (v2_dir / relative_path).resolve()
        try:
            item["path"].relative_to(v2_dir.resolve())
        except ValueError as exc:
            raise SchemaError("shard path escapes v2 directory", {"path": relative_path}) from exc
        descriptors.append(item)

    descriptors.sort(key=lambda item: item["order_key"])
    return descriptors


def _parse_v2_records(raw_doc: Any, descriptor: Dict[str, Any], source_path: Path) -> Tuple[List[SnapshotRecord], List[str]]:
    root = require_dict(raw_doc, f"shard {descriptor.get('shard_id')}")
    raw_records = root.get("records")
    if not isinstance(raw_records, list):
        raise ShardInvalid("shard records must be a list")

    records: List[SnapshotRecord] = []
    screen_ids: List[str] = []
    shard_id = require_nonempty_str(root.get("shard_id", descriptor.get("shard_id")), "shard_id")
    for index, raw_record in enumerate(raw_records):
        try:
            record = require_dict(raw_record, f"records[{index}]")
            canonical_id = require_nonempty_str(record.get("canonical_screen_id"), "canonical_screen_id")
            logic_ms = require_int(record.get("logic_ms"), "logic_ms", minimum=0)
            generation = require_int(record.get("generation"), "generation", minimum=1)
            keyframe_seq = record.get("keyframe_seq")
            decoded = record.get("decoded_frame_seq")
            position = record.get("play_position_ms")
            if keyframe_seq is not None:
                keyframe_seq = require_int(keyframe_seq, "keyframe_seq", minimum=0)
            if decoded is not None:
                decoded = require_int(decoded, "decoded_frame_seq", minimum=0)
            if position is not None:
                position = require_int(position, "play_position_ms", minimum=0)
            tombstone = bool(record.get("tombstone", False))
        except SchemaError as exc:
            raise ShardInvalid(exc.message, exc.details) from exc

        screen_ids.append(canonical_id)
        records.append(
            SnapshotRecord(
                canonical_screen_id=canonical_id,
                logic_ms=logic_ms,
                generation=generation,
                keyframe_seq=keyframe_seq,
                decoded_frame_seq=decoded,
                play_position_ms=position,
                tombstone=tombstone,
                source_kind="v2",
                source_locator=f"{source_path.as_posix()}#records[{index}]",
                shard_id=shard_id,
            )
        )
    return records, sorted(set(screen_ids))


def _load_one_shard(descriptor: Dict[str, Any]) -> Dict[str, Any]:
    path: Path = descriptor["path"]
    shard_id = descriptor["shard_id"]
    result: Dict[str, Any] = {
        "shard_id": shard_id,
        "relative_path": descriptor.get("relative_path"),
        "path": str(path),
        "logic_ms": descriptor.get("logic_ms"),
        "declared_screen_count": descriptor.get("declared_screen_count"),
        "expected_digest": descriptor.get("digest"),
        "actual_digest": None,
        "actual_screen_count": None,
        "screen_ids": descriptor.get("db_screen_ids", []),
        "valid": False,
        "records": [],
        "failures": [],
    }

    raw_doc: Any = None
    if not path.is_file():
        result["failures"].append({"code": "E_DIGEST", "reason": "shard file is missing"})
        return result

    try:
        raw_text = path.read_text(encoding="utf-8")
        raw_doc = json.loads(raw_text)
    except OSError as exc:
        result["failures"].append({"code": "E_DIGEST", "reason": f"shard cannot be read: {exc}"})
        return result
    except json.JSONDecodeError as exc:
        result["failures"].append({"code": "E_DIGEST", "reason": f"shard JSON is invalid: {exc}"})
        return result

    result["actual_digest"] = sha256_hex(raw_doc)
    try:
        records, actual_screen_ids = _parse_v2_records(raw_doc, descriptor, path)
        result["records"] = [record.to_dict() for record in records]
        result["actual_screen_count"] = len(actual_screen_ids)
        result["screen_ids"] = actual_screen_ids
    except ShardInvalid as exc:
        result["failures"].append({"code": "E_DIGEST", "reason": exc.reason, "details": exc.details})

    if not descriptor.get("digest"):
        result["failures"].append({"code": "E_DIGEST", "reason": "recorded shard digest is missing"})
    elif result["actual_digest"] != descriptor["digest"]:
        result["failures"].append(
            {
                "code": "E_DIGEST",
                "reason": "content digest does not match recorded digest",
                "expected_digest": descriptor["digest"],
                "actual_digest": result["actual_digest"],
            }
        )

    declared_count = descriptor.get("declared_screen_count")
    if not isinstance(declared_count, int) or isinstance(declared_count, bool):
        result["failures"].append({"code": "E_DIGEST", "reason": "declared_screen_count is missing or not an integer"})
    elif result["actual_screen_count"] is not None and declared_count != result["actual_screen_count"]:
        result["failures"].append(
            {
                "code": "E_DIGEST",
                "reason": "declared screen count does not match actual count",
                "declared_screen_count": declared_count,
                "actual_screen_count": result["actual_screen_count"],
            }
        )

    result["valid"] = not result["failures"]
    return result


def load_v2_snapshot(
    v2_dir: Path | str,
    *,
    max_workers: int | None = None,
    memory_budget_bytes: int = DEFAULT_MEMORY_BUDGET_BYTES,
) -> Dict[str, Any]:
    v2_dir = Path(v2_dir)
    descriptors = _read_shard_descriptors(v2_dir)
    if max_workers is None:
        max_workers = min(4, os.cpu_count() or 1)
    max_workers = max(1, int(max_workers))
    memory_budget_bytes = max(1, int(memory_budget_bytes))

    ordered = sorted(descriptors, key=lambda item: item["order_key"])
    shard_results: List[Optional[Dict[str, Any]]] = [None] * len(ordered)
    in_flight: Dict[Any, Tuple[int, int]] = {}
    used_bytes = 0
    peak_in_flight_bytes = 0
    peak_worker_count = 0
    budget_waits: List[Dict[str, Any]] = []
    concurrency_waits = 0

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="shard-loader") as executor:
        for index, descriptor in enumerate(ordered):
            path: Path = descriptor["path"]
            try:
                estimated_bytes = max(1, path.stat().st_size)
            except OSError:
                estimated_bytes = 4096

            oversized = estimated_bytes > memory_budget_bytes
            while in_flight and (
                len(in_flight) >= max_workers
                or (not oversized and used_bytes + estimated_bytes > memory_budget_bytes)
            ):
                if len(in_flight) >= max_workers:
                    concurrency_waits += 1
                if not oversized and used_bytes + estimated_bytes > memory_budget_bytes:
                    budget_waits.append(
                        {
                            "screen_key": descriptor["order_key"][0],
                            "shard_id": descriptor["shard_id"],
                            "estimated_bytes": estimated_bytes,
                            "used_bytes": used_bytes,
                            "budget_bytes": memory_budget_bytes,
                            "waiting_order_index": index,
                        }
                    )
                done, _not_done = wait(tuple(in_flight), return_when=FIRST_COMPLETED)
                for future in done:
                    item_index, reserved = in_flight.pop(future)
                    used_bytes -= reserved
                    shard_results[item_index] = future.result()

            reserved = estimated_bytes
            used_bytes += reserved
            peak_in_flight_bytes = max(peak_in_flight_bytes, used_bytes)
            future = executor.submit(_load_one_shard, descriptor)
            in_flight[future] = (index, reserved)
            peak_worker_count = max(peak_worker_count, len(in_flight))

        while in_flight:
            done, _not_done = wait(tuple(in_flight), return_when=FIRST_COMPLETED)
            for future in done:
                item_index, reserved = in_flight.pop(future)
                used_bytes -= reserved
                shard_results[item_index] = future.result()

    shards = [item for item in shard_results if item is not None]
    records: List[SnapshotRecord] = []
    failures: List[Dict[str, Any]] = []
    for shard in shards:
        if shard["valid"]:
            for raw_record in shard["records"]:
                records.append(SnapshotRecord(**raw_record))
        else:
            for failure in shard["failures"]:
                failures.append(
                    {
                        "code": "E_DIGEST",
                        "shard_id": shard["shard_id"],
                        "source": shard["path"],
                        **failure,
                    }
                )

    return {
        "shards": shards,
        "records": records,
        "failures": failures,
        "load_stats": {
            "configured_workers": max_workers,
            "peak_worker_count": peak_worker_count,
            "memory_budget_bytes": memory_budget_bytes,
            "peak_in_flight_bytes": peak_in_flight_bytes,
            "budget_wait_count": len(budget_waits),
            "concurrency_wait_count": concurrency_waits,
            "budget_waits": budget_waits,
            "deterministic_order_keys": [item["order_key"] for item in ordered],
        },
    }


def _candidate_sort_key(record: SnapshotRecord) -> Tuple[int, int, int, str]:
    source_rank = 2 if record.source_kind == "v2" else 1
    return (record.generation, record.logic_ms, source_rank, record.source_locator)


def merge_snapshots(
    v1_records: List[SnapshotRecord],
    v2_result: Dict[str, Any],
) -> Dict[str, Any]:
    valid_v2_records: List[SnapshotRecord] = v2_result.get("records", [])
    candidates = list(v1_records) + list(valid_v2_records)
    groups: Dict[Tuple[str, int], List[SnapshotRecord]] = {}
    for record in candidates:
        groups.setdefault((record.canonical_screen_id, record.logic_ms), []).append(record)

    points: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    field_names = ("generation", "keyframe_seq", "play_position_ms")

    for (canonical_id, logic_ms), group in sorted(groups.items(), key=lambda item: (item[0][1], item[0][0])):
        tombstones = [record for record in group if record.tombstone]
        if tombstones:
            winner = max(tombstones, key=_candidate_sort_key)
            adjudication = "tombstone_priority"
        else:
            winner = max(group, key=_candidate_sort_key)
            top_key = _candidate_sort_key(winner)[:2]
            same_gen_time = [
                record for record in group
                if (record.generation, record.logic_ms) == top_key
            ]
            adjudication = "generation_then_logic_ms" if len(same_gen_time) == 1 else "deterministic_source_tie_break"

        winner_dict = winner.to_dict()
        points.append(
            {
                "canonical_screen_id": canonical_id,
                "logic_ms": logic_ms,
                "winner": winner_dict,
                "candidate_count": len(group),
                "adjudication": adjudication,
            }
        )

        for field_name in field_names:
            values = [getattr(record, field_name) for record in group]
            if len({json.dumps(value, sort_keys=True) for value in values}) <= 1:
                continue
            field_candidates = []
            for record in group:
                field_candidates.append(
                    {
                        "value": getattr(record, field_name),
                        "source_kind": record.source_kind,
                        "source_locator": record.source_locator,
                        "shard_id": record.shard_id,
                        "generation": record.generation,
                        "logic_ms": record.logic_ms,
                        "tombstone": record.tombstone,
                    }
                )
            conflicts.append(
                {
                    "conflict_key": f"{logic_ms}:{canonical_id}:{field_name}",
                    "logic_ms": logic_ms,
                    "canonical_screen_id": canonical_id,
                    "field": field_name,
                    "selected_value": getattr(winner, field_name),
                    "winner_source": winner.source_locator,
                    "winner_shard_id": winner.shard_id,
                    "adjudication": adjudication,
                    "candidates": field_candidates,
                    "source_shard_id": winner.shard_id,
                    "source_locator": winner.source_locator,
                }
            )

    conflicts.sort(key=lambda item: (item["logic_ms"], item["canonical_screen_id"], item["field"]))
    points.sort(key=lambda item: (item["logic_ms"], item["canonical_screen_id"]))

    current: Dict[str, Dict[str, Any]] = {}
    for point in points:
        canonical_id = point["canonical_screen_id"]
        winner = point["winner"]
        existing = current.get(canonical_id)
        if existing is None or (winner["generation"], winner["logic_ms"]) >= (
            existing["generation"],
            existing["logic_ms"],
        ):
            current[canonical_id] = winner

    fallbacks = _build_shard_fallbacks(v2_result, valid_v2_records)
    return {
        "points": points,
        "current": current,
        "conflicts": conflicts,
        "digest_failures": v2_result.get("failures", []),
        "shard_fallbacks": fallbacks,
        "shard_load_stats": v2_result.get("load_stats", {}),
        "version_identification": {
            "v1_present": bool(v1_records),
            "v1_format": "flat-json/relative-seconds/no-generation",
            "v1_implicit_generation": 1,
            "v2_present": bool(v2_result.get("shards")),
            "v2_format": "sharded-json+sqlite-metadata/canonical-id/absolute-logic-ms",
            "v2_shard_count": len(v2_result.get("shards", [])),
            "digest_algorithm": "sha256(canonical_json)",
        },
        "shards": [
            {key: value for key, value in shard.items() if key != "records"}
            for shard in v2_result.get("shards", [])
        ],
    }


def _build_shard_fallbacks(v2_result: Dict[str, Any], valid_records: List[SnapshotRecord]) -> List[Dict[str, Any]]:
    fallbacks: List[Dict[str, Any]] = []
    valid_by_screen: Dict[str, List[SnapshotRecord]] = {}
    for record in valid_records:
        valid_by_screen.setdefault(record.canonical_screen_id, []).append(record)
    for records in valid_by_screen.values():
        records.sort(key=lambda record: (record.logic_ms, record.generation), reverse=True)

    for shard in v2_result.get("shards", []):
        if shard.get("valid"):
            continue
        shard_logic_ms = shard.get("logic_ms")
        screen_ids = shard.get("screen_ids") or shard.get("db_screen_ids") or []
        for canonical_id in screen_ids:
            candidates = valid_by_screen.get(canonical_id, [])
            prior = None
            if isinstance(shard_logic_ms, int):
                for record in candidates:
                    if record.logic_ms <= shard_logic_ms:
                        prior = record
                        break
            elif candidates:
                prior = candidates[0]
            fallbacks.append(
                {
                    "invalid_shard_id": shard.get("shard_id"),
                    "invalid_source": shard.get("path"),
                    "canonical_screen_id": canonical_id,
                    "fallback_shard_id": prior.shard_id if prior else None,
                    "fallback_source_locator": prior.source_locator if prior else None,
                    "fallback_logic_ms": prior.logic_ms if prior else None,
                    "fallback_generation": prior.generation if prior else None,
                    "available": prior is not None,
                }
            )
    fallbacks.sort(key=lambda item: (str(item["invalid_shard_id"]), item["canonical_screen_id"]))
    return fallbacks


def load_and_merge_snapshots(
    sample_dir: Path | str,
    manifest: Any,
    *,
    max_workers: int | None = None,
    memory_budget_bytes: int = DEFAULT_MEMORY_BUDGET_BYTES,
) -> Tuple[List[SnapshotRecord], Dict[str, Any]]:
    sample_dir = Path(sample_dir)
    v1_path = sample_dir / "snapshots" / "v1.json"
    v1_raw = load_json_file(v1_path)
    v1_records = _load_v1(v1_raw, manifest, "snapshots/v1.json")
    v2_result = load_v2_snapshot(
        sample_dir / "snapshots" / "v2",
        max_workers=max_workers,
        memory_budget_bytes=memory_budget_bytes,
    )
    merged = merge_snapshots(v1_records, v2_result)
    return v1_records, merged


def _load_v1(raw: Any, manifest: Any, source: str) -> List[SnapshotRecord]:
    from .validation import parse_v1_snapshot

    return parse_v1_snapshot(raw, manifest, source)
