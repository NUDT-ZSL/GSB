"""Edge cache consistency teaching component.

The package is intentionally standard-library only.  A local content source and
multiple cache nodes communicate over newline-delimited JSON on loopback TCP,
while SQLite stores the versioned ledger and snapshots are plain JSON files.
"""

__version__ = "1.0.0"

