"""Session snapshots: persist manifest version, plan, receipts, state-machine
transitions and committed version. Reopening a snapshot whose manifest version
differs from the current config version replaces state wholesale (never
merges); the old snapshot file is kept read-only."""
import json
import os
import stat

from .errors import E_VERSION, PlatformError

SNAPSHOT_FORMAT = 1


def save_snapshot(store, manifest, path):
    data = store.dump_all()
    committed = store.last_committed()
    snap = {
        "snapshot_format": SNAPSHOT_FORMAT,
        "manifest_version": manifest["format_version"],
        "manifest": manifest,
        "committed_version": committed["version"] if committed else None,
        "sessions": data["sessions"],
        "transitions": data["transitions"],
        "plans": data["plans"],
        "receipts": data["receipts"],
        "committed": data["committed"],
        "meta": data["meta"],
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return snap


def load_snapshot(path):
    with open(path, "r", encoding="utf-8") as fh:
        snap = json.load(fh)
    if snap.get("snapshot_format") != SNAPSHOT_FORMAT:
        raise PlatformError(E_VERSION, "unsupported snapshot format",
                            path=path,
                            snapshot_format=snap.get("snapshot_format"))
    return snap


def restore_snapshot(store, snap, current_manifest_version, logger=None):
    """Wholesale restore. Version conflict (snapshot committed version lower
    than an already-committed version, i.e. a downgrade) is rejected with
    E_VERSION. Manifest-version mismatch triggers wholesale replacement of
    manifest, plan and receipt state - never an incremental merge."""
    replaced = snap["manifest_version"] != current_manifest_version
    current_committed = store.last_committed()
    snap_committed = snap.get("committed_version")
    if (not replaced and snap_committed is not None
            and current_committed is not None
            and snap_committed < current_committed["version"]):
        raise PlatformError(
            E_VERSION,
            f"snapshot committed version {snap_committed} conflicts with "
            f"already committed version {current_committed['version']}",
            snapshot_committed=snap_committed,
            current_committed=current_committed["version"])

    store.replace_all(snap["sessions"], snap["transitions"], snap["plans"],
                      snap["receipts"], snap["committed"], snap["meta"])
    if logger:
        logger.log("snapshot_restored", replaced=replaced,
                   manifest_version=snap["manifest_version"],
                   committed_version=snap_committed)
    return {"replaced": replaced, "manifest": snap["manifest"],
            "committed_version": snap_committed}


def make_read_only(path):
    os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
