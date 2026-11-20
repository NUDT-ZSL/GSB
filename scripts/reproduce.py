#!/usr/bin/env python3
"""Generate data, run twice with the same seed, and compare canonical bytes."""

from __future__ import annotations

import hashlib
import multiprocessing
import subprocess
import sys
from pathlib import Path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    data = root / "data" / "demo.jsonl"
    first = root / "artifacts" / "repro_first.json"
    second = root / "artifacts" / "repro_second.json"
    subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "generate_dataset.py"),
            "--output",
            str(data),
        ],
        check=True,
        cwd=root,
    )
    command = [
        sys.executable,
        "-m",
        "anomaly_engine.cli",
        "run",
        "--dataset",
        str(data),
        "--baseline-config",
        str(root / "configs" / "demo.json"),
        "--conflict-config",
        str(root / "configs" / "demo_conflict.json"),
    ]
    subprocess.run([*command, "--output", str(first)], check=True, cwd=root)
    subprocess.run([*command, "--output", str(second)], check=True, cwd=root)
    first_hash = _sha(first)
    second_hash = _sha(second)
    print(
        f'{{"identical":{str(first_hash == second_hash).lower()},'
        f'"first_sha256":"{first_hash}","second_sha256":"{second_hash}"}}'
    )
    return 0 if first_hash == second_hash else 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
