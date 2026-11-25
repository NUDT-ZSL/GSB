#!/usr/bin/env python3
"""Generate deterministic local samples without installing the package."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from transcript_governance.samples import generate_sample_tree  # noqa: E402


if __name__ == "__main__":
    output = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "samples")
    paths = generate_sample_tree(output)
    print(f"generated samples in {paths.root}")
    print(f"PCM: {paths.pcm}")
    print(f"v1: {paths.v1}")
    print(f"v2 manifest: {paths.v2_dir / 'manifest.json'}")
    print(f"pcm_sha256: {paths.pcm_sha256}")
