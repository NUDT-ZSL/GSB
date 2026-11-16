"""Snapshot mismatch and graceful signal/shutdown integration tests."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_snapshot_config_mismatch_exit_code() -> None:
    script = r"""
import os, sys, tempfile
sys.path.insert(0, %r)
from recall_bench.snapshot import SnapshotStore, SnapshotMismatch
from recall_bench.errors import E_CONFIG_MISMATCH
tmp = tempfile.mkdtemp()
store = SnapshotStore(tmp, "cfg-1", "small")
store.save("recall", 3, 9, {"k": "v"}, "digest")
store2 = SnapshotStore(tmp, "cfg-2", "small")
try:
    store2.load("recall")
except SnapshotMismatch as exc:
    assert exc.code == E_CONFIG_MISMATCH
    print("mismatch-ok")
    sys.exit(0)
sys.exit(3)
""" % ROOT
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=ROOT
    )
    assert proc.returncode == 0, proc.stderr
    assert "mismatch-ok" in proc.stdout


def test_graceful_sigterm_exit_zero() -> None:
    if os.name == "nt" or not hasattr(signal, "SIGTERM"):
        print("skip signal test on platform without SIGTERM")
        return
    runner_script = r"""
import os, sys, time
sys.path.insert(0, %r)
from recall_bench.snapshot import ShutdownController
ctrl = ShutdownController()
for _ in range(50):
    time.sleep(0.05)
    if ctrl.should_stop():
        # Stage snapshot would be flushed here by the real engine.
        print("graceful-stop")
        sys.exit(0)
sys.exit(0)
""" % ROOT
    proc = subprocess.Popen(
        [sys.executable, "-c", runner_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=ROOT,
    )
    time.sleep(0.3)
    proc.terminate()
    stdout, stderr = proc.communicate(timeout=10)
    assert proc.returncode == 0, f"expected exit 0, got {proc.returncode}: {stderr}"
    assert "graceful-stop" in stdout


def test_snapshot_file_is_complete_json() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sys.path.insert(0, ROOT)
        from recall_bench.snapshot import SnapshotStore

        store = SnapshotStore(tmp, "cfg", "medium")
        path = store.save("fusion", 7, 11, {"fusion_key": "k"}, "digest-123")
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        assert payload["stage"] == "fusion"
        assert payload["cursor"] == 7
        assert payload["config_hash"] == "cfg"
        assert payload["scale_id"] == "medium"
        assert payload["version"] >= 1


TESTS = [
    test_snapshot_config_mismatch_exit_code,
    test_graceful_sigterm_exit_zero,
    test_snapshot_file_is_complete_json,
]


def main() -> int:
    failures = 0
    for test in TESTS:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception:  # noqa: BLE001
            import traceback

            failures += 1
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
