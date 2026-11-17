"""Diagnostic error codes and the immutable Diagnostic record.

All error codes produced by the workbench are defined here so that the
parser, lineage engine, checkpoint layer and CLI share one vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict

# A field entry or derivation reference could not be resolved (bad path,
# out-of-range array index, or target field that never existed).
E_FIELD = "E_FIELD"
# A derivation declaration does not belong to any known declaration version.
E_VERSION = "E_VERSION"
# A sensitive source feeds a direct downstream field that is marked plaintext.
E_MASK_BYPASS = "E_MASK_BYPASS"
# A derivation still points at a field that was removed in a later snapshot.
E_DANGLING_DERIVE = "E_DANGLING_DERIVE"
# A concurrent writer already holds the work-directory checkpoint lock.
E_LOCK = "E_LOCK"
# A checkpoint file was truncated/corrupted before it could be completed.
E_CORRUPT_CHECKPOINT = "E_CORRUPT_CHECKPOINT"


@dataclass(frozen=True)
class Diagnostic:
    """A single, explainable diagnostic record.

``entry_seq`` carries the 1-based ordinal of the offending entry inside its
document (field or derivation list), satisfying the requirement that rejected
records be attributable to a document version and entry sequence number.
    """

    code: str
    version: str
    entry_seq: int
    message: str
    field: str = ""
    path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def sort_key(diagnostic: Diagnostic):
    """Deterministic ordering independent of input arrangement."""

    return (
        diagnostic.code,
        diagnostic.version,
        diagnostic.entry_seq,
        diagnostic.field,
        diagnostic.path,
        diagnostic.message,
    )
