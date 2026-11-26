from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import PolicyIntegrityError, PolicyVersionRollbackError
from .models import PolicyRecord, jsonable
from .parser import load_v2_package
from .policy import affected_function_closure, merge_policy_records
from .store import StateStore
from .utils import canonical_json, stable_key_digest


class PolicyPackageManager:
    def __init__(
        self,
        store: StateStore,
        *,
        dependencies: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.store = store
        self.dependencies = dependencies or {}

    def install_view_snapshot(
        self,
        *,
        snapshot_id: str,
        version: int,
        clock_ms: int,
        policy_view: dict[str, Any],
        manifest: dict[str, Any] | None = None,
        kind: str = "merged_v1_v2",
    ) -> dict[str, Any]:
        records = [jsonable(merged.selected) for merged in policy_view.values()]
        payload_manifest = manifest or {}
        payload_manifest.setdefault("key_set_digest", stable_key_digest(list(policy_view)))
        self.store.install_policy_snapshot(
            snapshot_id=snapshot_id,
            version=version,
            kind=kind,
            manifest=payload_manifest,
            records=records,
            clock_ms=clock_ms,
        )
        return self.store.get_policy_snapshot(snapshot_id) or {}

    def apply_package(
        self, package_path: str | Path, *, clock_ms: int = 0
    ) -> dict[str, Any]:
        incoming_records, manifest, metadata_rows = load_v2_package(package_path)
        incoming_version = self._manifest_version(manifest)
        incoming_floor = int(
            manifest.get("generation_floor")
            or manifest.get("min_generation")
            or min(record.generation for record in incoming_records)
        )
        pointer = self.store.get_active_policy_snapshot()
        active_snapshot = (
            self.store.get_policy_snapshot(pointer["snapshot_id"]) if pointer else None
        )
        active_version = int(active_snapshot["version"]) if active_snapshot else 0
        active_floor = 0
        if active_snapshot:
            active_floor = int(
                active_snapshot["manifest"].get("generation_floor")
                or active_snapshot["manifest"].get("min_generation")
                or 0
            )

        if active_snapshot and incoming_version < active_version:
            raise PolicyVersionRollbackError(
                f"policy version rollback rejected: {incoming_version} < active {active_version}"
            )
        if incoming_floor < active_floor:
            raise PolicyIntegrityError(
                f"policy generation floor decreases: {incoming_floor} < active {active_floor}"
            )

        requested_mode = str(
            manifest.get("update_mode") or manifest.get("package_kind") or manifest.get("mode") or "auto"
        )
        base_version = manifest.get("base_version")
        is_incremental = requested_mode == "incremental" or (
            requested_mode == "auto"
            and active_snapshot is not None
            and base_version is not None
            and int(base_version) == active_version
            and bool(manifest.get("patch", True))
        )
        if requested_mode == "full":
            kind = "full"
        elif requested_mode == "incremental":
            kind = "incremental"
        else:
            kind = "incremental" if is_incremental else "full"

        active_record_objects: list[PolicyRecord] = []
        if active_snapshot:
            for row in active_snapshot.get("records", []):
                active_record_objects.append(PolicyRecord(**row))

        if kind == "incremental":
            if not active_snapshot:
                raise PolicyIntegrityError("incremental policy package requires an active snapshot")
            if base_version is None or int(base_version) != active_version:
                raise PolicyIntegrityError(
                    f"incremental base_version {base_version!r} does not match active {active_version}"
                )
            combined_by_key = {record.key: record for record in active_record_objects}
            for record in incoming_records:
                if record.tombstone:
                    combined_by_key.pop(record.key, None)
                else:
                    combined_by_key[record.key] = record
            final_records = list(combined_by_key.values())
            changed_keys = {record.key for record in incoming_records}
        else:
            final_records = incoming_records
            if active_snapshot:
                old_view = merge_policy_records(active_record_objects)
                new_view = merge_policy_records(incoming_records)
                changed_keys = self._diff_keys(old_view, new_view)
            else:
                changed_keys = {record.key for record in incoming_records}

        new_view = merge_policy_records(final_records)
        old_view = (
            merge_policy_records(active_record_objects) if active_record_objects else {}
        )
        closure_view = {**old_view, **new_view}
        closure = affected_function_closure(
            changed_keys, closure_view, dependencies=self.dependencies
        )

        key_digest = stable_key_digest(list(new_view))
        expected_digest = manifest.get("key_set_digest") or manifest.get("keys_digest")
        incoming_digest = stable_key_digest([record.key for record in incoming_records])
        digest_decision = {
            "manifest_digest": expected_digest,
            "incoming_package_digest": incoming_digest,
            "incoming_matches_manifest": (
                expected_digest == incoming_digest if expected_digest else None
            ),
            "post_apply_view_digest": key_digest,
        }
        if (
            expected_digest
            and kind == "full"
            and not any(record.tombstone for record in incoming_records)
            and expected_digest != key_digest
        ):
            raise PolicyIntegrityError("applied full policy key-set digest does not match manifest")

        snapshot_id = str(
            manifest.get("snapshot_id")
            or f"policy-v{incoming_version}-{key_digest.split(':', 1)[1][:12]}"
        )
        decision_manifest = dict(manifest)
        decision_manifest.update(
            {
                "generation_floor": incoming_floor,
                "applied_kind": kind,
                "active_version_before": active_version,
                "changed_keys": sorted(changed_keys),
                "invalidation_closure": closure,
                "digest_decision": digest_decision,
                "metadata_row_count": len(metadata_rows),
                "shard_hashes_verified": [
                    {"path": shard.get("path"), "sha256": shard.get("sha256")}
                    for shard in manifest.get("shards", [])
                ],
            }
        )
        self.store.install_policy_snapshot(
            snapshot_id=snapshot_id,
            version=incoming_version,
            kind=kind,
            manifest=decision_manifest,
            records=[jsonable(policy.selected) for policy in new_view.values()],
            clock_ms=clock_ms,
        )
        return {
            "snapshot_id": snapshot_id,
            "version": incoming_version,
            "kind": kind,
            "active_version_before": active_version,
            "generation_floor": incoming_floor,
            "changed_keys": sorted(changed_keys),
            "key_set_digest": key_digest,
            "digest_decision": digest_decision,
            "invalidation_closure": closure,
            "pointer": self.store.get_active_policy_snapshot(),
        }

    @staticmethod
    def _manifest_version(manifest: dict[str, Any]) -> int:
        value = manifest.get("version", manifest.get("package_version"))
        if isinstance(value, bool) or not isinstance(value, int):
            raise PolicyIntegrityError("policy manifest version must be an integer")
        return value

    @staticmethod
    def _diff_keys(old_view: dict[str, Any], new_view: dict[str, Any]) -> set[str]:
        changed: set[str] = set()
        for key in set(old_view) | set(new_view):
            old = old_view.get(key)
            new = new_view.get(key)
            if old is None or new is None:
                changed.add(key)
                continue
            if canonical_json(old.decision) != canonical_json(new.decision) or canonical_json(
                jsonable(old.selected)
            ) != canonical_json(jsonable(new.selected)):
                changed.add(key)
        return changed
