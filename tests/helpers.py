from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def make_plan(
    root: Path,
    *,
    plan_id: str = "test",
    budget_mib: float = 0.004,
    dimensions: list[str] | None = None,
    max_batch_rows: int = 64,
    quantiles: list[float] | None = None,
    read_fraction: float = 0.25,
) -> Path:
    plan = {
        "dimensions": dimensions or ["endpoint_pair", "protocol", "direction"],
        "directions": ["ingress", "egress"],
        "fixed_scale": 1000,
        "max_batch_rows": max_batch_rows,
        "max_clock_regression_ms": 500,
        "memory_budget_mib": budget_mib,
        "plan_id": plan_id,
        "protocols": ["TCP", "UDP", "QUIC"],
        "quantiles": quantiles or [0.5, 0.9],
        "read_buffer_fraction": read_fraction,
        "spill_dir": "spill",
    }
    path = root / "plan.json"
    path.write_text(json.dumps(plan, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (root / "spill").mkdir(exist_ok=True)
    return path


def record(
    seq: int,
    clock: int,
    *,
    src: str = "lo:127.0.0.1:41001",
    dst: str = "lo:127.0.0.1:42001",
    protocol: str = "TCP",
    direction: str = "ingress",
    byte_count: int = 100,
    packets: int = 2,
) -> dict[str, Any]:
    return {
        "bytes": byte_count,
        "clock_ms": clock,
        "direction": direction,
        "dst_endpoint": dst,
        "packets": packets,
        "protocol": protocol,
        "record_seq": seq,
        "src_endpoint": src,
    }


def write_batch(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("".join(stable_json(row) + "\n" for row in rows), encoding="utf-8")
    return path


def write_manifest(path: Path, batches: list[tuple[str, Path]]) -> Path:
    payload = {
        "batches": [
            {"batch_id": batch_id, "path": str(batch_path)}
            for batch_id, batch_path in batches
        ]
    }
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def run_cli(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    import os

    merged = os.environ.copy()
    merged.pop("FLOWAGG_FAULT_POINT", None)
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, "-m", "flowagg", *args],
        cwd=ROOT,
        env=merged,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )

