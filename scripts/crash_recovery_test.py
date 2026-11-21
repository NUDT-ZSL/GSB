from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def run(workspace: Path, *arguments: str, crash: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if crash:
        env["WADIAG_CRASH_POINT"] = crash
    return subprocess.run(
        [sys.executable, "-m", "wadiag", "--workspace", str(workspace), *arguments],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def parse(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    if not result.stdout.strip():
        return {}
    return json.loads(result.stdout)


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="wadiag-crash-test-"))
    load = run(workspace, "load")
    if load.returncode != 0:
        print(load.stdout + load.stderr, file=sys.stderr)
        return 1
    crashed = run(workspace, "flush", crash="flush_files")
    if crashed.returncode != 137:
        print(f"expected forced exit 137, got {crashed.returncode}", file=sys.stderr)
        return 1
    status_before = parse(run(workspace, "status"))
    resumed = parse(run(workspace, "resume"))
    verified = parse(run(workspace, "verify"))
    output = {
        "workspace": str(workspace),
        "forced_exit_code": crashed.returncode,
        "pending_before_resume": status_before.get("pending_prepared"),
        "remaining_after_resume": resumed.get("remaining_prepared"),
        "verify_ok": verified.get("ok"),
        "verify_errors": verified.get("errors"),
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if output["verify_ok"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
