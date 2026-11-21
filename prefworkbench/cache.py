"""Content-addressed cache for every pipeline stage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import content_hash, read_json, write_json
from .errors import BusinessError, E_CACHE_CORRUPT


class ContentAddressCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def key(self, stage: str, material: Any) -> str:
        return content_hash({"cache_schema": 1, "stage": stage, "material": material})

    def _path(self, stage: str, key: str) -> Path:
        return self.root / stage / f"{key}.json"

    def get(self, stage: str, key: str) -> dict[str, Any] | None:
        path = self._path(stage, key)
        if not path.exists():
            return None
        try:
            envelope = read_json(path)
        except (OSError, ValueError) as exc:
            raise BusinessError(
                E_CACHE_CORRUPT,
                f"cannot read cache entry: {exc}",
                stage=stage,
                details={"key": key, "path": str(path)},
            ) from exc
        if not isinstance(envelope, dict):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache envelope is not an object",
                stage=stage,
                details={"key": key, "path": str(path)},
            )
        payload = envelope.get("payload")
        expected = content_hash(payload)
        if (
            envelope.get("schema") != 1
            or envelope.get("stage") != stage
            or envelope.get("key") != key
            or envelope.get("checksum") != expected
        ):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache envelope checksum or identity mismatch",
                stage=stage,
                details={"key": key, "path": str(path)},
            )
        return payload

    def put(self, stage: str, key: str, payload: Any) -> None:
        path = self._path(stage, key)
        envelope = {
            "schema": 1,
            "stage": stage,
            "key": key,
            "checksum": content_hash(payload),
            "payload": payload,
        }
        if path.exists():
            existing = self.get(stage, key)
            if existing is not None:
                if content_hash(existing) != content_hash(payload):
                    raise BusinessError(
                        E_CACHE_CORRUPT,
                        "content-addressed key collision",
                        stage=stage,
                        details={"key": key, "path": str(path)},
                    )
                return
        write_json(path, envelope)

    def stage_keys(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        if not self.root.exists():
            return result
        for stage_dir in sorted(self.root.iterdir()):
            if not stage_dir.is_dir():
                continue
            result[stage_dir.name] = sorted(path.stem for path in stage_dir.glob("*.json"))
        return result

