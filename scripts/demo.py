from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "artifacts" / "demo.db"
RUN_ONE = ROOT / "artifacts" / "demo_run1.jsonl"
RUN_TWO = ROOT / "artifacts" / "demo_run2.jsonl"


def command_list() -> list[list[str]]:
    samples = ROOT / "samples"
    return [
        [
            "load",
            "--db",
            str(DB),
            "--topology",
            str(samples / "topology.json"),
            "--policies",
            str(samples / "policies.json"),
            "--events",
            str(samples / "events.json"),
            "--window-ms",
            "900",
            "--quota-per-second",
            "2",
            "--clock-tolerance-ms",
            "10",
            "--reset",
        ],
        ["status", "--db", str(DB)],
        ["sample", "--db", str(DB), "--advance-to", "3500"],
        [
            "sample",
            "--db",
            str(DB),
            "--advance-to",
            "4000",
            "--late-spans",
            str(samples / "late_spans.json"),
        ],
        ["query", "--db", str(DB), "--trace-id", "trace-checkout-001"],
        ["query", "--db", str(DB), "--trace-id", "trace-slow-003"],
        ["query", "--db", str(DB), "--trace-id", "trace-policy-006"],
        ["query", "--db", str(DB), "--trace-id", "trace-fast-000"],
        ["policy", "list", "--db", str(DB)],
        ["verify", "--db", str(DB)],
        ["status", "--db", str(DB)],
    ]


def run_once(output_path: Path) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key != "TAILSAMPLE_FAULT_POINT"
    }
    environment["PYTHONHASHSEED"] = "0"
    with output_path.open("w", encoding="utf-8", newline="\n") as output:
        for index, arguments in enumerate(command_list(), start=1):
            completed = subprocess.run(
                [sys.executable, "-m", "tailsample", *arguments],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=environment,
            )
            output.write(
                json.dumps(
                    {
                        "step": index,
                        "args": arguments,
                        "exit_code": completed.returncode,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            output.write("\n")
            output.write(completed.stdout)
            if completed.stderr:
                output.write(completed.stderr)
            if completed.returncode != 0:
                raise RuntimeError(
                    f"demo step {index} failed with exit code {completed.returncode}: "
                    f"{' '.join(arguments)}"
                )
    return hashlib.sha256(output_path.read_bytes()).hexdigest()


def main() -> int:
    first_hash = run_once(RUN_ONE)
    second_hash = run_once(RUN_TWO)
    identical = RUN_ONE.read_bytes() == RUN_TWO.read_bytes()
    summary = {
        "identical": identical,
        "run1_sha256": first_hash,
        "run2_sha256": second_hash,
        "run1": str(RUN_ONE.relative_to(ROOT)),
        "run2": str(RUN_TWO.relative_to(ROOT)),
        "database": str(DB.relative_to(ROOT)),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if identical else 1


if __name__ == "__main__":
    raise SystemExit(main())

