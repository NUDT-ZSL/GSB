#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = REPO_ROOT / "samples"


def _run(
    root: Path,
    *args: str,
    crash_point: str | None = None,
    expect_code: int = 0,
) -> dict[str, Any]:
    env = os.environ.copy()
    env.pop("BLOCKSTORE_CRASH_POINT", None)
    if crash_point is not None:
        env["BLOCKSTORE_CRASH_POINT"] = crash_point
    command = [
        sys.executable,
        "-m",
        "blockstore",
        "--root",
        str(root),
        "--compact",
        *args,
    ]
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != expect_code:
        raise RuntimeError(
            f"command failed ({result.returncode} != {expect_code}): "
            f"{' '.join(command)}\n{result.stdout}\n{result.stderr}"
        )
    if not result.stdout.strip():
        return {"ok": False, "exit_code": result.returncode}
    return json.loads(result.stdout)


def _require_empty_root(root: Path) -> None:
    if root.exists() and any(root.iterdir()):
        raise SystemExit(
            f"refusing to use non-empty demo root: {root}; choose a new --root"
        )
    root.mkdir(parents=True, exist_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic blockstore demo")
    parser.add_argument(
        "--root",
        help="new empty directory for normal-flow state; default is a temp directory",
    )
    args = parser.parse_args()
    base_root = (
        Path(args.root).resolve()
        if args.root
        else Path(tempfile.mkdtemp(prefix="blockstore-demo-"))
    )
    _require_empty_root(base_root)
    normal_root = base_root / "normal"
    recovery_root = base_root / "recovery"

    load = _run(
        normal_root,
        "load",
        "--volume",
        str(SAMPLES / "volume.json"),
        "--snapshots",
        str(SAMPLES / "snapshots.json"),
        "--rollbacks",
        str(SAMPLES / "rollbacks.json"),
    )
    rollback = _run(
        normal_root,
        "rollback",
        "--file",
        str(SAMPLES / "rollbacks.json"),
        "--request-id",
        "rb-main",
    )
    status_after_rollback = _run(normal_root, "status")
    gc_report = _run(normal_root, "gc")
    post_snapshot = _run(
        normal_root,
        "snapshot",
        "--file",
        str(SAMPLES / "post_rollback_snapshot.json"),
    )
    verify = _run(normal_root, "verify")

    load_crash = _run(
        recovery_root,
        "load",
        "--volume",
        str(SAMPLES / "volume.json"),
        "--snapshots",
        str(SAMPLES / "snapshots.json"),
        "--rollbacks",
        str(SAMPLES / "rollbacks.json"),
    )
    crashed = _run(
        recovery_root,
        "rollback",
        "--file",
        str(SAMPLES / "rollbacks.json"),
        "--request-id",
        "rb-main",
        crash_point="rollback:prepared",
        expect_code=97,
    )
    status_before_resume = _run(recovery_root, "status")
    resumed = _run(recovery_root, "resume")
    recovery_verify = _run(recovery_root, "verify")

    orphan_ids = sorted(
        item["snapshot_id"] for item in rollback["orphaned_snapshots"]
    )
    report = {
        "normal_flow": {
            "loaded_view_version": load["status"]["view_version"],
            "rollback_view_version": rollback["view_version"],
            "orphaned_snapshots": orphan_ids,
            "gc_deleted_blocks": gc_report["deleted_count"],
            "gc_retained_blocks": gc_report["retained_count"],
            "post_rollback_snapshot": post_snapshot["snapshot_id"],
            "verify_ok": verify["ok"],
            "live_snapshots": verify["live_snapshot_count"],
            "orphan_snapshots": verify["orphan_snapshot_count"],
        },
        "recovery_flow": {
            "load_view_version": load_crash["status"]["view_version"],
            "fault_exit_code": crashed.get("exit_code", 97),
            "view_before_resume": status_before_resume["view_version"],
            "anchor_before_resume": status_before_resume["anchor_snapshot_id"],
            "pending_rollbacks_before_resume": status_before_resume[
                "pending_operations"
            ]["rollbacks"],
            "recovery_action": resumed["recovery_actions"][0]["kind"],
            "view_after_resume": resumed["status"]["view_version"],
            "verify_ok": recovery_verify["ok"],
        },
        "roots": {
            "normal": str(normal_root),
            "recovery": str(recovery_root),
        },
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
