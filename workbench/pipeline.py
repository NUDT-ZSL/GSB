from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .canonical import canonical_dumps, sha256_json
from .analytics import latency_summary
from .engine import (
    advance_instance,
    buffer_digest,
    canonical_ledger,
    invalid_rule_closure,
    select_winning_observations,
)
from .errors import WorkbenchError
from .ioformats import (
    load_observations_jsonl,
    load_patch,
    load_registry,
    load_shard_manifest,
    load_shard_records,
)
from .rules import apply_patch_to_view, load_v1_rules, load_v2_bundle, merge_rule_sources
from .storage import Storage


def cmd_init_registry(db_path: str | Path, registry_path: str | Path, clock_ms: int = 0) -> dict[str, Any]:
    registry = load_registry(registry_path)
    with Storage(db_path) as store:
        changes = store.upsert_registry(registry)
        result = reconcile_store(store, clock_ms=clock_ms, reason="registry_loaded")
    return {"registry_hash": registry["content_hash"], "changes": changes, "ledger": result}


def cmd_load_observations(db_path: str | Path, observations_path: str | Path) -> dict[str, Any]:
    records = load_observations_jsonl(observations_path)
    with Storage(db_path) as store:
        result = store.store_observations(records, source_shard=Path(observations_path).name)
    return result


def cmd_load_shard_manifest(db_path: str | Path, manifest_path: str | Path, only_shard: str | None = None) -> dict[str, Any]:
    manifest = load_shard_manifest(manifest_path, verify_files=True)
    with Storage(db_path) as store:
        store.store_shard_manifest(manifest)
        loaded: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        for shard in manifest["shards"]:
            if only_shard and shard["shard_id"] != only_shard:
                continue
            if not shard["exists"]:
                missing.append({"shard_id": shard["shard_id"], "path": shard["path"], "coverage": shard.get("coverage", [])})
                continue
            records = load_shard_records(shard)
            batch_id = f"shard:{shard['shard_id']}:{shard['content_digest'][:16]}"
            stored = store.store_observations(records, source_shard=shard["shard_id"], batch_id=batch_id)
            store.mark_shard_loaded(shard, len(records))
            loaded.append({"shard_id": shard["shard_id"], "records": len(records), **stored})
    return {"manifest": manifest["path"], "loaded": loaded, "missing": missing, "complete": not missing}


def cmd_merge(
    db_path: str | Path | None,
    *,
    v1_path: str | Path | None,
    v2_manifest_path: str | Path | None,
    clock_ms: int,
) -> dict[str, Any]:
    v1 = load_v1_rules(v1_path) if v1_path else None
    v2 = load_v2_bundle(v2_manifest_path) if v2_manifest_path else None
    view = merge_rule_sources(v1, v2, injection_clock_ms=clock_ms)
    if db_path:
        with Storage(db_path) as store:
            store.save_rule_candidates(view)
            with store.transaction() as conn:
                store.ensure_rule_revisions(conn, view)
                conn.execute(
                    "INSERT OR IGNORE INTO rule_views(content_hash,view_json,clock_ms) VALUES(?,?,?)",
                    (view["content_hash"], canonical_dumps(view), clock_ms),
                )
                store.set_meta("current_rule_view_json", canonical_dumps(view), conn)
                store.set_meta("current_rule_view_hash", view["content_hash"], conn)
    return view


def _commit_limits(shards: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, list[dict[str, Any]]], list[str]]:
    limits: dict[str, int] = {}
    blocked: dict[str, list[dict[str, Any]]] = {}
    missing_ids: list[str] = []
    for shard in shards:
        if shard["status"] == "loaded":
            continue
        missing_ids.append(shard["shard_id"])
        for coverage in shard.get("coverage", []):
            instance_id = coverage["instance_id"]
            start = coverage.get("start_seq", coverage.get("min_seq"))
            end = coverage.get("end_seq", coverage.get("max_seq"))
            if not isinstance(start, int) or not isinstance(end, int):
                continue
            blocked.setdefault(instance_id, []).append(
                {"shard_id": shard["shard_id"], "start_seq": start, "end_seq": end, "content_digest": shard["content_digest"]}
            )
            limits[instance_id] = min(limits.get(instance_id, start), start)
    commit_limits = {instance_id: start - 1 for instance_id, start in limits.items()}
    for intervals in blocked.values():
        intervals.sort(key=lambda item: (item["start_seq"], item["end_seq"], item["shard_id"]))
    return commit_limits, blocked, sorted(missing_ids)


