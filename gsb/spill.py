from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import GSBCorruptStateError
from .util import atomic_write, canonical_json, sha256_bytes


_SAFE_QUERY = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_query_name(query_id: str) -> str:
    return _SAFE_QUERY.sub("_", query_id)[:120] or "query"


class SpillManager:
    def __init__(self, work_dir: Path, query_id: str) -> None:
        self.query_id = query_id
        self.directory = work_dir / "spill" / safe_query_name(query_id)

    def path_for_generation(self, generation: int) -> Path:
        return self.directory / f"state-{generation:08d}.json"

    def write(
        self,
        generation: int,
        groups: list[dict[str, Any]],
    ) -> tuple[Path, str, int]:
        payload = {
            "schema_version": 1,
            "query_id": self.query_id,
            "generation": generation,
            "groups": groups,
        }
        digest = sha256_bytes(canonical_json(payload).encode("utf-8"))
        envelope = {
            "schema_version": 1,
            "query_id": self.query_id,
            "generation": generation,
            "digest": digest,
            "payload": payload,
        }
        data = canonical_json(envelope).encode("utf-8")
        path = self.path_for_generation(generation)
        atomic_write(path, data)
        return path, digest, len(data)

    def read(self, path: Path, expected_digest: str) -> tuple[list[dict[str, Any]], int]:
        try:
            data = path.read_bytes()
            envelope = json.loads(data.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GSBCorruptStateError(
                "cannot read spill file",
                {"path": str(path), "cause": str(exc)},
            ) from exc
        payload = envelope.get("payload")
        digest = envelope.get("digest")
        if not isinstance(payload, dict) or not isinstance(digest, str):
            raise GSBCorruptStateError(
                "spill envelope is malformed",
                {"path": str(path)},
            )
        actual = sha256_bytes(canonical_json(payload).encode("utf-8"))
        if actual != digest or digest != expected_digest:
            raise GSBCorruptStateError(
                "spill digest mismatch during readback",
                {
                    "path": str(path),
                    "expected": expected_digest,
                    "envelope_digest": digest,
                    "actual": actual,
                },
            )
        groups = payload.get("groups")
        if not isinstance(groups, list):
            raise GSBCorruptStateError(
                "spill groups payload is malformed",
                {"path": str(path)},
            )
        return groups, len(data)

    def cleanup_except(self, keep: set[Path]) -> list[Path]:
        removed: list[Path] = []
        if not self.directory.is_dir():
            return removed
        for path in sorted(self.directory.glob("state-*.json")):
            if path not in keep:
                try:
                    path.unlink()
                    removed.append(path)
                except OSError:
                    pass
        return removed

