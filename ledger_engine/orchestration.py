from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

from .engine import compare_sensitive_pairs, compute_service, compute_truth
from .errors import LedgerError
from .jsonutil import hash_json, load_json
from .model import (
    DERIVED_ORDER,
    PARAM_FOR_DERIVED,
    Patch,
    Snapshot,
)
from .persistence import (
    carries_for_service,
    commit_service,
    committed_services,
    create_pending_round,
    current_state,
    ensure_base_loaded,
    entries_by_key,
    finalize_round,
    get_meta_int,
    load_base_snapshot,
    patch_record,
    pending_round,
    record_noop_patch,
    round_info,
    round_result_hash,
    sensitive_pairs,
    service_result,
    snapshot_for_version,
    staged_entry_count,
    stage_entries,
    store_sensitive_pairs,
)
from .validation import apply_patch, changed_sources, load_patch, parse_sample


SAMPLE_FILES = {
    "config": "ledger_config.json",
    "layers": "layers.json",
    "manifest": "manifest.json",
    "topology": "topology.json",
}


def load_sample(sample_dir: str | Path) -> Snapshot:
    root = Path(sample_dir)
    manifest_path = root / SAMPLE_FILES["manifest"]
    seed = 0
    file_names = dict(SAMPLE_FILES)
    if manifest_path.exists():
        manifest = load_json(manifest_path)
        if not isinstance(manifest, dict):
            raise LedgerError("E_BAD_SAMPLE", "sample manifest must be an object")
        raw_seed = manifest.get("seed", 0)
        if isinstance(raw_seed, bool) or not isinstance(raw_seed, int):
            raise LedgerError("E_BAD_SAMPLE", "sample seed must be an integer")
        seed = raw_seed
        if isinstance(manifest.get("files"), dict):
            file_names.update(manifest["files"])
    snapshot = parse_sample(
        str(root / file_names["topology"]),
        str(root / file_names["layers"]),
        str(root / file_names["config"]),
        seed=seed,
    )
    return snapshot


def load_patches(paths: Iterable[str]) -> list[Patch]:
    return normalize_patches(load_patch(path) for path in paths)


def normalize_patches(patches: Iterable[Patch]) -> list[Patch]:
    seen: dict[str, str] = {}
    result: list[Patch] = []
    for patch in patches:
        existing = seen.get(patch.patch_id)
        if existing is not None:
            if existing != patch.patch_hash:
                raise LedgerError(
                    "E_PATCH_CONFLICT",
                    "the same patch_id was supplied with different contents",
                    {"patch_id": patch.patch_id},
                )
            continue
        seen[patch.patch_id] = patch.patch_hash
        result.append(patch)
    return result


def _preflight_patches(
    base: Snapshot, current: Snapshot, patches: list[Patch]
) -> tuple[Snapshot, list[Patch], list[str]]:
    working = current
    applied: list[Patch] = []
    changes: list[str] = []
    for patch in patches:
        before = working
        working = apply_patch(working, base, patch)
        changed = changed_sources(before, working)
        if changed:
            changes.extend(changed)
        applied.append(patch)
    return working, applied, sorted(set(changes))


def _parse_change_source(source: str) -> tuple[str | None, str | None]:
    if not source.startswith("layers."):
        return None, None
    remainder = source[len("layers.") :]
    if "." not in remainder:
        return None, None
    layer, parameter = remainder.rsplit(".", 1)
    return layer, parameter


