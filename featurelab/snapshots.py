"""Checksummed, versioned, atomically replaced snapshots."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .errors import E_SNAPSHOT_CORRUPT, FeatureLabError
from .utils import atomic_write_json, fingerprint


_SNAPSHOT_PATTERN = re.compile(r"^snapshot-(\d{6,})\.json$")
_TEMP_PATTERN = re.compile(r"^snapshot-(\d{6,})\.json\..+\.tmp$")


@dataclass(frozen=True)
class RecoveryResult:
    payload: Optional[Mapping[str, Any]]
    version: int
    failures: tuple[Mapping[str, Any], ...]
    recovered_path: Optional[str]


class SnapshotManager:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def _paths(self) -> list[tuple[int, Path]]:
        if not self.directory.exists():
            return []
        result: list[tuple[int, Path]] = []
        for path in self.directory.iterdir():
            match = _SNAPSHOT_PATTERN.match(path.name)
            if match and path.is_file():
                result.append((int(match.group(1)), path))
        return sorted(result, key=lambda item: item[0], reverse=True)

    def latest_version(self) -> int:
        paths = self._paths()
        return paths[0][0] if paths else 0

    def write(self, payload: Mapping[str, Any], *, version: Optional[int] = None) -> int:
        self.directory.mkdir(parents=True, exist_ok=True)
        next_version = self.latest_version() + 1 if version is None else int(version)
        body = dict(payload)
        envelope = {
            "format": "featurelab-snapshot-v1",
            "complete": True,
            "version": next_version,
            "checksum": fingerprint(body),
            "payload": body,
        }
        path = self.directory / f"snapshot-{next_version:06d}.json"
        atomic_write_json(path, envelope)
        return next_version

    def recover(
        self,
        *,
        expected_experiment_fingerprint: Optional[str] = None,
        expected_dataset_fingerprint: Optional[str] = None,
    ) -> RecoveryResult:
        failures: list[Mapping[str, Any]] = []
        if self.directory.exists():
            for path in sorted(self.directory.iterdir()):
                match = _TEMP_PATTERN.match(path.name)
                if match and path.is_file():
                    failures.append(
                        {
                            "code": E_SNAPSHOT_CORRUPT,
                            "message": "incomplete temporary snapshot was ignored",
                            "details": {
                                "snapshot_version": int(match.group(1)),
                                "path": str(path),
                                "error": "temporary file was never atomically replaced",
                            },
                        }
                    )
        for version, path in self._paths():
            try:
                with path.open("r", encoding="utf-8") as handle:
                    envelope = json.load(handle)
                if not isinstance(envelope, Mapping):
                    raise ValueError("snapshot envelope is not an object")
                if envelope.get("format") != "featurelab-snapshot-v1":
                    raise ValueError("unsupported snapshot format")
                if envelope.get("complete") is not True:
                    raise ValueError("snapshot is not marked complete")
                if int(envelope.get("version", -1)) != version:
                    raise ValueError("snapshot version does not match its filename")
                payload = envelope.get("payload")
                if not isinstance(payload, Mapping):
                    raise ValueError("snapshot payload is missing")
                if envelope.get("checksum") != fingerprint(payload):
                    raise ValueError("snapshot checksum mismatch")
                if expected_experiment_fingerprint is not None and payload.get(
                    "experiment_fingerprint"
                ) != expected_experiment_fingerprint:
                    raise ValueError("snapshot belongs to a different experiment")
                if expected_dataset_fingerprint is not None and payload.get(
                    "dataset_fingerprint"
                ) != expected_dataset_fingerprint:
                    raise ValueError("snapshot belongs to a different dataset")
                return RecoveryResult(
                    payload=payload,
                    version=version,
                    failures=tuple(failures),
                    recovered_path=str(path),
                )
            except Exception as exc:
                failures.append(
                    {
                        "code": E_SNAPSHOT_CORRUPT,
                        "message": "snapshot was rejected; falling back to an older complete version",
                        "details": {
                            "snapshot_version": version,
                            "path": str(path),
                            "error": str(exc),
                        },
                    }
                )
        return RecoveryResult(
            payload=None,
            version=0,
            failures=tuple(failures),
            recovered_path=None,
        )

    def read_version(self, version: int) -> Mapping[str, Any]:
        path = self.directory / f"snapshot-{version:06d}.json"
        try:
            with path.open("r", encoding="utf-8") as handle:
                envelope = json.load(handle)
            payload = envelope["payload"]
            if envelope.get("checksum") != fingerprint(payload):
                raise ValueError("snapshot checksum mismatch")
            return payload
        except Exception as exc:
            raise FeatureLabError(
                E_SNAPSHOT_CORRUPT,
                "snapshot could not be read",
                details={"snapshot_version": version, "path": str(path), "error": str(exc)},
            ) from exc
