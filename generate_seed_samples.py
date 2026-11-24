#!/usr/bin/env python3
"""Fixed-seed sample generator entry point.

Run ``python generate_seed_samples.py samples/seed`` from the project root.
The generator uses only the Python standard library and writes deterministic
local files; it never accesses the network.
"""

from supply_chain_audit.generator import main


if __name__ == "__main__":
    raise SystemExit(main())
