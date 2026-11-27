#!/usr/bin/env python3
"""Convenience launcher for the Tkinter operator console.

Usage (after sourcing the Tcl/Tk environment when needed)::

    source setenv.sh
    python3 run_gui.py
"""

from domecalib.gui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