def build_invalidation_plan(
    connection: Any,
    *,
    snapshot: Snapshot,
    previous_round_id: str | None,
    changes: list[str],
) -> dict[str, Any]:
    ancestors = snapshot.ancestors()
    by_name = snapshot.service_map()
    layer_services: dict[str, set[str]] = {}
    for service in snapshot.services:
        layer_services.setdefault(service.layer, set()).add(service.name)

    layer_changes: list[tuple[str, str]] = []
    topology_changed = False
    for source in changes:
        layer, parameter = _parse_change_source(source)
        if layer is None:
            if source.startswith("services."):
                topology_changed = True
            continue
        layer_changes.append((layer, str(parameter)))

    plan: dict[str, Any] = {}
    affected_services: list[str] = []
    reused_services: list[str] = []
    recomputed_derived: list[dict[str, str]] = []
    reused_derived: list[dict[str, str]] = []

    for service_name in snapshot.topological_order():
        service = by_name[service_name]
        previous_payload = (
            service_result(connection, previous_round_id, service_name)
            if previous_round_id is not None
            else None
        )
        has_previous = previous_payload is not None
        direct_changes: set[str] = set()
        relevant_changes: set[str] = set()
        for layer, parameter in layer_changes:
            source = f"layers.{layer}.{parameter}"
            if any(
                by_name[ancestor].layer == layer
                for ancestor in ancestors[service_name]
            ):
                relevant_changes.add(source)
            if service.layer == layer:
                direct_changes.add(parameter)
        if topology_changed:
            relevant_changes.add("topology")

        recompute: list[str] = []
        reuse: list[str] = []
        for metric in DERIVED_ORDER:
            impacted = (
                not has_previous
                or topology_changed
                or any(
                    parameter in relevant_changes
                    or f"layers.{by_name[ancestor].layer}.{parameter}"
                    in relevant_changes
                    for parameter in PARAM_FOR_DERIVED[metric]
                    for ancestor in ancestors[service_name]
                )
            )
            if impacted:
                recompute.append(metric)
            else:
                reuse.append(metric)
        if not recompute:
            if not has_previous:
                recompute = list(DERIVED_ORDER)
                reuse = []
        if recompute:
            affected_services.append(service_name)
            for metric in recompute:
                recomputed_derived.append(
                    {"derived": metric, "service": service_name}
                )
        else:
            reused_services.append(service_name)
        for metric in reuse:
            reused_derived.append({"derived": metric, "service": service_name})
        plan[service_name] = {
            "changed_parameters": sorted(direct_changes),
            "has_previous": has_previous,
            "recompute_derived": recompute,
            "reuse_derived": reuse,
            "trigger_sources": sorted(relevant_changes),
        }

    return {
        "affected_services": affected_services,
        "decisions": {},
        "previous_round_id": previous_round_id,
        "recomputed_derived": recomputed_derived,
        "reused_derived": reused_derived,
        "reused_ledger_entries": [],
        "reused_services": reused_services,
        "sensitive_pairs": {"recomputed": [], "reused": []},
        "service_plan": plan,
        "topology_changed": topology_changed,
        "trigger_sources": sorted(changes),
    }


def _service_levels(snapshot: Snapshot) -> list[list[str]]:
    by_name = snapshot.service_map()
    level: dict[str, int] = {}
    for name in snapshot.topological_order():
        if not by_name[name].depends_on:
            level[name] = 0
        else:
            level[name] = 1 + max(level[item] for item in by_name[name].depends_on)
    grouped: dict[int, list[str]] = {}
    for name, value in level.items():
        grouped.setdefault(value, []).append(name)
    return [sorted(grouped[key]) for key in sorted(grouped)]


def _crash_after(kind: str, ordinal: int) -> None:
    variable = {
        "staged": "LEDGER_CRASH_AFTER_STAGED_SERVICE",
        "committed": "LEDGER_CRASH_AFTER_COMMITTED_SERVICE",
    }[kind]
    raw = os.environ.get(variable)
    if raw is not None and int(raw) == ordinal:
        os._exit(137)


