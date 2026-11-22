#!/usr/bin/env python3
"""Generate deterministic local samples (JSONL manifest + .npy arrays)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from chunked_attn import samples  # noqa: E402

if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "samples"
    path = samples.generate_samples(out)
    print("samples written to %s" % path)
