from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "samples" / "registry.json"
CLOCKS = ROOT / "samples" / "clocks.json"
SHARDS = sorted((ROOT / "samples" / "shards").glob("*.json"))


def run(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "probe_ledger", *args],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def load(workspace: Path) -> subprocess.CompletedProcess[str]:
    args = [
        "load",
        "--workspace",
        str(workspace),
        "--registry",
        str(REGISTRY),
        "--clocks",
        str(CLOCKS),
    ]
    for shard in SHARDS:
        args.extend(["--shard", str(shard)])
    return run(*args)


def digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot_meta(workspace: Path) -> dict[str, Any]:
    payload = json.loads((workspace / "ledger.json").read_text(encoding="utf-8"))
    return {
        "pointer_version": payload["pointer_version"],
        "instances_digest": payload["instances_digest"],
        "shard_registry_digest": payload["shard_registry_digest"],
        "buffer_digest": payload["buffer"]["digest"],
        "state_digest": digest_file(workspace / "ledger.json"),
    }


def main() -> int:
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "build_samples.py")],
        cwd=str(ROOT),
        check=True,
    )
    base = Path(tempfile.mkdtemp(prefix="probe-ledger-acceptance-"))
    normal_1 = base / "normal-1"
    normal_2 = base / "normal-2"
    recovery = base / "recovery"
    exits: dict[str, int] = {}

    for name, workspace in (("normal-1", normal_1), ("normal-2", normal_2)):
        loaded = load(workspace)
        exits[f"{name}-load"] = loaded.returncode
        reconciled = run("reconcile", "--workspace", str(workspace))
        exits[f"{name}-reconcile"] = reconciled.returncode

    loaded = load(recovery)
    exits["recovery-load"] = loaded.returncode
    killed = run(
        "reconcile",
        "--workspace",
        str(recovery),
        "--fault-after-transitions",
        "7",
    )
    exits["recovery-killed"] = killed.returncode
    resumed = run("resume", "--workspace", str(recovery))
    exits["recovery-resume"] = resumed.returncode

    verified: dict[str, int] = {}
    for name, workspace in (
        ("normal-1", normal_1),
        ("normal-2", normal_2),
        ("recovery", recovery),
    ):
        checked = run(
            "verify",
            "--workspace",
            str(workspace),
            "--registry",
            str(REGISTRY),
            "--clocks",
            str(CLOCKS),
            "--shard-dir",
            str(ROOT / "samples" / "shards"),
        )
        verified[name] = checked.returncode

    first_bytes = (normal_1 / "ledger.json").read_bytes()
    second_bytes = (normal_2 / "ledger.json").read_bytes()
    recovery_bytes = (recovery / "ledger.json").read_bytes()
    normal_identical = first_bytes == second_bytes
    recovery_identical = first_bytes == recovery_bytes
    expected_exits = {
        "normal-1-load": 0,
        "normal-1-reconcile": 0,
        "normal-2-load": 0,
        "normal-2-reconcile": 0,
        "recovery-load": 0,
        "recovery-killed": 137,
        "recovery-resume": 0,
    }
    ok = (
        exits == expected_exits
        and all(code == 0 for code in verified.values())
        and normal_identical
        and recovery_identical
    )
    payload = {
        "ok": ok,
        "exit_codes": exits,
        "verify_exit_codes": verified,
        "two_full_runs_byte_identical": normal_identical,
        "forced_recovery_byte_identical": recovery_identical,
        "normal_run_1": snapshot_meta(normal_1),
        "normal_run_2": snapshot_meta(normal_2),
        "recovery": snapshot_meta(recovery),
        "workspace": str(base),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
