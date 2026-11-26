#!/usr/bin/env python3
"""Root-level convenience wrapper for the fixed-seed input generator."""
from kfdiff.seed import generate_seed_files

if __name__ == "__main__":
    import json

    print(json.dumps({"ok": True, **generate_seed_files(".")},
                     indent=2, ensure_ascii=False))
