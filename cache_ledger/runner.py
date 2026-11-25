from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from .engine import (
    Usage,
    admission_layers,
    entry_heat,
    fits,
    layer_over_threshold,
    migration_cost,
    policy_target,
    threshold_blocks,
    victim_sort_key,
)
from .errors import CacheLedgerError, PolicyRollbackError
from .merge import build_merged_view, build_v1_only_view, compute_closure
from .models import SampleBundle
from .store import LedgerStore, canonical_json


def request_clock(bundle: SampleBundle, request: dict[str, Any]) -> int:
    clock = request.get("clock_ms")
    if clock is None:
        command = request.get("command")
        if command == "merge":
            clock = bundle.capacity.merge_clock_ms
        elif command == "rebalance":
            clock = bundle.capacity.default_clock_ms + bundle.capacity.cooldown_window_ms + 1_000
        elif command == "evict":
            clock = bundle.capacity.default_clock_ms + bundle.capacity.cooldown_window_ms + 2_000
        else:
            clock = bundle.capacity.default_clock_ms
    if not isinstance(clock, int) or isinstance(clock, bool) or clock < 0:
        raise CacheLedgerError("E_INVALID_CLOCK", "clock_ms must be a non-negative integer", field="clock_ms")
    return clock


def stored_view(store: LedgerStore) -> dict[str, Any]:
    pointer = store.get_meta("policy_pointer_fingerprint")
    if not pointer:
        raise CacheLedgerError("E_POLICY_NOT_MERGED", "run merge before this command")
    row = store.conn.execute("SELECT view_json FROM policy_versions WHERE fingerprint=?", (pointer,)).fetchone()
    if row is None:
        raise CacheLedgerError("E_POLICY_POINTER_MISSING", "policy pointer does not match a stored version")
    return json.loads(row["view_json"])


def current_usage(store: LedgerStore) -> Usage:
    tenant_blocks: dict[str, int] = {}
    tenant_bytes: dict[str, int] = {}
    for row in store.conn.execute("SELECT * FROM tenant_usage"):
        tenant_blocks[row["tenant"]] = int(row["used_blocks"])
        tenant_bytes[row["tenant"]] = int(row["used_bytes"])
    layer_blocks: dict[str, int] = {}
    layer_bytes: dict[str, int] = {}
    for row in store.conn.execute("SELECT * FROM layer_usage"):
        layer_blocks[row["layer"]] = int(row["used_blocks"])
        layer_bytes[row["layer"]] = int(row["used_bytes"])
    total = int(store.conn.execute("SELECT COALESCE(SUM(used_bytes),0) AS b FROM tenant_usage").fetchone()["b"])
    return Usage(tenant_blocks, tenant_bytes, layer_blocks, layer_bytes, total)


def candidate_rows(
    store: LedgerStore,
    bundle: SampleBundle,
    view: Optional[dict[str, Any]],
    *,
    admitted_only: bool = True,
    tenant: Optional[str] = None,
) -> list[dict[str, Any]]:
    sql = """
        SELECT e.*, t.eviction_priority_weight
        FROM entries e JOIN tenants t ON t.name=e.tenant
    """
    clauses: list[str] = []
    params: list[Any] = []
    if admitted_only:
        clauses.append("e.status='admitted'")
    if tenant:
        clauses.append("e.tenant=?")
        params.append(tenant)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY e.seq"
    policies = (view or {}).get("policies", {})
    result: list[dict[str, Any]] = []
    for row in store.conn.execute(sql, params).fetchall():
        item = dict(row)
        item["heat_score"] = entry_heat(item)
        policy = policies.get(item["key"], {})
        item["policy_tombstone"] = bool(policy.get("tombstone", False))
        item["policy_invalid"] = bool(policy.get("invalid", False))
        result.append(item)
    return result


