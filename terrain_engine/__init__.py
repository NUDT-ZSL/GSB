"""Procedural tiled terrain off-screen render engine.

The package is deliberately dependency-light: numpy for the numeric kernels
and the Python standard library for everything else (json, sqlite3, threading,
pathlib, ...).  All public entry points are deterministic: given the same
inputs they produce byte-identical results regardless of tile partitioning,
thread count or call order.
"""

__version__ = "1.0.0"

