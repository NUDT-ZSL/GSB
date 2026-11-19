#!/usr/bin/env python3
"""Run all configured business-error injections and print a JSON report."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from replication_diag.injections import run_injection_manifest
from replication_diag.util import stable_json_text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(ROOT / "config" / "sample.json"))
    parser.add_argument("--injections", default=str(ROOT / "config" / "injections.json"))
    parser.add_argument("--id")
    args = parser.parse_args()
    report = run_injection_manifest(args.config, args.injections, args.id)
    sys.stdout.buffer.write(stable_json_text(report).encode("utf-8"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