def run_pending_round(
    connection: Any,
    *,
    workers: int,
    crash_enabled: bool = True,
) -> dict[str, Any]:
    pending = pending_round(connection)
    if pending is None:
        raise LedgerError("E_NO_PENDING", "there is no pending diagnostic round")
    round_id = pending["round_id"]
    invalidations = pending["invalidations"]
    snapshot = snapshot_for_version(connection, pending["version"])
    reuse_base = pending["reuse_base_round"]
    plan = invalidations["service_plan"]
    truth = compute_truth(snapshot)
    payloads: dict[str, dict[str, Any]] = {}
    all_entries: dict[str, dict[str, dict[str, Any]]] = {}
    service_ordinal = 0
    worker_limit = max(
        1,
        min(
            workers,
            snapshot.config.max_concurrency,
        ),
    )

    previous_entry_cache: dict[str, dict[str, dict[str, Any]]] = {}
    if reuse_base is not None:
        for name in snapshot.topological_order():
            previous_entry_cache[name] = entries_by_key(
                connection, reuse_base, name
            )

    for level in _service_levels(snapshot):
        pending_tasks: list[dict[str, Any]] = []
        for service_name in level:
            service_ordinal += 1
            existing = service_result(connection, round_id, service_name)
            if existing is not None:
                payloads[service_name] = existing["payload"]
                continue
            plan_item = plan[service_name]
            previous = (
                service_result(connection, reuse_base, service_name)
                if reuse_base is not None
                else None
            )
            pending_tasks.append(
                {
                    "carry_inputs": carries_for_service(connection, service_name),
                    "changed_parameters": plan_item["changed_parameters"],
                    "ordinal": service_ordinal,
                    "previous_entries": previous_entry_cache.get(service_name, {}),
                    "previous_payload": None if previous is None else previous["payload"],
                    "reuse_metrics": plan_item["reuse_derived"],
                    "service": service_name,
                    "upstream_payloads": dict(payloads),
                }
            )

        if not pending_tasks:
            continue

        def compute(task: dict[str, Any]) -> tuple[str, Any]:
            result = compute_service(
                snapshot,
                task["service"],
                truth=truth[task["service"]],
                upstream_payloads=task["upstream_payloads"],
                previous_payload=task["previous_payload"],
                previous_entries=task["previous_entries"],
                reuse_metrics=task["reuse_metrics"],
                changed_parameters=task["changed_parameters"],
                carry_inputs=task["carry_inputs"],
            )
            return task["service"], result

        if len(pending_tasks) > 1 and worker_limit > 1:
            with ThreadPoolExecutor(max_workers=worker_limit) as executor:
                computed = dict(executor.map(compute, pending_tasks))
        else:
            computed = dict(compute(task) for task in pending_tasks)

        for task in sorted(pending_tasks, key=lambda item: item["service"]):
            service_name = task["service"]
            result = computed[service_name]
            decision = _decision(
                service_name=service_name,
                plan_item=plan[service_name],
                reuse_base=reuse_base,
                result=result,
                previous_payload=task["previous_payload"],
            )
            stage_entries(
                connection,
                round_id=round_id,
                service=service_name,
                entries=result.entries,
            )
            if crash_enabled:
                _crash_after("staged", task["ordinal"])
            commit_service(
                connection,
                round_id=round_id,
                version=pending["version"],
                service=service_name,
                payload=result.payload,
                decision=decision,
                new_carries=result.new_carries,
            )
            payloads[service_name] = result.payload
            all_entries[service_name] = result.entries
            if crash_enabled:
                _crash_after("committed", task["ordinal"])

    expected = set(snapshot.topological_order())
    committed = set(committed_services(connection, round_id))
    missing = sorted(expected - committed)
    if missing:
        raise LedgerError(
            "E_INCOMPLETE_ROUND",
            "pending round still has uncommitted services",
            {"services": missing},
        )

    payloads = {
        name: service_result(connection, round_id, name)["payload"]
        for name in snapshot.topological_order()
    }
    pairs = compare_sensitive_pairs(snapshot, payloads)
    previous_pair_keys: set[str] = set()
    if reuse_base is not None:
        previous_pair_keys = {
            pair["pair_key"]
            for pair in sensitive_pairs(connection, reuse_base)
        }
    recomputed_sets = {
        name: set(plan[name]["recompute_derived"])
        for name in plan
    }
    reused_pair_keys: list[str] = []
    recomputed_pair_keys: list[str] = []
    for pair in pairs:
        metric = pair["comparison"]
        is_reused = (
            pair["pair_key"] in previous_pair_keys
            and metric not in recomputed_sets[pair["left"]]
            and metric not in recomputed_sets[pair["right"]]
        )
        if is_reused:
            reused_pair_keys.append(pair["pair_key"])
        else:
            recomputed_pair_keys.append(pair["pair_key"])
    store_sensitive_pairs(connection, round_id=round_id, pairs=pairs)

    decisions = {
        name: service_result(connection, round_id, name)["decision"]
        for name in snapshot.topological_order()
    }
    reused_entries = []
    for name in snapshot.topological_order():
        decision = decisions[name]
        for registration_no in decision["reused_registration_nos"]:
            reused_entries.append(
                {
                    "registration_no": int(registration_no),
                    "service": name,
                }
            )
    invalidations = dict(invalidations)
    invalidations["decisions"] = decisions
    invalidations["reused_ledger_entries"] = reused_entries
    invalidations["sensitive_pairs"] = {
        "recomputed": sorted(recomputed_pair_keys),
        "reused": sorted(reused_pair_keys),
    }

    service_hashes = {
        name: service_result(connection, round_id, name)["payload_hash"]
        for name in snapshot.topological_order()
    }
    pair_hashes = {
        pair["pair_key"]: hash_json(pair)
        for pair in pairs
    }
    result_hash = round_result_hash(
        service_hashes=service_hashes,
        pair_hashes=pair_hashes,
        invalidations=invalidations,
    )
    finalize_round(
        connection,
        round_id=round_id,
        result_hash=result_hash,
        invalidations=invalidations,
    )
    return round_summary(connection, round_id)


