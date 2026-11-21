"""Local query execution and fragment index storage engine for a document DB.

Merges v1 (single-file JSON) and v2 (sharded JSON + SQLite metadata) index
snapshots into a unified in-memory index, executes a restricted query subset
with fragment fetch-back, and persists index generation / batch watermark /
fragment commit state in SQLite for crash recovery. All time decisions use an
injected logical clock in milliseconds.
"""

__version__ = "0.1.0"
