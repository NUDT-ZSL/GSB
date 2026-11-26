#!/usr/bin/env python3
"""Fixed-seed local sample generator.

Running this script is equivalent to ``python run.py --mode generate``.
It creates only deterministic local PCM/JSONL/SQLite fixtures.
"""
from __future__ import annotations

import json

from prosody_diag.config import Config
from prosody_diag.samplegen import generate_sample_workspace
from prosody_diag.trees import write_builtin_trees


def main() -> int:
    config = Config()
    manifest = generate_sample_workspace("samples", config)
    write_builtin_trees("samples", config)
    print(json.dumps({"ok": True, "manifest": manifest}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