def _decision(
    *,
    service_name: str,
    plan_item: dict[str, Any],
    reuse_base: str | None,
    result: Any,
    previous_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    reused_numbers = set(int(value) for value in result.reused_conversions.values())
    for metric in result.reused_derived:
        if previous_payload is None:
            continue
        for source in previous_payload["derived"][metric]["sources"]:
            if source["service"] == service_name:
                reused_numbers.add(int(source["registration_no"]))
    return {
        "changed_parameters": plan_item["changed_parameters"],
        "previous_round_id": reuse_base,
        "recomputed_conversions": sorted(result.recomputed_conversions),
        "recomputed_derived": sorted(result.recomputed_derived),
        "reused_conversions": {
            key: int(value)
            for key, value in sorted(result.reused_conversions.items())
        },
        "reused_derived": sorted(result.reused_derived),
        "reused_registration_nos": sorted(reused_numbers),
        "service": service_name,
        "trigger_sources": plan_item["trigger_sources"],
    }


def round_summary(connection: Any, round_id: str) -> dict[str, Any]:
    info = round_info(connection, round_id)
    invalidations = info["invalidations"]
    services = committed_services(connection, round_id)
    pair_count = len(sensitive_pairs(connection, round_id))
    ledger_count = connection.execute(
        "SELECT COUNT(*) AS count FROM ledger_entries WHERE round_id = ?",
        (round_id,),
    ).fetchone()["count"]
    defects: list[dict[str, Any]] = []
    for service in services:
        result = service_result(connection, round_id, service)
        for metric, payload in result["payload"]["derived"].items():
            if not payload["ledger_ok"]:
                defects.append(
                    {
                        "derived": metric,
                        "first_defect": payload["first_defect"],
                        "service": service,
                    }
                )
    return {
        "affected_services": sorted(invalidations.get("affected_services", [])),
        "defects": defects,
        "ledger_entries": int(ledger_count),
        "logical_clock": info["clock"],
        "ok": True,
        "recomputed_derived": invalidations.get("recomputed_derived", []),
        "result_hash": info["result_hash"],
        "reused_services": sorted(invalidations.get("reused_services", [])),
        "round_id": round_id,
        "sensitive_pairs": pair_count,
        "service_count": len(services),
        "trigger_sources": invalidations.get("trigger_sources", []),
        "version": info["version"],
    }


def status_document(connection: Any) -> dict[str, Any]:
    state = current_state(connection)
    pending = pending_round(connection)
    pending_view: dict[str, Any] | None = None
    if pending is not None:
        pending_view = dict(pending)
        pending_view["committed_services"] = committed_services(
            connection, pending["round_id"]
        )
        pending_view["staged_entries"] = staged_entry_count(
            connection, pending["round_id"]
        )
        pending_view["committed_entries"] = connection.execute(
            "SELECT COUNT(*) AS count FROM ledger_entries "
            "WHERE round_id = ? AND status = 'COMMITTED'",
            (pending["round_id"],),
        ).fetchone()["count"]
    display_round = state["round_id"]
    complete = pending is None and state["round_id"] is not None
    if display_round is None and pending is not None:
        display_round = pending["round_id"]
    services: list[dict[str, Any]] = []
    pairs: list[dict[str, Any]] = []
    info: dict[str, Any] | None = None
    if display_round is not None:
        info = round_info(connection, display_round)
        pairs = sensitive_pairs(connection, display_round)
        by_service: dict[str, list[dict[str, Any]]] = {}
        for pair in pairs:
            by_service.setdefault(pair["left"], []).append(pair)
            by_service.setdefault(pair["right"], []).append(pair)
        for service in committed_services(connection, display_round):
            result = service_result(connection, display_round, service)
            payload = result["payload"]
            services.append(
                {
                    "derived": payload["derived"],
                    "layer": payload["layer"],
                    "sensitive": by_service.get(service, []),
                    "service": service,
                }
            )
    defects: list[dict[str, Any]] = []
    for service in services:
        for metric, payload in service["derived"].items():
            if not payload["ledger_ok"]:
                defects.append(
                    {
                        "derived": metric,
                        "first_defect": payload["first_defect"],
                        "service": service["service"],
                    }
                )
    document = {
        "complete": complete,
        "defects": defects,
        "invalidations": None if info is None else info["invalidations"],
        "logical_clock": 0 if info is None else info["clock"],
        "logical_clock_watermark": get_meta_int(connection, "logical_clock", 0),
        "ok": True,
        "pending_round": pending_view,
        "round_id": display_round,
        "schema": "infra-ledger-status/v1",
        "sensitive": pairs,
        "services": services,
        "version": state["version"] if info is None else info["version"],
    }
    return document


def diagnose_round(
    connection: Any,
    *,
    sample: Snapshot,
    patches: list[Patch],
    workers: int,
) -> dict[str, Any]:
    patches = normalize_patches(patches)
    ensure_base_loaded(connection, sample)
    existing_pending = pending_round(connection)
    if existing_pending is not None:
        if patches:
            raise LedgerError(
                "E_PENDING_ROUND",
                "cannot apply patches while a diagnostic round is pending; run resume first",
                {"round_id": existing_pending["round_id"]},
            )
        return run_pending_round(connection, workers=workers)

    base = load_base_snapshot(connection)
    state = current_state(connection)
    current = snapshot_for_version(connection, state["version"])
    unapplied: list[Patch] = []
    for patch in patches:
        existing = patch_record(connection, patch.patch_id)
        if existing is not None:
            if existing["patch_hash"] != patch.patch_hash:
                raise LedgerError(
                    "E_PATCH_CONFLICT",
                    "patch_id was already used with different contents",
                    {"patch_id": patch.patch_id},
                )
            continue
        unapplied.append(patch)

    working, applied, changes = _preflight_patches(base, current, unapplied)
    if working.version != current.version:
        plan = build_invalidation_plan(
            connection,
            snapshot=working,
            previous_round_id=state["round_id"],
            changes=changes,
        )
        create_pending_round(
            connection,
            snapshot=working,
            parent_version=state["version"],
            parent_round_id=state["round_id"],
            reuse_base_round=state["round_id"],
            invalidations=plan,
            patches=applied,
            step=working.config.logical_clock_step,
        )
        return run_pending_round(connection, workers=workers)

    for patch in applied:
        record_noop_patch(
            connection,
            patch=patch,
            version=current.version,
            clock=state["logical_clock"],
        )
    if state["round_id"] is not None:
        return round_summary(connection, state["round_id"])

    plan = build_invalidation_plan(
        connection,
        snapshot=current,
        previous_round_id=None,
        changes=[],
    )
    create_pending_round(
        connection,
        snapshot=current,
        parent_version=None,
        parent_round_id=None,
        reuse_base_round=None,
        invalidations=plan,
        patches=[],
        step=current.config.logical_clock_step,
    )
    return run_pending_round(connection, workers=workers)