def _loaded_shards_for_instance(shards: list[dict[str, Any]], instance_id: str) -> list[str]:
    result = []
    for shard in shards:
        if shard["status"] != "loaded":
            continue
        if any(item.get("instance_id") == instance_id for item in shard.get("coverage", [])):
            result.append(shard["shard_id"])
    return sorted(result)


def _enrich_entry(
    entry: dict[str, Any],
    *,
    records_by_instance: dict[str, list[dict[str, Any]]],
    shards: list[dict[str, Any]],
    blocked: dict[str, list[dict[str, Any]]],
    view: dict[str, Any] | None,
) -> dict[str, Any]:
    instance_id = entry["instance_id"]
    all_records = records_by_instance.get(instance_id, [])
    pending = [record for record in all_records if record["seq"] > entry["committed_through_seq"]]
    entry["pending_count"] = len(pending)
    entry["pending_buffer_digest"] = buffer_digest(pending)
    entry["loaded_shards"] = _loaded_shards_for_instance(shards, instance_id)
    entry["blocked_intervals"] = blocked.get(instance_id, [])
    entry["window_complete"] = not entry["blocked_intervals"]

    rule = None
    if view:
        binding = view.get("instance_bindings", {}).get(instance_id)
        if binding:
            rule = view.get("rules", {}).get(binding["rule_key"])
    effective_at = (rule or {}).get("effective_at_ms", 0)
    observation_clocks = [
        decision.get("logical_clock_ms")
        for decision in entry.get("decisions", [])
        if decision.get("kind") == "observation" and isinstance(decision.get("logical_clock_ms"), int)
    ]
    latencies = [
        decision["latency_ms"]
        for decision in entry.get("decisions", [])
        if decision.get("kind") == "observation" and isinstance(decision.get("latency_ms"), int)
    ]
    entry["latency_summary"] = latency_summary(latencies)
    before_effective = sum(1 for clock in observation_clocks if clock < effective_at)
    entry["observation_window"] = {
        "rule_effective_at_ms": effective_at,
        "min_observation_clock_ms": min(observation_clocks) if observation_clocks else None,
        "max_observation_clock_ms": max(observation_clocks) if observation_clocks else None,
        "observations_before_effective": before_effective,
        "observations_after_effective": len(observation_clocks) - before_effective,
        "blocked_interval_count": len(entry["blocked_intervals"]),
        "consistent": before_effective == 0 and not entry["blocked_intervals"],
    }
    return entry


