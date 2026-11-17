#!/usr/bin/env python3
"""Generate deterministic config/samples/injections into a directory."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from peworkbench.cli import main

if __name__ == "__main__":
    sys.exit(int(main(["generate"] + sys.argv[1:])))
