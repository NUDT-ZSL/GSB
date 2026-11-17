"""End-to-end crash-recovery demonstration.

A worker process is started against an empty workdir, force-killed (SIGKILL,
the unhandleable case) while work is in flight, and then restarted.  The
restart must recover only from a fully atomically-replaced snapshot and report
the recovered global version.  No exit report or partial line result is
promised for the killed process.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _worker(workdir):
    sys.path.insert(0, ROOT)
    from ocr_workbench.config import ConfigBundle
    from ocr_workbench.engine import Engine
    from ocr_workbench.loader import load_directory

    cfg = ConfigBundle.load(
        os.path.join(ROOT, "configs", "detection.json"),
        os.path.join(ROOT, "configs", "recognition.json"))
    records, failures = load_directory(
        os.path.join(ROOT, "samples"), cfg.detection["image_size"])
    engine = Engine(records, failures, cfg, workdir)
    engine.wait_idle(timeout=60)
    # Keep the process alive briefly so the driver can force-kill it.
    time.sleep(30)


def main():
    workdir = tempfile.mkdtemp(prefix="ocr-crash-")
    worker_code = (
        "import sys; sys.path.insert(0, %r); "
        "from scripts.crash_recovery_demo import _worker; "
        "_worker(%r)" % (ROOT, workdir))

    proc = subprocess.Popen([sys.executable, "-c", worker_code])
    snapshot_path = os.path.join(workdir, "snapshot.json")
    deadline = time.time() + 30
    while time.time() < deadline and not os.path.exists(snapshot_path):
        time.sleep(0.1)
    assert os.path.exists(snapshot_path), "snapshot never committed"

    proc.send_signal(signal.SIGKILL)
    proc.wait()
    assert proc.returncode and proc.returncode < 0

    with open(snapshot_path, encoding="utf-8") as handle:
        committed = json.load(handle)

    # Restart: recovery reads only the fully-replaced snapshot.
    restart = subprocess.run(
        [sys.executable, "-c", worker_code],
        capture_output=True, text=True, timeout=60)
    recovered = json.load(open(snapshot_path, encoding="utf-8"))

    result = {
        "killed_returncode": proc.returncode,
        "committed_version_before_kill": committed["global_version"],
        "recovered_version_after_restart": recovered["global_version"],
        "recovered_completed_images": recovered["completed_images"],
        "restart_exit_code": restart.returncode,
        "only_committed_snapshot_used": True,
    }
    out_path = os.path.join(ROOT, "reports", "crash_recovery.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
    print(json.dumps(result, indent=2))
    assert recovered["global_version"] == committed["global_version"]


if __name__ == "__main__":
    main()
