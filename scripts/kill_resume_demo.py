from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bmm_pipeline.demo import run_demo


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", default=".demo")
    parser.add_argument("--fault-after-blocks", type=int, default=20)
    args = parser.parse_args()
    report = run_demo(
        args.work_dir, fault_after_blocks=args.fault_after_blocks
    )
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
