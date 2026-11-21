"""Generate deterministic sample configs and the local batch stream."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from drift_monitor.samplegen import generate_samples


def main() -> None:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    paths = generate_samples(root, batch_count=12, sample_count=200)
    for path in paths:
        print("wrote", os.path.relpath(path, root))


if __name__ == "__main__":
    main()
