from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import (
    atomic_write_bytes,
    canonical_bytes,
    sha256_bytes,
    sha256_file,
    strict_json_loads,
)
from .errors import (
    E_IO,
    E_SPILL_READBACK_FAILED,
    E_SPILL_TAMPERED,
    EngineError,
)
from .filelock import FileLock
from .models import GroupState


@dataclass(frozen=True, slots=True)
class SpillRecord:
    spill_id: str
    rel_path: str
    absolute_path: Path
    sha256: str
    size_bytes: int
    reused: bool


@dataclass(frozen=True, slots=True)
class SpillRead:
    state: GroupState
    sha256: str
    size_bytes: int
    rel_path: str


class SpillManager:
    def __init__(self, root: str | Path, spill_dir: str) -> None:
        self.root = Path(root).resolve()
        self.base = (self.root / spill_dir).resolve()
        try:
            self.base.relative_to(self.root)
        except ValueError as exc:
            raise EngineError(E_IO, "spill directory escapes the workspace root") from exc
        self.base.mkdir(parents=True, exist_ok=True)

    def _payload(
        self,
        mode: str,
        state: GroupState,
        state_json: str,
    ) -> dict[str, Any]:
        return {
            "schema": "colagg-spill-v1",
            "mode": mode,
            "group_hash": state.group_hash,
            "group_key": list(state.key),
            "version": state.version,
            "state_json": strict_json_loads(state_json),
        }

    def spill_id(
        self,
        mode: str,
        state: GroupState,
        state_json: str,
    ) -> str:
        return sha256_bytes(canonical_bytes(self._payload(mode, state, state_json)))[:32]

    def materialize(
        self,
        *,
        mode: str,
        state: GroupState,
        state_json: str,
        tamper: bool = False,
    ) -> SpillRecord:
        spill_id = self.spill_id(mode, state, state_json)
        filename = (
            f"spill-{mode}-{state.group_hash[:16]}-v{state.version}-{spill_id[:16]}.json"
        )
        absolute = self.base / filename
        rel_path = str(absolute.relative_to(self.root))
        lock_path = absolute.with_suffix(absolute.suffix + ".lock")
        payload_bytes = canonical_bytes(self._payload(mode, state, state_json))
        expected_digest = sha256_bytes(payload_bytes)
        reused = False

        with FileLock(lock_path):
            if absolute.exists():
                actual = sha256_file(absolute)
                if actual != expected_digest:
                    raise EngineError(
                        E_SPILL_TAMPERED,
                        "existing spill file checksum does not match its deterministic content",
                        spill_id=spill_id,
                        path=rel_path,
                        expected_digest=expected_digest,
                        actual_digest=actual,
                    )
                reused = True
            else:
                atomic_write_bytes(absolute, payload_bytes)
            if tamper:
                with open(absolute, "ab") as handle:
                    handle.write(b"\nTAMPERED\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                actual = sha256_file(absolute)
                raise EngineError(
                    E_SPILL_TAMPERED,
                    "injected spill file tampering after atomic rename",
                    spill_id=spill_id,
                    path=rel_path,
                    expected_digest=expected_digest,
                    actual_digest=actual,
                )

        return SpillRecord(
            spill_id=spill_id,
            rel_path=rel_path,
            absolute_path=absolute,
            sha256=expected_digest,
            size_bytes=len(payload_bytes),
            reused=reused,
        )

    def read(
        self,
        *,
        mode: str,
        spill_id: str,
        rel_path: str,
        expected_digest: str,
        inject_corrupt: bool = False,
    ) -> SpillRead:
        absolute = (self.root / rel_path).resolve()
        try:
            absolute.relative_to(self.base)
        except ValueError as exc:
            raise EngineError(
                E_SPILL_READBACK_FAILED,
                "spill path escapes the configured spill directory",
                spill_id=spill_id,
                path=rel_path,
            ) from exc
        lock_path = absolute.with_suffix(absolute.suffix + ".lock")
        with FileLock(lock_path):
            if not absolute.exists():
                raise EngineError(
                    E_SPILL_READBACK_FAILED,
                    "spill file is missing",
                    spill_id=spill_id,
                    path=rel_path,
                )
            if inject_corrupt:
                with open(absolute, "ab") as handle:
                    handle.write(b"\nINJECTED_READBACK_CORRUPTION\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            actual = sha256_file(absolute)
            if actual != expected_digest:
                raise EngineError(
                    E_SPILL_READBACK_FAILED,
                    "spill file checksum verification failed during readback",
                    spill_id=spill_id,
                    path=rel_path,
                    expected_digest=expected_digest,
                    actual_digest=actual,
                )
            data = absolute.read_bytes()
            size_bytes = len(data)
        try:
            payload = strict_json_loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise EngineError(
                E_SPILL_READBACK_FAILED,
                "spill file is not valid canonical JSON",
                spill_id=spill_id,
                path=rel_path,
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != "colagg-spill-v1"
            or payload.get("mode") != mode
            or payload.get("group_hash") is None
        ):
            raise EngineError(
                E_SPILL_READBACK_FAILED,
                "spill file header is inconsistent",
                spill_id=spill_id,
                path=rel_path,
            )
        try:
            state = GroupState.from_payload(payload["state_json"], mode)
        except (KeyError, TypeError, ValueError) as exc:
            raise EngineError(
                E_SPILL_READBACK_FAILED,
                "spill state payload is malformed",
                spill_id=spill_id,
                path=rel_path,
            ) from exc
        if state.group_hash != payload["group_hash"]:
            raise EngineError(
                E_SPILL_READBACK_FAILED,
                "spill group hash is inconsistent",
                spill_id=spill_id,
                path=rel_path,
            )
        return SpillRead(
            state=state,
            sha256=actual,
            size_bytes=size_bytes,
            rel_path=rel_path,
        )

    def cleanup_orphans(self, referenced: set[str]) -> dict[str, Any]:
        removed_temps = 0
        removed_orphans = 0
        retained = 0
        for path in sorted(self.base.glob(".tmp-*")):
            try:
                path.unlink()
                removed_temps += 1
            except FileNotFoundError:
                pass
        for path in sorted(self.base.glob("spill-*.json")):
            rel = str(path.relative_to(self.root))
            if rel in referenced:
                retained += 1
                continue
            try:
                path.unlink()
                removed_orphans += 1
            except FileNotFoundError:
                pass
        return {
            "removed_temp_files": removed_temps,
            "removed_orphan_spill_files": removed_orphans,
            "retained_spill_files": retained,
        }
