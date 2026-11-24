from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import atomic_write_json, canonical_json_bytes, safe_read_json, sha256_value


@dataclass(frozen=True)
class ReadResult:
    status: str
    version: str | None = None
    payload: Any = None
    reason: str | None = None


class ChunkStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.chunks_dir = self.state_dir / "chunks"
        self.stale_dir = self.state_dir / "stale"
        self.chunks_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, logical_key: str) -> Path:
        parts = [part for part in logical_key.split("/") if part]
        if not parts or any(part in {".", ".."} for part in parts):
            raise ValueError(f"非法分块键: {logical_key}")
        return self.chunks_dir.joinpath(*parts).with_suffix(".json")

    def read_valid(self, logical_key: str, expected_version: str) -> ReadResult:
        path = self.path_for(logical_key)
        if not path.exists():
            return ReadResult(status="missing")
        try:
            wrapper = safe_read_json(path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return ReadResult(status="invalid", reason="unreadable_json")
        if not isinstance(wrapper, dict):
            return ReadResult(status="invalid", reason="bad_wrapper")
        actual_version = wrapper.get("version")
        if not isinstance(actual_version, str):
            return ReadResult(status="invalid", reason="missing_version")
        if actual_version != expected_version:
            return ReadResult(
                status="version_conflict",
                version=actual_version,
                reason="version_mismatch",
            )
        payload = wrapper.get("payload")
        payload_sha = wrapper.get("payload_sha256")
        if not isinstance(payload_sha, str):
            return ReadResult(
                status="invalid", version=actual_version, reason="missing_payload_checksum"
            )
        if sha256_value(payload) != payload_sha:
            return ReadResult(
                status="invalid", version=actual_version, reason="payload_checksum_mismatch"
            )
        return ReadResult(status="valid", version=actual_version, payload=payload)

    def _archive_existing(self, path: Path, logical_key: str, version: str | None) -> None:
        if not path.exists():
            return
        stale_name = f"{logical_key}.{((version or 'unknown')[:16])}.stale.json"
        stale_path = self.stale_dir.joinpath(*[part for part in stale_name.split("/") if part])
        stale_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, stale_path)

    def write(self, logical_key: str, version: str, payload: Any) -> dict[str, Any]:
        path = self.path_for(logical_key)
        prior_version: str | None = None
        if path.exists():
            try:
                wrapper = safe_read_json(path)
                if isinstance(wrapper, dict) and isinstance(wrapper.get("version"), str):
                    prior_version = wrapper["version"]
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                prior_version = None
            if prior_version != version:
                self._archive_existing(path, logical_key, prior_version)

        wrapper = {
            "schema_version": 1,
            "version": version,
            "payload_sha256": sha256_value(payload),
            "payload": payload,
        }
        body = json.dumps(
            wrapper,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        from .util import atomic_write_bytes

        atomic_write_bytes(path, body)
        return {
            "status": "written",
            "path": str(path),
            "previous_version": prior_version,
            "version_changed": prior_version is not None and prior_version != version,
        }

    def write_manifest(self, payload: Any) -> None:
        atomic_write_json(self.state_dir / "manifest.json", payload)

    def read_manifest(self) -> dict[str, Any] | None:
        path = self.state_dir / "manifest.json"
        if not path.exists():
            return None
        try:
            value = safe_read_json(path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def write_interruption(
        self,
        *,
        exit_code: int,
        signal_name: str,
        phase: str,
        track: str | None,
        frame_interval: list[int] | None,
        chunks_completed: int,
    ) -> None:
        atomic_write_json(
            self.state_dir / "interruption.json",
            {
                "schema_version": 1,
                "interrupted": True,
                "exit_code": int(exit_code),
                "signal": signal_name,
                "phase": phase,
                "track": track,
                "frame_interval": frame_interval,
                "chunks_completed": int(chunks_completed),
            },
        )

    def read_interruption(self) -> dict[str, Any] | None:
        path = self.state_dir / "interruption.json"
        if not path.exists():
            return None
        try:
            value = safe_read_json(path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(value, dict) or not value.get("interrupted"):
            return None
        return value

    def clear_interruption(self) -> None:
        (self.state_dir / "interruption.json").unlink(missing_ok=True)


def version_from_payload(payload: Any) -> str:
    return sha256_value(payload)


__all__ = [
    "ChunkStore",
    "ReadResult",
    "canonical_json_bytes",
    "version_from_payload",
]

