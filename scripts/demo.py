from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "state" / "demo.sqlite3"


def reset() -> None:
    shutil.rmtree(ROOT / "state", ignore_errors=True)


def invoke(command: str, *args: str) -> tuple[int, Any]:
    cmd = [
        sys.executable,
        "-m",
        "mcde",
        "--db",
        str(DB),
        "--samples",
        str(ROOT / "samples"),
        command,
        *args,
    ]
    completed = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    print(f"$ python -m mcde {' '.join(cmd[3:])}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = {
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    return completed.returncode, payload


def show(label: str, value: Any) -> None:
    print(f"{label}:")
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
    print()


def main() -> int:
    reset()
    code, load = invoke("load")
    show("load", load)
    if code:
        return code

    code, merge = invoke("merge")
    show(
        "merge",
        {
            "summary": merge["summary"],
            "clusters": {
                cluster: {
                    "status": item["status"],
                    "rejection_code": item["rejection_code"],
                    "conflict_count": item["conflict_count"],
                }
                for cluster, item in sorted(merge["clusters"].items())
            },
            "east-a checkout.replicas": next(
                key
                for key in merge["clusters"]["east-a"]["keys"]
                if key["key"] == "apps.checkout.replicas"
            ),
        },
    )
    if code:
        return code

    code, plan = invoke("plan")
    show(
        "plan",
        {
            "summary": plan["summary"],
            "window": plan["schedule"]["window"],
            "outcomes": plan["schedule"]["outcomes"],
            "rounds": plan["schedule"]["rounds"],
        },
    )
    if code:
        return code

    code, applied = invoke("apply")
    show(
        "apply",
        {
            "version": applied["version"],
            "idempotent": applied["idempotent"],
            "logical_clock_ms": applied["logical_clock_ms"],
            "summary": applied["summary"],
            "clusters": [
                {
                    "cluster": item["cluster"],
                    "decision": item["decision_status"],
                    "dispatch": item["dispatch_status"],
                    "reason": item["dispatch_reason_code"],
                    "ledger": item["ledger"],
                }
                for item in applied["clusters"]
            ],
            "rounds": applied["rounds"],
        },
    )
    if code:
        return code

    code, status = invoke("status")
    show(
        "status",
        {
            "active_version": status["active_version"],
            "logical_clock_ms": status["logical_clock_ms"],
            "summary": status["summary"],
            "clusters": status["clusters"],
        },
    )
    if code:
        return code

    code, verified = invoke("verify")
    show(
        "verify",
        {
            key: verified[key]
            for key in (
                "ok",
                "residual_count",
                "explained_residual_count",
                "unexplained_drift_count",
                "applied_clean_clusters",
                "summary",
            )
        },
    )
    if code:
        return code

    code, replay = invoke("apply")
    show(
        "idempotent replay",
        {
            "version": replay.get("version"),
            "idempotent": replay.get("idempotent"),
            "summary": replay.get("summary"),
        },
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())

