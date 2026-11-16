"""Deterministic structure fingerprints and loss-log hashes."""
from __future__ import annotations

import hashlib
import json
from typing import Iterable

from .models import UnifiedProject, LossEntry


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def structure_fingerprint(project: UnifiedProject) -> str:
    """SHA-256 over the canonical unified project content."""
    payload = canonical_json(project.to_fingerprint_dict())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def loss_log_hash(loss_log: Iterable[LossEntry]) -> str:
    payload = canonical_json([e.to_dict() for e in loss_log])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def bytes_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
