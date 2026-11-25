#!/usr/bin/env python3
"""Convenience entry point for the deterministic sample generator."""

from __future__ import annotations

import argparse
import json

from ragdiag.sample_generator import generate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data")
    args = parser.parse_args()
    print(
        json.dumps(
            generate(args.out),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

