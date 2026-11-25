#!/usr/bin/env python3
"""Fixed-seed entry point: python generate_sample.py --out-dir data/generated."""

from risk_governance.generator import DEFAULT_SEED, build_dataset, generate_files, main

__all__ = ["DEFAULT_SEED", "build_dataset", "generate_files", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
