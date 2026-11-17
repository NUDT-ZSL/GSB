#!/usr/bin/env python3
"""Launch the Tkinter diagnostic workbench UI."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from h2workbench.gui import launch

ROOT = os.path.join(os.path.dirname(__file__), "..")

if __name__ == "__main__":
    launch(os.path.join(ROOT, "samples"),
           os.path.join(ROOT, "artifacts", "snapshots"))
