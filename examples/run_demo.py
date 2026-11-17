"""End-to-end deterministic demo for the blocksnap component.

Scenarios, in fixed order:

1. first snapshot and rollback (low change ratio -> bitmap_diff / full_compare
   cross-check, bit-identical)
2. multi-snapshot chain rollback (incremental_chain path)
3. manifest format-version upgrade with whole-entry replacement
4. interrupted rollback and journal-based recovery
5. business codes E_INTEGRITY, E_CHAIN, E_VOLUME / E_SNAPSHOT

Run on Windows::

    python examples\\run_demo.py --workspace demo_workspace
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blocksnap.audit import run_audit
from blocksnap.errors import ChainError, IntegrityError, SnapshotError, VolumeError
from blocksnap.logging_utils import query_logs
from blocksnap.manager import SnapshotManager
from blocksnap.rollback import PATH_BITMAP, PATH_CHAIN, PATH_FULL, rollback
from blocksnap.volume import Volume

PLAN_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "samples",
    "snapshot_plan.json",
)


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def load_plan() -> dict:
    with open(PLAN_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def fresh_workspace(root: str, name: str) -> tuple[str, str, str]:
    path = os.path.join(root, name)
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)
    image = os.path.join(path, "volume.bin")
    store = os.path.join(path, "store")
    return path, image, store


def make_manager(image: str, store: str, plan: dict) -> SnapshotManager:
    Volume.create(image, int(plan["block_size"]), int(plan["block_count"]))
    return SnapshotManager(Volume.load(image), store)


def inject(manager: SnapshotManager, injections: list[dict]) -> None:
    for item in injections:
        manager.inject_hex(int(item["block_index"]), item["hex"])


def scenario_one(root: str, plan: dict) -> dict:
    section("Scenario 1: first snapshot, low-ratio change, 3-path identity")
    _, image, store = fresh_workspace(root, "scenario1")
    spec = plan["scenarios"][0]
    with make_manager(image, store, plan) as manager:
        first = manager.create_snapshot(spec["snapshots"][0]["name"])
        inject(manager, spec["snapshots"][1]["injections"])
        second = manager.create_snapshot(spec["snapshots"][1]["name"])
        assert first.state_digest != second.state_digest
        live_digest = manager.volume.current_digest()
        results = {}
        for forced in (PATH_BITMAP, PATH_FULL, PATH_CHAIN):
            report = rollback(manager, first.name, path=forced)
            results[forced] = {
                "path": report["path"],
                "volume_blocks": report["volume_blocks"],
                "changed_blocks": report["changed_blocks"],
                "changed_ratio": report["changed_ratio"],
                "peak_memory_estimate": report["peak_memory_estimate"],
                "bit_identical": report["bit_identical"],
                "post_rollback_digest": report["post_rollback_digest"],
            }
            manager.volume.close()
            reopened = SnapshotManager(Volume.load(image), store)
            manager.volume = reopened.volume
            for item in spec["snapshots"][1]["injections"]:
                manager.volume.inject_hex(int(item["block_index"]), item["hex"])
        identical = len({r["post_rollback_digest"] for r in results.values()}) == 1
        conclusion = run_audit(manager)
        out = {
            "scenario": "first_snapshot_and_rollback",
            "live_digest_before_rollback": live_digest,
            "paths": results,
            "all_paths_bit_identical": identical,
            "audit_status": conclusion["status"],
        }
        print(json.dumps(out, sort_keys=True, indent=2))
        return out


def scenario_two(root: str, plan: dict) -> dict:
    section("Scenario 2: multi-snapshot incremental chain rollback")
    _, image, store = fresh_workspace(root, "scenario2")
    spec = plan["scenarios"][1]
    with make_manager(image, store, plan) as manager:
        first = manager.create_snapshot(spec["snapshots"][0]["name"])
        entries = [first]
        for snap in spec["snapshots"][1:]:
            inject(manager, snap["injections"])
            entries.append(manager.create_snapshot(snap["name"]))
        target = entries[0]
        auto_report = rollback(manager, target.name)
        cross = {PATH_CHAIN: auto_report["post_rollback_digest"]}

    for forced in (PATH_BITMAP, PATH_FULL):
        ws = os.path.join(root, f"scenario2-{forced}")
        img2 = os.path.join(ws, "volume.bin")
        store2 = os.path.join(ws, "store")
        with make_manager(img2, store2, plan) as manager:
            manager.create_snapshot(spec["snapshots"][0]["name"])
            for snap in spec["snapshots"][1:]:
                inject(manager, snap["injections"])
                manager.create_snapshot(snap["name"])
            report = rollback(manager, target.name, path=forced)
            cross[forced] = report["post_rollback_digest"]
            conclusion_forced = run_audit(manager)

    with SnapshotManager(Volume.load(image), store) as manager:
        conclusion = run_audit(manager)
        out = {
            "scenario": "multi_snapshot_chain",
            "target_snapshot": target.name,
            "selected_path": auto_report["path"],
            "snapshot_count": auto_report["snapshot_count"],
            "volume_blocks": auto_report["volume_blocks"],
            "changed_blocks": auto_report["changed_blocks"],
            "peak_memory_estimate": auto_report["peak_memory_estimate"],
            "path_digests": cross,
            "all_paths_bit_identical": len(set(cross.values())) == 1,
            "audit_status": conclusion["status"],
        }
        print(json.dumps(out, sort_keys=True, indent=2))
        return out


def rebuild_diverged(image: str, plan: dict, spec: dict) -> Volume:
    """Recreate the fully-diverged volume state without CoW bookkeeping."""
    volume = Volume.load(image)
    for snap in spec["snapshots"]:
        for item in snap["injections"]:
            volume.inject_hex(int(item["block_index"]), item["hex"])
    return volume


def scenario_three(root: str, plan: dict) -> dict:
    section("Scenario 3: manifest format-version upgrade, whole replacement")
    _, image, store = fresh_workspace(root, "scenario3")
    spec = plan["scenarios"][1]
    with make_manager(image, store, plan) as manager:
        for snap in spec["snapshots"]:
            inject(manager, snap["injections"])
            manager.create_snapshot(snap["name"])
        before = run_audit(manager)
        manager.seal_for_rollback()
        bundle = build_upgrade_bundle(manager, plan)
        report = manager.replace_manifest(
            bundle["manifest"], bundle["objects"]
        )
        after = run_audit(manager)
        out = {
            "scenario": "manifest_upgrade_whole_replace",
            "old_format_version": report["old_format_version"],
            "new_format_version": report["new_format_version"],
            "version_changed": report["version_changed"],
            "digest_changed": report["digest_changed"],
            "affected_snapshots": report["affected_snapshots"],
            "rebuilt_chain": report["rebuilt_chain"],
            "backup_readonly": report["replaced"],
            "audit_before": before["status"],
            "audit_after": after["status"],
        }
        print(json.dumps(out, sort_keys=True, indent=2))
        return out


def build_upgrade_bundle(manager: SnapshotManager, plan: dict) -> dict:
    old = manager.manifest
    rename = plan["manifest_upgrade"]["rename"]
    new_entries = []
    staged_objects = {}
    for entry in old.snapshots:
        cloned = copy.deepcopy(entry)
        cloned.name = rename.get(entry.name, entry.name)
        new_entries.append(cloned)
        if entry.bitmap_object and not manager.objects.exists(entry.bitmap_object):
            raise ChainError("missing bitmap", location="demo.build_upgrade_bundle")
    new_manifest = manager.manifest.__class__(
        format_version=int(plan["manifest_upgrade"]["to_format_version"]),
        sequence=len(new_entries),
        block_size=old.block_size,
        block_count=old.block_count,
        baseline_object=old.baseline_object,
        snapshots=new_entries,
    )
    return {"manifest": new_manifest, "objects": staged_objects}


def scenario_high_ratio(root: str, plan: dict) -> dict:
    section("Scenario 2b: high change ratio selects full_compare")
    spec = plan["scenarios"][2]
    _, image, store = fresh_workspace(root, "scenario2b")
    with make_manager(image, store, plan) as manager:
        target = manager.create_snapshot(spec["snapshots"][0]["name"])
        for index in spec["high_ratio_indices"]:
            manager.inject_hex(int(index), spec["hex"])
        manager.seal_for_rollback()
        auto = rollback(manager, target.name)
        forced = {}
        for ws_name, used in (("fb", PATH_BITMAP), ("ff", PATH_FULL), ("fc", PATH_CHAIN)):
            ws = os.path.join(root, f"scenario2b-{ws_name}")
            img2 = os.path.join(ws, "volume.bin")
            store2 = os.path.join(ws, "store")
            with make_manager(img2, store2, plan) as m2:
                m2.create_snapshot(spec["snapshots"][0]["name"])
                for index in spec["high_ratio_indices"]:
                    m2.inject_hex(int(index), spec["hex"])
                m2.seal_for_rollback()
                report = rollback(m2, target.name, path=used)
                forced[used] = report["post_rollback_digest"]
        out = {
            "scenario": "high_change_ratio_full_path",
            "selected_path": auto["path"],
            "changed_blocks": auto["changed_blocks"],
            "volume_blocks": auto["volume_blocks"],
            "changed_ratio": auto["changed_ratio"],
            "threshold": auto["threshold"],
            "peak_memory_estimate": auto["peak_memory_estimate"],
            "all_paths_bit_identical": len(set(forced.values())) == 1,
        }
        print(json.dumps(out, sort_keys=True, indent=2))
        return out


def scenario_four(root: str, plan: dict) -> dict:
    section("Scenario 4: interrupted rollback + durable recovery")
    _, image, store = fresh_workspace(root, "scenario4")
    spec = plan["scenarios"][1]
    with make_manager(image, store, plan) as manager:
        target_entry = None
        for snap in spec["snapshots"]:
            inject(manager, snap["injections"])
            target_entry = manager.create_snapshot(snap["name"])
        pre_digest = manager.volume.current_digest()
        crashed = False
        try:
            rollback(manager, target_entry.name,
                     path=PATH_CHAIN, crash_after_prepare=True)
        except RuntimeError:
            crashed = True
        journal_open = manager.journal.exists()
        live_unchanged = manager.volume.current_digest() == pre_digest
        manager.volume.close()

    with SnapshotManager(Volume.load(image), store) as restarted:
        result = restarted.recover_pending()
        post_digest = restarted.volume.current_digest()
        completed = post_digest == target_entry.state_digest
        out = {
            "scenario": "interrupted_rollback_recovery",
            "crash_observed": crashed,
            "journal_present_after_crash": journal_open,
            "live_volume_unpolluted": live_unchanged,
            "recovery_aborted": bool(isinstance(result, dict) and result.get("aborted")),
            "recovery_completed": completed,
            "recovered_digest_matches_snapshot": completed,
            "detached_after_recovery": restarted.detached,
        }
        print(json.dumps(out, sort_keys=True, indent=2))
        return out


def scenario_five(root: str, plan: dict) -> dict:
    section("Scenario 5: business codes E_VOLUME / E_SNAPSHOT / E_INTEGRITY / E_CHAIN")
    _, image, store = fresh_workspace(root, "scenario5")
    spec = plan["scenarios"][1]
    codes = []
    with make_manager(image, store, plan) as manager:
        for snap in spec["snapshots"]:
            inject(manager, snap["injections"])
            manager.create_snapshot(snap["name"])
        first = manager.list_snapshots()[0]

        try:
            manager.inject_hex(plan["block_count"], "ab")
        except VolumeError as exc:
            codes.append(exc.to_record())
        try:
            manager.create_snapshot(first.name)
        except SnapshotError as exc:
            codes.append(exc.to_record())

        corruptible = next(
            entry for entry in manager.list_snapshots() if entry.blocks
        )
        target_ref = corruptible.blocks[0]
        object_path = os.path.join(
            store, "objects", "sha256",
            target_ref.digest[:2], target_ref.digest,
        )
        with open(object_path, "r+b") as handle:
            handle.seek(0)
            handle.write(b"\xff" * 16)
        try:
            rollback(manager, corruptible.name, path=PATH_BITMAP)
        except IntegrityError as exc:
            codes.append(exc.to_record())

        missing = next(
            entry for entry in reversed(manager.list_snapshots()) if entry.blocks
        )
        ref = missing.blocks[0]
        object_id = ref.object_id
        bad_path = os.path.join(store, "objects", "sha256",
                                object_id.split("-")[1][:2], object_id.split("-")[1])
        os.remove(bad_path)
        try:
            rollback(manager, missing.name, path=PATH_CHAIN)
        except ChainError as exc:
            codes.append(exc.to_record())

    logs = query_logs(store)
    logged_codes = sorted({rec.get("code") for rec in logs if rec.get("code")})
    out = {
        "scenario": "business_code_paths",
        "raised_codes": [item["code"] for item in codes],
        "first_corrupt_block": next(
            (item["context"].get("first_corrupt_block")
             for item in codes if item["code"] == "E_INTEGRITY"), None),
        "codes_present_in_structured_log": logged_codes,
    }
    print(json.dumps(out, sort_keys=True, indent=2))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default="demo_workspace")
    args = parser.parse_args()
    plan = load_plan()
    root = os.path.abspath(args.workspace)
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root, exist_ok=True)
    summary = [
        scenario_one(root, plan),
        scenario_two(root, plan),
        scenario_high_ratio(root, plan),
        scenario_three(root, plan),
        scenario_four(root, plan),
        scenario_five(root, plan),
    ]
    section("Demo summary (stable order)")
    print(json.dumps(summary, sort_keys=True, indent=2))
    with open(os.path.join(root, "demo_summary.json"), "w", encoding="utf-8") as h:
        json.dump(summary, h, sort_keys=True, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
