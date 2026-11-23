"""Canonical data types and byte-stable serialization helpers."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .errors import fail


def canonical_bytes(value: Any) -> bytes:
    """Serialize JSON-compatible data deterministically.

    Floats are rounded before serialization by the producers. ``allow_nan`` is
    disabled so a non-finite value can never silently leak into a hash.
    """

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def artifact_hash(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def round_float(value: float, decimals: int) -> float:
    if not math.isfinite(float(value)):
        return 0.0
    return round(float(value), decimals)


def quantize_tree(value: Any, decimals: int) -> Any:
    if isinstance(value, float):
        return round_float(value, decimals)
    if isinstance(value, dict):
        return {key: quantize_tree(item, decimals) for key, item in value.items()}
    if isinstance(value, list):
        return [quantize_tree(item, decimals) for item in value]
    if isinstance(value, tuple):
        return [quantize_tree(item, decimals) for item in value]
    return value


def read_json(path: str | Any) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_atomic(path: str | Any, value: Any) -> None:
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{hashlib.sha1(str(id(value)).encode()).hexdigest()[:8]}.tmp")
    data = canonical_bytes(value) + b"\n"
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        import os

        os.fsync(handle.fileno())
    import os

    os.replace(tmp, target)
    _fsync_dir(target.parent)


def _fsync_dir(path: str | Any) -> None:
    import os
    from pathlib import Path

    directory = str(Path(path))
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class ManifestEntry:
    utterance_id: str
    speaker_id: str
    ref_id: str
    start_ms: int
    end_ms: int
    text: str
    seed: int
    ref_revision: int
    precision_profile: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ManifestEntry":
        if "ref_id" not in value or value.get("ref_id") in (None, ""):
            fail(
                "E_MISSING_REF_ID",
                "ref_id 缺失",
                field="ref_id",
                details={"utterance_id": value.get("utterance_id")},
            )
        required = [
            "utterance_id",
            "speaker_id",
            "ref_id",
            "start_ms",
            "end_ms",
            "text",
            "seed",
            "ref_revision",
            "precision_profile",
        ]
        missing = [field for field in required if field not in value]
        if missing:
            fail(
                "E_MANIFEST_FIELD_MISSING",
                "样本清单缺少必需字段",
                field=missing[0],
                details={"missing_fields": missing},
            )
        return cls(
            utterance_id=str(value["utterance_id"]),
            speaker_id=str(value["speaker_id"]),
            ref_id=str(value["ref_id"]),
            start_ms=int(value["start_ms"]),
            end_ms=int(value["end_ms"]),
            text=str(value["text"]),
            seed=int(value["seed"]),
            ref_revision=int(value["ref_revision"]),
            precision_profile=str(value["precision_profile"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def with_ref_revision(self, revision: int) -> "ManifestEntry":
        return ManifestEntry(
            utterance_id=self.utterance_id,
            speaker_id=self.speaker_id,
            ref_id=self.ref_id,
            start_ms=self.start_ms,
            end_ms=self.end_ms,
            text=self.text,
            seed=self.seed,
            ref_revision=revision,
            precision_profile=self.precision_profile,
        )


def entries_to_dict(entries: Iterable[ManifestEntry]) -> list[dict[str, Any]]:
    return [entry.to_dict() for entry in entries]