def _group_records(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(record["instance_id"], []).append(record)
    return grouped


def _current_rule_fingerprint(instance_id: str, registry_instance: dict[str, Any], view: dict[str, Any] | None) -> tuple[str, int, str]:
    if not view:
        return "default", 0, sha256_json({"default": True})
    binding = view.get("instance_bindings", {}).get(instance_id)
    rule_key = binding["rule_key"] if binding else None
    rule = view.get("rules", {}).get(rule_key) if rule_key else None
    if rule is None:
        return "__default__", 0, sha256_json({"default": True})
    return rule["rule_key"], rule.get("generation", 0), sha256_json(rule)


def reconcile_store(
    store: Storage,
    *,
    clock_ms: int,
    reason: str = "reconcile",
    touched_instances: set[str] | None = None,
    recomputation_report: dict[str, Any] | None = None,
    patch: dict[str, Any] | None = None,
    view_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    registry = store.get_registry()
    if registry is None:
        raise WorkbenchError("E_REGISTRY_NOT_LOADED", "load registry before reconcile", {})
    view = view_override or store.get_rule_view()
    records = store.get_observation_records()
    grouped = _group_records(records)
    shards = store.get_shards()
    commit_limits, blocked, missing_shard_ids = _commit_limits(shards)
    current_ledger = store.get_ledger()
    old_entries = {item["instance_id"]: item for item in current_ledger["instances"]} if current_ledger else {}

    entries: dict[str, dict[str, Any]] = {}
    actions: dict[str, str] = {}
    source_versions: dict[str, int] = {}
    current_version = store.current_version()

    for instance in registry["instances"]:
        instance_id = instance["instance_id"]
        old_entry = old_entries.get(instance_id)
        limit = commit_limits.get(instance_id)
        if limit is not None and old_entry and limit < old_entry["committed_through_seq"]:
            raise WorkbenchError(
                "E_SHARD_INTERVAL_CONFLICT",
                "missing shard barrier is before already committed sequence",
                {"instance_id": instance_id, "barrier_limit": limit, "committed_through_seq": old_entry["committed_through_seq"]},
            )

        if touched_instances is not None:
            if instance_id in touched_instances or old_entry is None:
                entry = advance_instance(instance, grouped.get(instance_id, []), view, commit_limit_seq=limit)
                action = "recompute" if instance_id in touched_instances else "initial"
            else:
                entry = old_entry
                action = "reuse"
                source_versions[instance_id] = current_version
        else:
            rule_key, rule_generation, rule_hash = _current_rule_fingerprint(instance_id, instance, view)
            rule_changed = bool(
                old_entry
                and (
                    old_entry.get("rule_content_hash") != rule_hash
                    or old_entry.get("lease_generation") != instance["lease_generation"]
                )
            )
            if old_entry is None or rule_changed:
                entry = advance_instance(instance, grouped.get(instance_id, []), view, commit_limit_seq=limit)
                action = "rule_changed_recompute" if rule_changed else "initial"
            else:
                before_hash = sha256_json(old_entry)
                candidate = advance_instance(
                    instance,
                    grouped.get(instance_id, []),
                    view,
                    previous_entry=old_entry,
                    commit_limit_seq=limit,
                )
                candidate = _enrich_entry(
                    candidate,
                    records_by_instance=grouped,
                    shards=shards,
                    blocked=blocked,
                    view=view,
                )
                if sha256_json(candidate) == before_hash:
                    entry = old_entry
                    action = "reuse"
                    source_versions[instance_id] = current_version
                else:
                    entry = candidate
                    action = "incremental"

        entry = _enrich_entry(
            entry,
            records_by_instance=grouped,
            shards=shards,
            blocked=blocked,
            view=view,
        )
        entries[instance_id] = entry
        actions[instance_id] = action

    watermark = max(clock_ms, int(store.get_meta("clock_watermark_ms", "0") or 0))
    candidate_ledger = canonical_ledger(
        entries,
        registry_hash=registry["content_hash"],
        rule_view_hash=(view or {}).get("content_hash"),
        clock_watermark_ms=watermark,
    )
    if current_ledger and candidate_ledger["ledger_hash"] == current_ledger["ledger_hash"]:
        return {
            "idempotent": True,
            "ledger_version": current_version,
            "ledger_hash": current_ledger["ledger_hash"],
            "actions": actions,
            "missing_shards": missing_shard_ids,
            "buffered_batches": [item["batch_id"] for item in store.buffered_batches()],
        }

    batch_ids_to_commit = _committable_batches(store, entries)
    committed = store.commit_ledger(
        entries,
        reason=reason,
        clock_ms=watermark,
        view=view_override,
        patch=patch,
        recomputation=recomputation_report or {"actions": actions},
        entry_actions=actions,
        entry_source_versions=source_versions,
        mark_batch_ids=batch_ids_to_commit,
    )
    return {
        "idempotent": False,
        **committed,
        "actions": actions,
        "missing_shards": missing_shard_ids,
        "committed_batches": batch_ids_to_commit,
        "buffered_batches": [item["batch_id"] for item in store.buffered_batches() if item["batch_id"] not in batch_ids_to_commit],
    }


def _committable_batches(store: Storage, entries: dict[str, dict[str, Any]]) -> list[str]:
    batches = store.buffered_batches()
    result = []
    for batch in batches:
        items = store.conn.execute(
            "SELECT instance_id, seq FROM pending_batch_items WHERE batch_id=?",
            (batch["batch_id"],),
        ).fetchall()
        if all(seq <= entries[instance_id]["committed_through_seq"] for instance_id, seq in items if instance_id in entries):
            result.append(batch["batch_id"])
    return sorted(result)


def cmd_reconcile(db_path: str | Path, clock_ms: int = 0) -> dict[str, Any]:
    with Storage(db_path) as store:
        return reconcile_store(store, clock_ms=clock_ms, reason="reconcile")


def cmd_resume(db_path: str | Path, clock_ms: int = 0) -> dict[str, Any]:
    with Storage(db_path) as store:
        before = store.buffered_batches()
        result = reconcile_store(store, clock_ms=clock_ms, reason="resume")
    return {"recovery": {"buffered_batches_before": before, "crash_point_env": os.environ.get("WB_CRASH_POINT")}, **result}


def cmd_patch(db_path: str | Path, patch_path: str | Path, clock_ms: int) -> dict[str, Any]:
    patch = load_patch(patch_path)
    with Storage(db_path) as store:
        existing = store.patch_exists(patch["patch_id"], patch["content_hash"])
        if existing:
            ledger = store.get_ledger()
            return {"idempotent": True, "stored_result": existing, "ledger_hash": ledger["ledger_hash"] if ledger else None}
        view = store.get_rule_view()
        if view is None:
            raise WorkbenchError("E_RULE_VIEW_MISSING", "run merge --db before patch", {"patch_id": patch["patch_id"]})
        next_view, patch_report = apply_patch_to_view(view, patch, patch_clock_ms=clock_ms)
        touched = set(patch_report["closure"]["affected_instances"])
        store.save_rule_candidates(next_view)
        result = reconcile_store(
            store,
            clock_ms=clock_ms,
            reason=f"patch:{patch['patch_id']}",
            touched_instances=touched,
            recomputation_report=patch_report,
            patch=patch,
            view_override=next_view,
        )
        return {"patch_report": patch_report, "reconcile": result}


def cmd_status(db_path: str | Path) -> dict[str, Any]:
    with Storage(db_path) as store:
        ledger = store.get_ledger()
        if ledger is None:
            raise WorkbenchError("E_LEDGER_EMPTY", "no committed ledger; load registry and reconcile", {"db_path": str(db_path)})
        return ledger


def status_canonical_text(db_path: str | Path) -> str:
    with Storage(db_path) as store:
        row = store.get_ledger_row()
        if row is None:
            raise WorkbenchError("E_LEDGER_EMPTY", "no committed ledger", {"db_path": str(db_path)})
        return row["canonical_json"]


def cmd_status_verbose(db_path: str | Path) -> dict[str, Any]:
    with Storage(db_path) as store:
        ledger = store.get_ledger()
        if ledger is None:
            raise WorkbenchError("E_LEDGER_EMPTY", "no committed ledger", {"db_path": str(db_path)})
        version = store.current_version()
        entry_rows = store.conn.execute(
            "SELECT instance_id, computed_in_version, action, entry_json FROM ledger_entries WHERE ledger_version=? ORDER BY instance_id",
            (version,),
        ).fetchall()
        operational_entries = []
        for row in entry_rows:
            full = json.loads(row["entry_json"])
            operational_entries.append(
                {
                    "instance_id": row["instance_id"],
                    "action": row["action"],
                    "computed_in_version": row["computed_in_version"],
                    "loaded_shards": full.get("loaded_shards", []),
                    "pending_count": full.get("pending_count"),
                    "pending_buffer_digest": full.get("pending_buffer_digest"),
                    "blocked_intervals": full.get("blocked_intervals", []),
                    "window_complete": full.get("window_complete"),
                }
            )
        return {
            "ledger": ledger,
            "operational_state": {
                "current_ledger_version": version,
                "clock_watermark_ms": store.get_meta("clock_watermark_ms"),
                "shards": store.get_shards(),
                "buffered_batches": store.buffered_batches(),
                "instances": operational_entries,
            },
        }
