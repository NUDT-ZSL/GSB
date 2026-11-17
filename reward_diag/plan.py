from __future__ import annotations

import copy
from typing import Any

from .config import ConfigError, apply_config_updates
from .io_utils import read_json, sha256_obj
from .samples import (
    data_fingerprint,
    load_records,
    pair_id_from_entry,
    records_to_entries,
)


def load_plan(path: str) -> dict[str, Any]:
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise ConfigError("E_PLAN_INVALID", "batch plan must be an object")
    batches = raw.get("batches")
    if not isinstance(batches, list):
        raise ConfigError("E_PLAN_INVALID", "batches must be an array")
    previous_ids: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, batch in enumerate(batches):
        if not isinstance(batch, dict):
            raise ConfigError("E_PLAN_INVALID", f"batches[{index}] must be an object")
        batch_id = batch.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise ConfigError("E_PLAN_INVALID", f"batches[{index}].batch_id is invalid")
        if batch_id in previous_ids:
            raise ConfigError("E_PLAN_INVALID", f"duplicate batch_id {batch_id}")
        previous_ids.add(batch_id)

        deletes = batch.get("delete", [])
        if not isinstance(deletes, list) or any(
            not isinstance(item, str) or not item for item in deletes
        ):
            raise ConfigError("E_PLAN_INVALID", f"{batch_id}.delete must be string array")
        if len(set(deletes)) != len(deletes):
            raise ConfigError("E_PLAN_INVALID", f"{batch_id}.delete contains duplicates")

        for field in ("replace", "add"):
            records = batch.get(field, [])
            if not isinstance(records, list) or any(
                not isinstance(item, dict) for item in records
            ):
                raise ConfigError("E_PLAN_INVALID", f"{batch_id}.{field} must be object array")

        replace_ids = {
            record.get("pair_id")
            for record in batch.get("replace", [])
            if isinstance(record.get("pair_id"), str)
        }
        if replace_ids.intersection(deletes):
            raise ConfigError(
                "E_PLAN_INVALID", f"{batch_id} cannot delete and replace the same id"
            )
        normalized = copy.deepcopy(batch)
        normalized.setdefault("delete", [])
        normalized.setdefault("replace", [])
        normalized.setdefault("add", [])
        validated.append(normalized)
    return {"format_version": raw.get("format_version", 1), "batches": validated}


def batch_hash(batch: dict[str, Any]) -> str:
    return sha256_obj(batch)


def apply_batch_to_raw(
    raw_entries: list[dict[str, Any]], batch: dict[str, Any]
) -> list[dict[str, Any]]:
    batch_id = batch["batch_id"]
    target_ids = set(batch.get("delete", []))
    target_ids.update(
        record["pair_id"]
        for record in batch.get("replace", [])
        if isinstance(record.get("pair_id"), str)
    )
    updated = [
        entry
        for entry in raw_entries
        if pair_id_from_entry(entry) not in target_ids
    ]
    updated.extend(records_to_entries(batch.get("replace", []), batch_id, "replace"))
    updated.extend(records_to_entries(batch.get("add", []), batch_id, "add"))
    return updated


def materialize_prefix(
    base_entries: list[dict[str, Any]],
    base_config: dict[str, Any],
    plan: dict[str, Any],
    upto: int,
) -> dict[str, Any]:
    raw_entries = copy.deepcopy(base_entries)
    config = copy.deepcopy(base_config)
    batch_ids: list[str] = []
    batch_hashes: list[str] = []
    for batch in plan["batches"][:upto]:
        raw_entries = apply_batch_to_raw(raw_entries, batch)
        config = apply_config_updates(config, batch)
        batch_ids.append(batch["batch_id"])
        batch_hashes.append(batch_hash(batch))
    loaded = load_records(raw_entries)
    return {
        "raw_entries": raw_entries,
        "config": config,
        "loaded": loaded,
        "data_fingerprint": data_fingerprint(raw_entries, loaded),
        "batch_ids": batch_ids,
        "batch_hashes": batch_hashes,
    }


def materialize_final(
    base_entries: list[dict[str, Any]],
    base_config: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    return materialize_prefix(base_entries, base_config, plan, len(plan["batches"]))
