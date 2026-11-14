"""一键复现：生成全部样例场景，逐个运行 CLI，并打印退出码与产物路径。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from audition_plan.sample_data import SCENARIOS, generate_all

EXPECTED_EXIT = {
    "09_invalid_missing_field": 2,
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    samples_dir = Path(args[0]) if args else Path("samples")
    out_dir = Path(args[1]) if len(args) > 1 else Path("out")

    roots = generate_all(samples_dir)
    failures = 0
    for root in roots:
        target = out_dir / root.name
        proc = subprocess.run(
            [
                sys.executable, "-m", "audition_plan",
                "--events", str(root / "events.json"),
                "--clips", str(root / "clips.json"),
                "--out", str(target),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        expected = EXPECTED_EXIT.get(root.name, 0)
        status = "OK" if proc.returncode == expected else "FAIL"
        failures += proc.returncode != expected
        print(f"[{status}] {root.name} ({SCENARIOS[root.name]['title']}) exit={proc.returncode}")
        if proc.returncode != expected:
            print(proc.stderr.rstrip())
        elif proc.returncode == 0:
            print(f"        CSV: {target / 'audition_plan.csv'}")
            print(f"        JSON: {target / 'audition_plan_report.json'}")
        else:
            print(f"        stderr: {proc.stderr.strip()}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
