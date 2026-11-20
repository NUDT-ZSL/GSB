from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / ".demo"
DB = DEMO / "pool.db"
READY = DEMO / "echo-ready.json"


def run_cli(*arguments: str) -> str:
    command = [sys.executable, "-m", "dbpool", *arguments]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr)
        raise SystemExit(completed.returncode)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    return completed.stdout


def wait_ready(timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if READY.exists():
            json.loads(READY.read_text(encoding="utf-8"))
            return
        time.sleep(0.05)
    raise RuntimeError("echo service did not become ready")


def main() -> int:
    if DEMO.exists():
        shutil.rmtree(DEMO)
    DEMO.mkdir(parents=True)
    echo = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "tools" / "echo_server.py"),
            "--listen",
            "127.0.0.1:19091",
            "--listen",
            "127.0.0.1:19092",
            "--ready-file",
            str(READY),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_ready()
        output: dict[str, object] = {}
        output["load"] = json.loads(
            run_cli(
                "load",
                "--endpoints",
                "samples/endpoints.json",
                "--config",
                "samples/pool_config.json",
                "--trace",
                "samples/trace.json",
                "--db",
                str(DB),
            )
        )
        for seq, clock in ((1, 0), (2, 0), (3, 0), (4, 0), (5, 5), (6, 5)):
            run_cli(
                "acquire",
                "--db",
                str(DB),
                "--seq",
                str(seq),
                "--clock-ms",
                str(clock),
            )
        for seq, clock in ((6, 10), (3, 15), (2, 25)):
            run_cli(
                "release",
                "--db",
                str(DB),
                "--seq",
                str(seq),
                "--clock-ms",
                str(clock),
            )
        output["status_at_25"] = json.loads(
            run_cli("status", "--db", str(DB), "--at-clock", "25")
        )
        output["resume"] = json.loads(
            run_cli("resume", "--db", str(DB), "--clock-ms", "26")
        )
        verification_one = run_cli(
            "verify",
            "--endpoints",
            "samples/endpoints.json",
            "--config",
            "samples/pool_config.json",
            "--trace",
            "samples/trace.json",
            "--transport",
            "loopback",
        )
        verification_two = run_cli(
            "verify",
            "--endpoints",
            "samples/endpoints.json",
            "--config",
            "samples/pool_config.json",
            "--trace",
            "samples/trace.json",
            "--transport",
            "loopback",
        )
        output["verify_runs_identical"] = verification_one == verification_two
        output["verification"] = json.loads(verification_one)
        (DEMO / "verification.json").write_text(
            json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    finally:
        echo.terminate()
        try:
            echo.wait(timeout=5)
        except subprocess.TimeoutExpired:
            echo.kill()


if __name__ == "__main__":
    raise SystemExit(main())
