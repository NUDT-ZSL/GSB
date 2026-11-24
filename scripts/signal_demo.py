from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    state_dir = ROOT / "artifacts" / "signal-demo"
    if state_dir.exists():
        shutil.rmtree(state_dir)
    state_dir.mkdir(parents=True)

    cmd = [
        sys.executable,
        "-m",
        "animrecalc.cli",
        "baseline",
        "--sample-dir",
        str(ROOT / "samples"),
        "--state-dir",
        str(state_dir),
        "--workers",
        "1",
        "--chunk-delay-ms",
        "250",
    ]
    kwargs: dict[str, object] = {"cwd": str(ROOT)}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

    proc = subprocess.Popen(cmd, **kwargs)  # type: ignore[arg-type]
    first_chunk = state_dir / "chunks" / "chunk_000000.json"
    deadline = time.time() + 15
    while time.time() < deadline and not first_chunk.exists():
        time.sleep(0.05)
    if not first_chunk.exists():
        proc.kill()
        raise RuntimeError("timed out waiting for first chunk")

    time.sleep(0.1)
    if os.name == "nt":
        proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
    else:
        proc.send_signal(signal.SIGTERM)
    return_code = proc.wait(timeout=20)
    marker = state_dir / "INTERRUPTED.json"
    print(
        json.dumps(
            {
                "signal_exit_code": return_code,
                "expected_exit_code": 75,
                "marker_exists": marker.exists(),
                "marker": json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if return_code != 75 or not marker.exists():
        return 1

    recover = subprocess.run(
        [
            sys.executable,
            "-m",
            "animrecalc.cli",
            "recover",
            "--sample-dir",
            str(ROOT / "samples"),
            "--state-dir",
            str(state_dir),
            "--workers",
            "2",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    print(recover.stdout)
    if recover.returncode != 0:
        print(recover.stderr, file=sys.stderr)
    return recover.returncode


if __name__ == "__main__":
    raise SystemExit(main())