def crash_after_commits(store: LedgerStore, initial_count: int) -> None:
    raw = os.environ.get("CACHE_LEDGER_CRASH_AFTER_COMMITS")
    current = int(store.conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"])
    committed = current - initial_count
    if raw and committed >= int(raw):
        os._exit(86)
    hang_raw = os.environ.get("CACHE_LEDGER_HANG_AFTER_COMMITS")
    if hang_raw and committed >= int(hang_raw):
        marker = os.environ.get("CACHE_LEDGER_HANG_MARKER")
        if marker:
            Path(marker).write_text(str(os.getpid()), encoding="utf-8")
        import threading

        threading.Event().wait(120)


def build_admission_plan(bundle: SampleBundle, view: dict[str, Any], clock: int) -> dict[str, dict[str, Any]]:
    """Build the order-independent allocation plan for every stream entry.

    Tenant worker processes all derive the same plan from seq order, then commit
    only their tenant shard.  Every planned admitted set fits all hard limits,
    so arbitrary process interleavings cannot overcommit or change payloads.
    """
    planned_usage = Usage({}, {}, {}, {}, 0)
    plan: dict[str, dict[str, Any]] = {}
    for entry in bundle.entries:
        policy = view["policies"][entry.key]
        desired_layer, reasons = policy_target(policy, entry, clock, bundle.capacity)
        if desired_layer is None:
            plan[entry.key] = {
                "action": "deny",
                "reason": "tombstone",
                "reasons": reasons,
                "policy": policy,
            }
            continue
        selected_layer: Optional[str] = None
        failure_reason = "no_feasible_layer"
        for layer in admission_layers(desired_layer):
            ok, reason = fits(
                entry=entry,
                layer=layer,
                tenants=bundle.tenants,
                capacity=bundle.capacity,
                usage=planned_usage,
            )
            if ok:
                selected_layer = layer
                break
            failure_reason = reason or failure_reason
        if selected_layer is None:
            plan[entry.key] = {
                "action": "defer",
                "reason": failure_reason,
                "desired_layer": desired_layer,
                "reasons": reasons,
                "policy": policy,
            }
            continue
        blocks = entry.block_size
        bytes_used = blocks * bundle.capacity.block_bytes
        tenant = entry.tenant
        planned_usage.tenant_blocks[tenant] = planned_usage.tenant_blocks.get(tenant, 0) + blocks
        planned_usage.tenant_bytes[tenant] = planned_usage.tenant_bytes.get(tenant, 0) + bytes_used
        planned_usage.layer_blocks[selected_layer] = planned_usage.layer_blocks.get(selected_layer, 0) + blocks
        planned_usage.layer_bytes[selected_layer] = planned_usage.layer_bytes.get(selected_layer, 0) + bytes_used
        planned_usage.total_bytes += bytes_used
        movement = "hold"
        if selected_layer < entry.declared_layer:
            movement = "promote"
        elif selected_layer > entry.declared_layer:
            movement = "demote"
        plan[entry.key] = {
            "action": "admit",
            "layer": selected_layer,
            "requested_layer": desired_layer,
            "movement": movement,
            "reasons": reasons,
            "policy": policy,
        }
    return plan


def run_merge(store: LedgerStore, bundle: SampleBundle, request: dict[str, Any]) -> dict[str, Any]:
    clock = request_clock(bundle, request)
    store.check_clock(clock, request.get("clock_tolerance_ms"))
    view = build_merged_view(bundle, clock)
    old_view = build_v1_only_view(bundle, clock)
    closure = compute_closure(bundle, old_view, view)

    patch_path = request.get("patch_path")
    previous_view: Optional[dict[str, Any]] = None
    if store.get_meta("policy_pointer_fingerprint"):
        previous_view = stored_view(store)
    if patch_path:
        patch = json.loads(Path(patch_path).read_text(encoding="utf-8"))
        base = previous_view or view
        new_policies = {key: dict(policy) for key, policy in base["policies"].items()}
        for key, changes in (patch.get("policies") or {}).items():
            if key not in new_policies:
                raise CacheLedgerError("E_UNDECLARED_POLICY_KEY", "patch references unknown key", key=key)
            policy = dict(new_policies[key])
            policy.update(changes)
            policy["key"] = key
            new_policies[key] = policy
        view = dict(base)
        view["policies"] = new_policies
        view["policy_fingerprint"] = request.get("fingerprint") or (
            "patch-" + canonical_json({"clock": clock, "patch": patch})
        )
        if "generation" in patch:
            view["v2_snapshot_generation"] = int(patch["generation"])
        if previous_view is not None:
            current_layers = {
                row["key"]: row["current_layer"]
                for row in store.conn.execute("SELECT key,current_layer FROM entries WHERE current_layer IS NOT NULL")
            }
            closure = compute_closure(
                bundle,
                previous_view["policies"],
                view,
                current_layers=current_layers,
                extra_changed_keys=set((patch.get("policies") or {}).keys()),
            )

    committed_generation = store.get_meta("policy_generation")
    if committed_generation is not None and int(view.get("v2_snapshot_generation", 0)) < int(committed_generation):
        raise PolicyRollbackError(int(committed_generation), int(view["v2_snapshot_generation"]))

    fingerprint = view["policy_fingerprint"]
    existing = store.find_decision(f"policy-merge:{fingerprint}")
    if existing is not None:
        return {
            "ok": True,
            "idempotent": True,
            "decision_seq": existing["seq"],
            "clock_ms": clock,
            "view": view,
            "closure": closure,
        }
    intent = {
        "idempotency_key": f"policy-merge:{fingerprint}",
        "kind": "policy_merge",
        "clock_ms": clock,
        "policy_fingerprint": fingerprint,
        "payload": {"view": view, "closure": closure},
    }
    initial = int(store.conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"])
    decision = store.execute_intent(intent)
    crash_after_commits(store, initial)
    store.record_report("merge", "global", clock, {"closure": closure, "fingerprint": fingerprint})
    return {"ok": True, "decision_seq": decision["seq"], "clock_ms": clock, "view": view, "closure": closure}


def run_admit(store: LedgerStore, bundle: SampleBundle, request: dict[str, Any]) -> dict[str, Any]:
    clock = request_clock(bundle, request)
    store.check_clock(clock, request.get("clock_tolerance_ms"))
    view = stored_view(store)
    policy_fingerprint = view["policy_fingerprint"]
    tenant_filter = request.get("tenant")
    if tenant_filter and tenant_filter not in bundle.tenants:
        raise CacheLedgerError("E_UNDECLARED_TENANT", "unknown --tenant", tenant=tenant_filter)
    sql = "SELECT * FROM entries WHERE status IN ('new','deferred')"
    params: list[Any] = []
    if tenant_filter:
        sql += " AND tenant=?"
        params.append(tenant_filter)
    sql += " ORDER BY seq"
    rows = store.conn.execute(sql, params).fetchall()
    initial_count = int(store.conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"])
    admission_plan = build_admission_plan(bundle, view, clock)
    entries_by_key = {entry.key: entry for entry in bundle.entries}
    admitted: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    denied: list[dict[str, Any]] = []

    for row in rows:
        entry = entries_by_key[row["key"]]
        planned = admission_plan[entry.key]
        policy = planned["policy"]
        if planned["action"] == "deny":
            intent = {
                "idempotency_key": f"deny:{entry.key}:{policy_fingerprint[:12]}",
                "kind": "deny",
                "entry_key": entry.key,
                "tenant": entry.tenant,
                "clock_ms": clock,
                "policy_fingerprint": policy_fingerprint,
                "payload": {"key": entry.key, "reason": "tombstone", "reasons": planned["reasons"]},
                "mutation_type": "deny",
            }
            decision = store.execute_intent(intent)
            denied.append({"key": entry.key, "seq": decision["seq"], "reason": "tombstone"})
            crash_after_commits(store, initial_count)
            continue

        if planned["action"] == "defer":
            idem = f"defer:{entry.key}:{clock}:{policy_fingerprint[:12]}"
            intent = {
                "idempotency_key": idem,
                "kind": "defer",
                "entry_key": entry.key,
                "tenant": entry.tenant,
                "clock_ms": clock,
                "policy_fingerprint": policy_fingerprint,
                "payload": {
                    "key": entry.key,
                    "reason": planned["reason"],
                    "desired_layer": planned["desired_layer"],
                },
            }
            decision = store.execute_intent(intent)
            deferred.append({"key": entry.key, "seq": decision["seq"], "reason": planned["reason"]})
            crash_after_commits(store, initial_count)
            continue

        selected_layer = planned["layer"]
        movement = planned["movement"]
        intent = {
            "idempotency_key": f"admit:{entry.key}",
            "kind": "admit",
            "entry_key": entry.key,
            "tenant": entry.tenant,
            "clock_ms": clock,
            "policy_fingerprint": policy_fingerprint,
            "payload": {
                "key": entry.key,
                "layer": selected_layer,
                "requested_layer": planned["requested_layer"],
                "declared_layer": entry.declared_layer,
                "movement": movement,
                "reasons": planned["reasons"],
                "block_size": entry.block_size,
                "size_bytes": entry.block_size * bundle.capacity.block_bytes,
                "policy_source": policy.get("source", "default"),
                "policy_generation": int(policy.get("generation", 0)),
            },
        }
        decision = store.execute_intent(intent)
        admitted.append({"key": entry.key, "seq": decision["seq"], "layer": selected_layer, "movement": movement})
        crash_after_commits(store, initial_count)

    result = {
        "ok": True,
        "command": "admit",
        "clock_ms": clock,
        "tenant": tenant_filter,
        "admitted": admitted,
        "deferred": deferred,
        "denied": denied,
        "counts": {"admitted": len(admitted), "deferred": len(deferred), "denied": len(denied)},
    }
    store.record_report("admit", tenant_filter or "global", clock, result)
    return result


def _evict_one(
    store: LedgerStore,
    bundle: SampleBundle,
    candidate: dict[str, Any],
    clock: int,
    reason: str,
    policy_fingerprint: str,
) -> dict[str, Any]:
    layer = candidate["current_layer"]
    intent = {
        "idempotency_key": f"evict:{candidate['key']}",
        "kind": "evict",
        "entry_key": candidate["key"],
        "tenant": candidate["tenant"],
        "clock_ms": clock,
        "policy_fingerprint": policy_fingerprint,
        "payload": {
            "key": candidate["key"],
            "layer": layer,
            "block_size": int(candidate["block_size"]),
            "size_bytes": int(candidate["size_bytes"]),
            "reason": reason,
        },
    }
    decision = store.execute_intent(intent)
    return {"key": candidate["key"], "seq": decision["seq"], "layer": layer, "tenant": candidate["tenant"], "reason": reason}


def run_evict(store: LedgerStore, bundle: SampleBundle, request: dict[str, Any]) -> dict[str, Any]:
    clock = request_clock(bundle, request)
    store.check_clock(clock, request.get("clock_tolerance_ms"))
    view = stored_view(store)
    fingerprint = view["policy_fingerprint"]
    tenant_filter = request.get("tenant")
    initial_count = int(store.conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"])
    evicted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    candidates = candidate_rows(store, bundle, view, tenant=tenant_filter)
    tombstones = sorted([row for row in candidates if row["policy_tombstone"]], key=victim_sort_key)
    for candidate in tombstones:
        result = _evict_one(store, bundle, candidate, clock, "policy_tombstone", fingerprint)
        evicted.append(result)
        crash_after_commits(store, initial_count)

    tenants = [bundle.tenants[tenant_filter]] if tenant_filter else [bundle.tenants[name] for name in sorted(bundle.tenants)]
    for tenant in tenants:
        while True:
            usage_row = store.conn.execute("SELECT * FROM tenant_usage WHERE tenant=?", (tenant.name,)).fetchone()
            if int(usage_row["used_blocks"]) <= tenant.soft_quota_blocks:
                break
            rows = candidate_rows(store, bundle, view, tenant=tenant.name)
            if not rows:
                rejected.append({"tenant": tenant.name, "reason": "soft_quota_requires_eviction_but_no_candidate"})
                break
            victim = sorted(rows, key=victim_sort_key)[0]
            result = _evict_one(store, bundle, victim, clock, "soft_quota", fingerprint)
            evicted.append(result)
            crash_after_commits(store, initial_count)

    capacity = bundle.capacity
    budget_limit = capacity.total_byte_budget * capacity.byte_pressure_pct // 100
    target_limit = capacity.total_byte_budget * capacity.byte_target_pct // 100
    total_bytes = current_usage(store).total_bytes
    if total_bytes > budget_limit and not tenant_filter:
        while current_usage(store).total_bytes > target_limit:
            rows = candidate_rows(store, bundle, view)
            if not rows:
                rejected.append({"reason": "byte_budget_requires_eviction_but_no_candidate"})
                break
            victim = sorted(rows, key=victim_sort_key)[0]
            result = _evict_one(store, bundle, victim, clock, "byte_budget_pressure", fingerprint)
            evicted.append(result)
            crash_after_commits(store, initial_count)

    result = {
        "ok": True,
        "command": "evict",
        "clock_ms": clock,
        "tenant": tenant_filter,
        "evicted": evicted,
        "rejected_candidates": rejected,
        "counts": {"evicted": len(evicted), "rejected": len(rejected)},
    }
    store.record_report("evict", tenant_filter or "global", clock, result)
    return result


def run_rebalance(store: LedgerStore, bundle: SampleBundle, request: dict[str, Any]) -> dict[str, Any]:
    clock = request_clock(bundle, request)
    store.check_clock(clock, request.get("clock_tolerance_ms"))
    view = stored_view(store)
    fingerprint = view["policy_fingerprint"]
    tenant_filter = request.get("tenant")
    budget_ms = int(request.get("time_budget_ms") or bundle.capacity.rebalance_time_budget_ms)
    if budget_ms < 0:
        raise CacheLedgerError("E_INVALID_BUDGET", "time_budget_ms must be non-negative")
    initial_count = int(store.conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"])
    usage = current_usage(store)
    total_bytes = usage.total_bytes
    byte_pressure = total_bytes * 100 > bundle.capacity.total_byte_budget * bundle.capacity.byte_pressure_pct
    remaining_budget = budget_ms
    migrated: list[dict[str, Any]] = []
    not_started: list[dict[str, Any]] = []
    failed_after_start: list[dict[str, Any]] = []
    abandoned: list[dict[str, Any]] = []

    for source_layer in ("L1", "L2"):
        used_blocks = usage.layer_blocks[source_layer]
        over = layer_over_threshold(bundle.capacity, source_layer, used_blocks)
        effective_threshold_pct = bundle.capacity.rebalance_threshold
        if byte_pressure and not over:
            effective_threshold_pct = max(0, effective_threshold_pct - 10)
            limit = bundle.capacity.layer_max_blocks[source_layer]
            over = used_blocks * 100 > limit * effective_threshold_pct
        if not over:
            continue
        target_layer = "L2" if source_layer == "L1" else "L3"
        source_limit = bundle.capacity.layer_max_blocks[source_layer]

        def source_still_over() -> bool:
            used = usage.layer_blocks[source_layer]
            if bundle.capacity.threshold_unit == "blocks":
                return layer_over_threshold(bundle.capacity, source_layer, used)
            return used * 100 > source_limit * effective_threshold_pct

        pending = sorted(
            candidate_rows(store, bundle, view, tenant=tenant_filter),
            key=victim_sort_key,
        )
        pending = [row for row in pending if row["current_layer"] == source_layer]
        processed: set[str] = set()
        while pending:
            if not source_still_over():
                for candidate in pending:
                    if candidate["key"] not in processed:
                        not_started.append({
                            "key": candidate["key"], "tenant": candidate["tenant"], "layer": source_layer,
                            "reason": "source_within_threshold",
                        })
                        processed.add(candidate["key"])
                break
            startable: Optional[dict[str, Any]] = None
            for candidate in pending:
                key = candidate["key"]
                if key in processed:
                    continue
                last_tick = int(candidate["last_migration_clock_ms"] or candidate["last_change_clock_ms"] or 0)
                elapsed = clock - last_tick
                if elapsed < bundle.capacity.cooldown_window_ms:
                    not_started.append({
                        "key": key, "tenant": candidate["tenant"], "layer": source_layer,
                        "reason": "cooldown_window", "elapsed_ms": elapsed,
                        "cooldown_window_ms": bundle.capacity.cooldown_window_ms,
                    })
                    processed.add(key)
                    continue
                cost = migration_cost(bundle.capacity, int(candidate["block_size"]))
                if cost > remaining_budget:
                    abandoned.append({
                        "key": key, "tenant": candidate["tenant"], "layer": source_layer,
                        "from_layer": source_layer, "to_layer": target_layer,
                        "reason": "BUDGET_EXHAUSTED",
                        "cost_ms": cost, "remaining_budget_ms": remaining_budget,
                        "detail": "next priority candidate cost does not fit remaining indivisible logical budget",
                    })
                    processed.add(key)
                    continue
                target_used = usage.layer_blocks[target_layer]
                if target_used + int(candidate["block_size"]) > bundle.capacity.layer_max_blocks[target_layer]:
                    not_started.append({
                        "key": key, "tenant": candidate["tenant"], "layer": source_layer,
                        "target_layer": target_layer, "reason": "target_layer_capacity",
                    })
                    processed.add(key)
                    continue
                startable = candidate
                break

            if startable is None:
                for candidate in pending:
                    if candidate["key"] in processed:
                        continue
                    cost = migration_cost(bundle.capacity, int(candidate["block_size"]))
                    abandoned.append({
                        "key": candidate["key"], "tenant": candidate["tenant"],
                        "from_layer": source_layer, "to_layer": target_layer,
                        "reason": "BUDGET_EXHAUSTED", "cost_ms": cost,
                        "remaining_budget_ms": remaining_budget,
                    })
                    processed.add(candidate["key"])
                break

            key = startable["key"]
            cost = migration_cost(bundle.capacity, int(startable["block_size"]))
            intent = {
                "idempotency_key": f"migrate:{key}:{target_layer}",
                "kind": "rebalance",
                "entry_key": key,
                "tenant": startable["tenant"],
                "clock_ms": clock,
                "policy_fingerprint": fingerprint,
                "payload": {
                    "key": key,
                    "from_layer": source_layer,
                    "to_layer": target_layer,
                    "block_size": int(startable["block_size"]),
                    "size_bytes": int(startable["size_bytes"]),
                    "cost_ms": cost,
                    "priority_key": victim_sort_key(startable),
                    "policy_source": startable.get("policy_source"),
                    "policy_generation": int(startable.get("policy_generation") or 0),
                },
            }
            try:
                decision = store.execute_intent(intent, fail_after_start=bool(request.get("fail_key") == key))
            except CacheLedgerError as exc:
                failed_after_start.append({
                    "key": key, "tenant": startable["tenant"], "from_layer": source_layer,
                    "to_layer": target_layer, "error_code": exc.code, "reason": exc.message,
                })
                processed.add(key)
                usage = current_usage(store)
                continue
            migrated.append({
                "key": key, "seq": decision["seq"], "tenant": startable["tenant"],
                "from_layer": source_layer, "to_layer": target_layer, "cost_ms": cost,
            })
            remaining_budget -= cost
            processed.add(key)
            usage.layer_blocks[source_layer] -= int(startable["block_size"])
            usage.layer_blocks[target_layer] += int(startable["block_size"])
            crash_after_commits(store, initial_count)

        if threshold_blocks(bundle.capacity, source_layer) >= usage.layer_blocks[source_layer]:
            for candidate in pending:
                if candidate["key"] not in processed:
                    not_started.append({
                        "key": candidate["key"], "tenant": candidate["tenant"], "layer": source_layer,
                        "reason": "source_within_threshold",
                    })
                    processed.add(candidate["key"])

    result = {
        "ok": True,
        "command": "rebalance",
        "clock_ms": clock,
        "tenant": tenant_filter,
        "budget_ms": budget_ms,
        "spent_budget_ms": budget_ms - remaining_budget,
        "remaining_budget_ms": remaining_budget,
        "byte_budget_pressure": byte_pressure,
        "migrated": migrated,
        "not_started": not_started,
        "failed_after_start": failed_after_start,
        "abandoned": abandoned,
        "rejected_candidates": not_started + failed_after_start,
        "counts": {
            "migrated": len(migrated),
            "not_started": len(not_started),
            "failed_after_start": len(failed_after_start),
            "abandoned": len(abandoned),
        },
    }
    store.record_report("rebalance", tenant_filter or "global", clock, result)
    return result


DISPATCH = {
    "merge": run_merge,
    "admit": run_admit,
    "rebalance": run_rebalance,
    "evict": run_evict,
}


def execute_command(
    store: LedgerStore,
    bundle: SampleBundle,
    request: dict[str, Any],
    *,
    request_id: Optional[int] = None,
) -> dict[str, Any]:
    command = request["command"]
    if command not in DISPATCH:
        raise CacheLedgerError("E_UNKNOWN_COMMAND", f"unsupported logged command: {command}")
    resolved_clock = request_clock(bundle, request)
    store.check_clock(resolved_clock, request.get("clock_tolerance_ms"))
    request = dict(request)
    request["clock_ms"] = resolved_clock
    own_request_id = request_id is None
    if own_request_id:
        request_id = store.start_command(command, request, resolved_clock)
    try:
        result = DISPATCH[command](store, bundle, request)
        if own_request_id:
            store.finish_command(request_id, "completed", result)
        return result
    except Exception as exc:
        if own_request_id and request_id is not None:
            error = exc.to_dict() if isinstance(exc, CacheLedgerError) else {"code": "E_INTERNAL", "message": str(exc)}
            store.finish_command(request_id, "failed", {"ok": False, "error": error, "clock_ms": request.get("clock_ms", 0)})
        raise


def resume(store: LedgerStore, bundle: SampleBundle) -> dict[str, Any]:
    replayed_pending: list[str] = []
    for row in store.pending_operations("prepared"):
        decision = store.commit_prepared(row["idempotency_key"])
        replayed_pending.append(row["idempotency_key"])

    resumed_commands: list[dict[str, Any]] = []
    for row in store.running_commands():
        request = json.loads(row["request_json"])
        result = execute_command(store, bundle, request, request_id=int(row["request_id"]))
        store.finish_command(int(row["request_id"]), "completed", result)
        resumed_commands.append({"request_id": row["request_id"], "command": row["command"]})

    return {
        "ok": True,
        "command": "resume",
        "half_committed_replayed": replayed_pending,
        "resumed_commands": resumed_commands,
        "status": store.status(),
    }
