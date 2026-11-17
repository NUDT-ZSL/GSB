"""Session snapshots: versioned persistence and whole-replace restore.

A snapshot persists the manifest version, the policy decision (including
the tie threshold), the per-stream window ledger, the send plan and the
diagnostic conclusions. Reopening a snapshot whose manifest version
differs from the current session replaces the session state wholesale
(never merges). After a restore, replay verification recomputes the plan
deterministically and compares it byte-for-byte; any divergence is
reported as E_REPLAY.
"""

import json
import os

from .errors import DiagError, E_REPLAY
from .manifest import validate_manifest
from .planner import Plan, simulate

SNAPSHOT_VERSION = 1


def save_snapshot(session, path):
    snap = {
        "snapshot_version": SNAPSHOT_VERSION,
        "manifest_version": session.manifest["version"],
        "manifest": session.manifest,
        "policy": session.policy_decision,
        "plan": session.plan.to_dict() if session.plan else None,
        "ledger": (session.report["runtime"]["ledger"]
                   if session.report else None),
        "diagnostics": session.report["diagnostics"]
        if session.report else [],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(snap, handle, indent=2, sort_keys=True)
    return path


def load_snapshot(path):
    with open(path, "r", encoding="utf-8") as handle:
        snap = json.load(handle)
    if snap.get("snapshot_version") != SNAPSHOT_VERSION:
        raise DiagError(E_REPLAY,
                        "unsupported snapshot_version "
                        f"{snap.get('snapshot_version')}",
                        path="$.snapshot_version")
    return snap


def restore_into_session(session, snap):
    """Restore ``snap`` into ``session``.

    When the snapshot manifest version differs from the session's current
    manifest version, the whole session state (manifest, ledger, plan) is
    replaced by the snapshot content; nothing is merged incrementally.
    Returns a restore report including the replay verification verdict.
    """
    current_version = session.manifest["version"]
    snap_version = snap["manifest_version"]
    replaced = snap_version != current_version

    if replaced:
        session.manifest = validate_manifest(snap["manifest"])
        session.policy_decision = snap["policy"]
        session.plan = None
        session.report = None

    replay = verify_replay(snap)
    return {
        "replaced": replaced,
        "previous_manifest_version": current_version,
        "snapshot_manifest_version": snap_version,
        "replay": replay,
    }


def verify_replay(snap):
    """Recompute the send plan from the snapshot manifest and policy and
    compare it byte-for-byte with the persisted plan."""
    if snap.get("plan") is None:
        raise DiagError(E_REPLAY, "snapshot contains no send plan",
                        path="$.plan")
    manifest = validate_manifest(snap["manifest"])
    strategy = snap["policy"]["selected"]
    recomputed = simulate(manifest, strategy)
    expected = Plan.frame_signature(snap["plan"]["frames"])
    actual = Plan.frame_signature(recomputed.frames)
    if expected != actual:
        index = next(
            (i for i, (a, b) in enumerate(zip(expected, actual)) if a != b),
            min(len(expected), len(actual)))
        raise DiagError(
            E_REPLAY,
            "snapshot plan is not reproducible: first divergence at "
            f"frame {index}",
            path=f"$.plan.frames[{index}]",
            details={
                "frame_index": index,
                "expected": expected[index] if index < len(expected) else None,
                "actual": actual[index] if index < len(actual) else None,
                "expected_frames": len(expected),
                "actual_frames": len(actual),
            })
    expected_grants = [tuple(g) for g in snap["plan"]["predicted_grants"]]
    if expected_grants != recomputed.predicted_grants:
        raise DiagError(
            E_REPLAY,
            "snapshot predicted grants are not reproducible",
            path="$.plan.predicted_grants",
            details={"expected": expected_grants,
                     "actual": [list(g) for g in
                                recomputed.predicted_grants]})
    return {"status": "ok", "frames_replayed": len(expected),
            "strategy": strategy}


def list_snapshots(directory):
    """Stable (name-sorted) listing of snapshot files."""
    if not os.path.isdir(directory):
        return []
    return sorted(name for name in os.listdir(directory)
                  if name.endswith(".json"))
