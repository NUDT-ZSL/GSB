"""Small integration-facing helpers."""

from __future__ import annotations

from typing import Any

from .errors import STAGES
from .models import OperatorResult
from .snapshot import SnapshotStore


def injection_diagnostics(result: OperatorResult) -> list[dict[str, Any]]:
    """Return only records owned by the four injectable execution stages."""

    return [
        diagnostic.to_dict()
        for diagnostic in result.diagnostics
        if diagnostic.stage in STAGES
    ]


def query_snapshot(store: SnapshotStore, category: str) -> dict[str, Any]:
    """Query a versioned snapshot through the public integration surface."""

    return store.query(category)
