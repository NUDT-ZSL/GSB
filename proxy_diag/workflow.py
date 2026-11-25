"""Application workflows used by both CLI and built-in verification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .engine import recover_claims, replay_all, replay_targeted
from .errors import E_VERSION_ROLLBACK, E_PACKAGE_CONFLICT, E_STATE, WorkbenchError
from .faults import maybe_kill
from .jsonio import canonical_hash, read_json, write_json_atomic
from .locks import global_lock
from .merge import build_unified_view
from .packageio import PackageBundle, load_package
from .paths import Paths
from .reporting import build_status
from .server import start_loopback_server
from .store import Store
from .tableflow import apply_patch_file
from .validation import validate_bundle


def incoming_fingerprint(bundle: PackageBundle) -> dict[str, Any]:
    manifest = bundle.v2_manifest
    return {
        "package_id": bundle.package_id,
        "manifest_version": bundle.manifest_version,
        "generation_floor": manifest.get("session_generation_floor"),
        "shards": sorted(
            (
                {"path": shard.get("path"), "sha256": shard.get("sha256")}
                for shard in manifest.get("shards", [])
            ),
            key=lambda item: str(item["path"]),
        ),
        "table_digests": dict(sorted((manifest.get("table_digests") or {}).items())),
        "v1_hash": canonical_hash(bundle.v1) if bundle.v1 is not None else None,
        "v1_table_hash": canonical_hash(bundle.v1_table) if bundle.v1_table is not None else None,
        "v2_table_hash": canonical_hash(bundle.v2_table) if bundle.v2_table is not None else None,
    }


def classify_change(old: dict[str, Any], new: dict[str, Any]) -> str:
    if old.get("generation_floor") != new.get("generation_floor"):
        return "full_replace"
    old_shards = {item["path"]: item["sha256"] for item in old.get("shards", [])}
    new_shards = {item["path"]: item["sha256"] for item in new.get("shards", [])}
    for path, digest in old_shards.items():
        if new_shards.get(path) != digest:
            return "full_replace"
    old_tables = old.get("table_digests", {})
    new_tables = new.get("table_digests", {})
    for version, digest in old_tables.items():
        if new_tables.get(version) != digest:
            return "full_replace"
    if old.get("v1_hash") != new.get("v1_hash"):
        return "full_replace"
    return "incremental_append"


def ensure_table_versions(store: Store, bundle: PackageBundle, clock_ms: int) -> None:
    if bundle.v1_table and bundle.v1_table.get("version"):
        store.ensure_table_version(
            str(bundle.v1_table["version"]),
            bundle.v1_table.get("entries", []),
            clock_ms,
        )
    if bundle.v2_table and bundle.v2_table.get("version"):
        store.ensure_table_version(
            str(bundle.v2_table["version"]),
            bundle.v2_table.get("entries", []),
            clock_ms,
        )


def load_workflow(paths: Paths, descriptor: str | Path | None = None) -> dict[str, Any]:
    with global_lock(paths.global_lock_path):
        return _load_locked(paths, descriptor)


def _load_locked(paths: Paths, descriptor: str | Path | None) -> dict[str, Any]:
    bundle = load_package(paths, descriptor)
    validation = validate_bundle(bundle)
    view = build_unified_view(bundle, validation)
    view["clock_watermark_ms"] = bundle.clock.watermark_ms
    fingerprint = incoming_fingerprint(bundle)
    descriptor_digest = canonical_hash(bundle.descriptor)

    with Store(paths) as store:
        ensure_table_versions(store, bundle, bundle.clock.watermark_ms)
        current = store.current_snapshot()
        existing_package = store.package(bundle.package_id)
        if existing_package is not None and existing_package["descriptor_digest"] != descriptor_digest:
            raise WorkbenchError(
                E_PACKAGE_CONFLICT,
                "package_id was already registered with different content",
                {"package_id": bundle.package_id},
            )

        if current is None:
            decision = "full_replace"
            reason = "initial_snapshot"
        elif bundle.manifest_version < int(current["snapshot_version"]):
            raise WorkbenchError(
                E_VERSION_ROLLBACK,
                "log package manifest version would roll back current snapshot",
                {
                    "incoming_version": bundle.manifest_version,
                    "current_version": int(current["snapshot_version"]),
                    "package_id": bundle.package_id,
                },
            )
        elif bundle.manifest_version == int(current["snapshot_version"]):
            if current["view_hash"] == view["view_hash"]:
                decision = "no_operation"
                reason = "same_version_and_digest"
            else:
                raise WorkbenchError(
                    E_PACKAGE_CONFLICT,
                    "same manifest version has different snapshot digest",
                    {"version": bundle.manifest_version},
                )
        else:
            current_package = store.package(current["package_id"])
            if current_package is not None:
                old_payload = json.loads(current_package["payload_json"])
                old_fingerprint = old_payload.get("fingerprint", {})
                decision = classify_change(old_fingerprint, fingerprint)
                reason = "fingerprint_comparison"
            else:
                decision = "full_replace"
                reason = "current_package_fingerprint_unavailable"

        if decision != "no_operation" or current is None or current["view_hash"] != view["view_hash"]:
            store.apply_snapshot(view, package_id=bundle.package_id, switched_clock_ms=bundle.clock.watermark_ms)
        if existing_package is None:
            store.register_package(
                package_id=bundle.package_id,
                manifest_version=bundle.manifest_version,
                descriptor_digest=descriptor_digest,
                decision=decision,
                payload={"fingerprint": fingerprint, "descriptor": bundle.descriptor},
                clock_ms=bundle.clock.watermark_ms,
            )

    return {
        "ok": True,
        "command": "load",
        "decision": decision,
        "reason": reason,
        "package_id": bundle.package_id,
        "manifest_version": bundle.manifest_version,
        "view_hash": view["view_hash"],
        "validation": validation,
        "fingerprint": fingerprint,
    }


def merge_workflow(paths: Paths, descriptor: str | Path | None = None) -> dict[str, Any]:
    with global_lock(paths.global_lock_path):
        if descriptor is not None:
            load_result = _load_locked(paths, descriptor)
        else:
            with Store(paths) as store:
                has_snapshot = store.current_snapshot() is not None
            if not has_snapshot:
                load_result = _load_locked(paths, None)
            else:
                load_result = None

        with Store(paths) as store:
            view = store.get_unified_view()
            if view is None:
                bundle = load_package(paths, descriptor)
                validation = validate_bundle(bundle)
                view = build_unified_view(bundle, validation)
                view["clock_watermark_ms"] = bundle.clock.watermark_ms
                store.apply_snapshot(view, package_id=bundle.package_id, switched_clock_ms=bundle.clock.watermark_ms)
            maybe_kill("merge_before_upsert")
            stats = store.upsert_sessions_from_view(view)
        write_json_atomic(paths.unified_view_path, view)
        return {
            "ok": True,
            "command": "merge",
            "session_count": view["session_count"],
            "view_hash": view["view_hash"],
            "upsert": stats,
            "load": load_result,
            "output_path": str(paths.unified_view_path),
        }


def load_plan(paths: Paths, plan_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(plan_path) if plan_path else paths.samples / "test_service_plan.json"
    if not path.is_absolute():
        path = paths.root / path
    return read_json(path)


def load_replay_clock(paths: Paths) -> int:
    clock_path = paths.samples / "logical_clock.json"
    return int(read_json(clock_path)["watermark_ms"])


def replay_workflow(
    paths: Paths,
    *,
    session_id: str | None = None,
    base_url: str | None = None,
    plan_path: str | Path | None = None,
    worker_id: str = "cli",
    recover: bool = False,
) -> dict[str, Any]:
    plan = load_plan(paths, plan_path)
    clock_ms = load_replay_clock(paths)
    server = None
    registry = None
    if base_url is None:
        server, base_url, registry = start_loopback_server(plan)
    try:
        with Store(paths) as store:
            recovery = recover_claims(store, paths) if recover else None
            if session_id:
                result = replay_targeted(
                    store,
                    paths,
                    session_id,
                    base_url=base_url,
                    plan=plan,
                    replay_clock_ms=clock_ms,
                    worker_id=worker_id,
                )
            else:
                result = replay_all(
                    store,
                    paths,
                    base_url=base_url,
                    plan=plan,
                    replay_clock_ms=clock_ms,
                    worker_id=worker_id,
                )
                if recovery is not None:
                    result["recovery"] = recovery
            result["ok"] = True
            result["command"] = "resume" if recover else "replay"
            result["base_url_mode"] = "external" if server is None else "embedded_loopback"
            if registry is not None:
                result["embedded_server_calls"] = registry.state()["call_count"]
            return result
    finally:
        if server is not None:
            server.shutdown()


def patch_workflow(paths: Paths, patch_path: str | Path) -> dict[str, Any]:
    with global_lock(paths.global_lock_path):
        with Store(paths) as store:
            for filename in ("v1_forward_table.json", "v2_forward_table.json"):
                table_path = paths.samples / "tables" / filename
                if table_path.exists():
                    table = read_json(table_path)
                    store.ensure_table_version(
                        str(table["version"]),
                        table.get("entries", []),
                        int(read_json(paths.samples / "logical_clock.json")["watermark_ms"]),
                    )
            closure = apply_patch_file(store, patch_path)
        write_json_atomic(paths.report_dir / "last_closure.json", closure)
        return closure


def status_workflow(paths: Paths, *, write: bool = True) -> dict[str, Any]:
    with Store(paths) as store:
        status = build_status(store)
    if write:
        write_json_atomic(paths.status_path, status)
    return status


def resume_workflow(
    paths: Paths,
    *,
    base_url: str | None = None,
    plan_path: str | Path | None = None,
    worker_id: str = "resume",
) -> dict[str, Any]:
    return replay_workflow(
        paths,
        base_url=base_url,
        plan_path=plan_path,
        worker_id=worker_id,
        recover=True,
    )
