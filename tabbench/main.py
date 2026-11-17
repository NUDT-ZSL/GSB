#!/usr/bin/env python3
"""tabbench CLI.

Examples:
  python3 main.py --config configs/experiment.yaml \
      --out-dir runs/c1 --max-concurrency 1
  python3 main.py --config configs/experiment.yaml \
      --out-dir runs/c4 --max-concurrency 4
  python3 tools/compare_runs.py runs/c1 runs/c4
"""

import argparse
import sys

from workbench.runner import run_workbench


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tabbench")
    parser.add_argument("--config", required=True,
                        help="experiment config YAML")
    parser.add_argument("--data", default=None,
                        help="data description YAML (overrides data_file)")
    parser.add_argument("--out-dir", default="runs/run",
                        help="output directory for snapshots/results/report")
    parser.add_argument("--max-concurrency", type=int, default=None,
                        help="override scheduler.max_concurrency")
    parser.add_argument("--fresh", action="store_true",
                        help="remove the output directory before running")
    args = parser.parse_args(argv)
    return run_workbench(
        config_path=args.config,
        data_path=args.data,
        out_dir=args.out_dir,
        max_concurrency=args.max_concurrency,
        fresh=args.fresh,
    )


if __name__ == "__main__":
    sys.exit(main())
