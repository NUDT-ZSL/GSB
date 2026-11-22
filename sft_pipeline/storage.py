from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .errors import (
    BusinessError,
    E_CACHE_CORRUPT,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    E_STAGE_ORDER,
    E_STATE_INCONSISTENT,
)
from .stages import STAGE_NAMES
from .util import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    read_json_file,
    sha256_bytes,
    sha256_json,
)
from .workspace import Workspace


MANIFEST_SCHEMA = 1
SNAPSHOT_SCHEMA = 1


def committed_stages(state: dict[str, Any]) -> list[str]:
    return [stage for stage in STAGE_NAMES if stage in state["stages"]]


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA,
        "next_version": 1,
        "generation": 0,
        "stages": {},
    }


def _manifest_envelope(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA,
        "checksum": sha256_json(state),
        "state": state,
    }


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class ProcessLock:
    """Non-blocking cross-process lock with stale-owner recovery."""

    def __init__(self, path: Path, label: str, busy_code: str = E_STAGE_BUSY) -> None:
        self.path = path
        self.label = label
        self.busy_code = busy_code
        self.owner_pid = os.getpid()
        self._acquired = False

    def _busy(self, details: dict[str, Any] | None = None) -> BusinessError:
        return BusinessError(
            self.busy_code,
            f"{self.label} is busy",
            details=details or {},
        )

    def acquire(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(3):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                try:
                    raw = self.path.read_text(encoding="ascii").strip()
                    owner = int(raw) if raw else None
                except (OSError, ValueError):
                    owner = None
                if owner is not None and _pid_running(owner):
                    raise self._busy({"owner_pid": owner})
                if owner is None:
                    try:
                        age = time.time() - self.path.stat().st_mtime
                    except OSError:
                        age = 0.0
                    if age < 2.0:
                        raise self._busy({"owner_pid": None, "reason": "lock_init"})
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise self._busy({"owner_pid": owner}) from exc
                continue
            try:
                os.write(descriptor, f"{self.owner_pid}\n".encode("ascii"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._acquired = True
            return self
        raise self._busy({"reason": "stale_lock_race"})

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            raw = self.path.read_text(encoding="ascii").strip()
        except OSError:
            raw = ""
        if raw == str(self.owner_pid):
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self._acquired = False

    def __enter__(self) -> "ProcessLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


class SnapshotStore:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        workspace.ensure_layout()
        self.operation_lock = ProcessLock(
            workspace.locks_dir / "pipeline.lock", "pipeline operation"
        )
        self.commit_lock = ProcessLock(
            workspace.locks_dir / "commit.lock", "stage commit"
        )

    def ensure_manifest(self) -> dict[str, Any]:
        if not self.workspace.manifest_path.exists():
            state = _empty_state()
            atomic_write_json(
                self.workspace.manifest_path, _manifest_envelope(state)
            )
        return self.load_manifest()

    def _manifest_corrupt(
        self, message: str, version: int = 0, **details: Any
    ) -> BusinessError:
        return BusinessError(
            E_SNAPSHOT_CORRUPT,
            message,
            stage="manifest",
            details={"version": version, **details},
        )

    def load_manifest(self) -> dict[str, Any]:
        if not self.workspace.manifest_path.exists():
            return _empty_state()
        try:
            envelope = read_json_file(self.workspace.manifest_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise self._manifest_corrupt(f"cannot read manifest: {exc}") from exc
        if not isinstance(envelope, dict):
            raise self._manifest_corrupt("manifest envelope must be an object")
        state = envelope.get("state")
        checksum = envelope.get("checksum")
        if not isinstance(state, dict) or not isinstance(checksum, str):
            raise self._manifest_corrupt("manifest envelope is incomplete")
        version = state.get("next_version", 0)
        if sha256_json(state) != checksum:
            raise self._manifest_corrupt(
                "manifest checksum mismatch", version=int(version or 0)
            )
        try:
            self._validate_state(state)
        except BusinessError as exc:
            if exc.code == E_SNAPSHOT_CORRUPT:
                raise
            raise self._manifest_corrupt(
                exc.message,
                version=int(version or 0),
                details=exc.as_dict(),
            ) from exc
        return state

    def _validate_state(self, state: dict[str, Any]) -> None:
        if state.get("schema_version") != MANIFEST_SCHEMA:
            raise BusinessError(
                E_STATE_INCONSISTENT, "unsupported manifest schema"
            )
        next_version = state.get("next_version")
        generation = state.get("generation")
        stages = state.get("stages")
        if (
            isinstance(next_version, bool)
            or not isinstance(next_version, int)
            or next_version < 1
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 0
            or not isinstance(stages, dict)
        ):
            raise BusinessError(
                E_STATE_INCONSISTENT, "manifest counters or stages are invalid"
            )
        expected_names = list(STAGE_NAMES[: len(stages)])
        if set(stages.keys()) != set(expected_names):
            raise BusinessError(
                E_STATE_INCONSISTENT,
                "committed stages must form a contiguous prefix",
                details={"found": list(stages.keys())},
            )
        previous_version = 0
        for stage_name in expected_names:
            entry = stages[stage_name]
            if not isinstance(entry, dict):
                raise BusinessError(
                    E_STATE_INCONSISTENT, "stage entry must be an object"
                )
            version = entry.get("version")
            if (
                isinstance(version, bool)
                or not isinstance(version, int)
                or version <= previous_version
                or version >= next_version
            ):
                raise BusinessError(
                    E_STATE_INCONSISTENT,
                    "stage versions must be strictly increasing and below next_version",
                    stage=stage_name,
                )
            previous_version = version
            for field in ("checksum", "cache_key", "snapshot"):
                if not isinstance(entry.get(field), str) or not entry[field]:
                    raise BusinessError(
                        E_STATE_INCONSISTENT,
                        f"stage entry field {field} is invalid",
                        stage=stage_name,
                    )

    def head_token(self, state: dict[str, Any] | None = None) -> str:
        current = state if state is not None else self.load_manifest()
        return sha256_json(current)

    def _write_state(self, state: dict[str, Any]) -> None:
        atomic_write_json(
            self.workspace.manifest_path, _manifest_envelope(state)
        )

    def _snapshot_path(self, stage: str, version: int) -> Path:
        return (
            self.workspace.snapshots_dir
            / stage
            / f"v{version:06d}.json"
        )

    def _snapshot_relative(self, stage: str, version: int) -> str:
        path = self._snapshot_path(stage, version)
        return path.relative_to(self.workspace.root).as_posix()

    def _expected_completed_ids(
        self,
        state: dict[str, Any],
        stage: str,
        payload: dict[str, Any],
    ) -> list[str]:
        index = STAGE_NAMES.index(stage)
        if index == 0:
            records = payload.get("records")
            if not isinstance(records, list):
                raise BusinessError(
                    E_STATE_INCONSISTENT,
                    "ingest payload has no records",
                    stage=stage,
                )
            return sorted(record["sample_id"] for record in records)
        previous_stage = STAGE_NAMES[index - 1]
        previous_payload = self.read_snapshot(
            previous_stage, state["stages"][previous_stage]
        )["payload"]
        if stage in {"normalize", "quality", "dedupe"}:
            return list(previous_payload["completed_sample_ids"])
        previous_records = previous_payload.get("records")
        if not isinstance(previous_records, list):
            raise BusinessError(
                E_STATE_INCONSISTENT,
                "upstream payload has no records",
                stage=stage,
            )
        if stage == "sample":
            return sorted(
                record["sample_id"]
                for record in previous_records
                if record.get("dedupe", {}).get("kept")
            )
        return sorted(record["sample_id"] for record in previous_records)

    def _cache_object_path(self, checksum: str) -> Path:
        return self.workspace.cache_objects_dir / f"{checksum}.json"

    def _cache_key_path(self, cache_key: str) -> Path:
        return self.workspace.cache_keys_dir / f"{cache_key}.json"

    def _put_cache_object(
        self,
        stage: str,
        checksum: str,
        payload_bytes: bytes,
    ) -> None:
        object_path = self._cache_object_path(checksum)
        if object_path.exists():
            existing = object_path.read_bytes()
            if sha256_bytes(existing) != checksum:
                raise BusinessError(
                    E_CACHE_CORRUPT,
                    "cache object checksum mismatch",
                    stage=stage,
                    details={"content_checksum": checksum},
                )
        else:
            atomic_write_bytes(object_path, payload_bytes)

    def _publish_cache_key(
        self,
        stage: str,
        cache_key: str,
        checksum: str,
        dependency_digest: str,
    ) -> None:
        key_path = self._cache_key_path(cache_key)
        key_record = {
            "stage": stage,
            "cache_key": cache_key,
            "content_checksum": checksum,
            "dependency_digest": dependency_digest,
            "object": f"{checksum}.json",
        }
        if key_path.exists():
            try:
                current = read_json_file(key_path)
            except (OSError, json.JSONDecodeError) as exc:
                raise BusinessError(
                    E_CACHE_CORRUPT,
                    "cache key index is corrupt",
                    stage=stage,
                    details={"cache_key": cache_key},
                ) from exc
            if current != key_record:
                raise BusinessError(
                    E_CACHE_CORRUPT,
                    "cache key maps to different content",
                    stage=stage,
                    details={"cache_key": cache_key},
                )
        else:
            atomic_write_json(key_path, key_record)

    def _preflight_cache_key(
        self,
        stage: str,
        cache_key: str,
        checksum: str,
        dependency_digest: str,
    ) -> None:
        key_path = self._cache_key_path(cache_key)
        if not key_path.exists():
            return
        expected = {
            "stage": stage,
            "cache_key": cache_key,
            "content_checksum": checksum,
            "dependency_digest": dependency_digest,
            "object": f"{checksum}.json",
        }
        try:
            current = read_json_file(key_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache key index is corrupt",
                stage=stage,
                details={"cache_key": cache_key},
            ) from exc
        if current != expected:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache key maps to different content",
                stage=stage,
                details={"cache_key": cache_key},
            )

    def publish_snapshot_cache(
        self,
        stage: str,
        entry: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        """Ensure a committed snapshot has a published cache index.

        Cache indexes are published only after the manifest commit. This
        method is used during reconciliation to finish publication if a
        process died between the manifest replace and the index write.
        """
        payload_bytes = canonical_json_bytes(payload)
        checksum = sha256_bytes(payload_bytes)
        if checksum != entry["checksum"]:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "cannot publish cache from mismatched snapshot payload",
                stage=stage,
                details={"version": entry.get("version")},
            )
        self._put_cache_object(stage, checksum, payload_bytes)
        self._publish_cache_key(
            stage,
            entry["cache_key"],
            checksum,
            entry["dependency_digest"],
        )

    def get_cached(self, stage: str, cache_key: str) -> dict[str, Any] | None:
        key_path = self._cache_key_path(cache_key)
        if not key_path.exists():
            return None
        try:
            key_record = read_json_file(key_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache key index is corrupt",
                stage=stage,
                details={"cache_key": cache_key},
            ) from exc
        if (
            not isinstance(key_record, dict)
            or key_record.get("stage") != stage
            or key_record.get("cache_key") != cache_key
        ):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache key metadata mismatch",
                stage=stage,
                details={"cache_key": cache_key},
            )
        checksum = key_record.get("content_checksum")
        if not isinstance(checksum, str):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache key has no content checksum",
                stage=stage,
            )
        object_path = self._cache_object_path(checksum)
        if not object_path.exists():
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache object is missing",
                stage=stage,
                details={"content_checksum": checksum},
            )
        raw = object_path.read_bytes()
        if sha256_bytes(raw) != checksum:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache object checksum mismatch",
                stage=stage,
                details={"content_checksum": checksum},
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache object is not valid JSON",
                stage=stage,
            ) from exc
        if not isinstance(payload, dict) or payload.get("stage") != stage:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache object stage mismatch",
                stage=stage,
            )
        return payload

    def commit_payload(
        self,
        *,
        stage: str,
        payload: dict[str, Any],
        cache_key: str,
        upstream_checksum: str | None,
        dependency_digest: str,
        expected_head: str | None = None,
        requested_version: int | None = None,
    ) -> dict[str, Any]:
        if stage not in STAGE_NAMES:
            raise BusinessError(E_STAGE_ORDER, "unknown stage", stage=stage)
        if payload.get("stage") != stage:
            raise BusinessError(
                E_STATE_INCONSISTENT,
                "payload stage does not match commit stage",
                stage=stage,
            )
        payload_bytes = canonical_json_bytes(payload)
        checksum = sha256_bytes(payload_bytes)
        completed_ids = payload.get("completed_sample_ids", [])
        if not isinstance(completed_ids, list) or any(
            not isinstance(identifier, str) for identifier in completed_ids
        ):
            raise BusinessError(
                E_STATE_INCONSISTENT,
                "completed_sample_ids must be a string list",
                stage=stage,
            )
        if completed_ids != sorted(set(completed_ids)):
            raise BusinessError(
                E_STATE_INCONSISTENT,
                "completed_sample_ids must be sorted and unique",
                stage=stage,
            )
        if stage == "sample":
            processed_count = int(payload.get("candidate_count", len(completed_ids)))
            output_count = int(payload.get("actual_total", 0))
        elif stage == "export":
            processed_count = len(completed_ids)
            output_count = int(payload.get("record_count", len(completed_ids)))
        else:
            processed_count = len(completed_ids)
            output_count = len(completed_ids)

        with self.commit_lock:
            state = self.load_manifest()
            if expected_head is not None and self.head_token(state) != expected_head:
                raise BusinessError(
                    E_STAGE_BUSY,
                    "pipeline state changed before commit",
                    stage=stage,
                    details={"expected_head": expected_head},
                )
            index = STAGE_NAMES.index(stage)
            current_names = committed_stages(state)
            if stage in state["stages"]:
                if requested_version is not None:
                    raise BusinessError(
                        E_SNAPSHOT_ROLLBACK,
                        "cannot replace a committed stage with an older version",
                        stage=stage,
                        details={
                            "requested_version": requested_version,
                            "committed_version": state["stages"][stage]["version"],
                        },
                    )
                raise BusinessError(
                    E_STAGE_ORDER,
                    "stage is already committed; invalidate it before replacement",
                    stage=stage,
                )
            if len(current_names) != index:
                raise BusinessError(
                    E_STAGE_ORDER,
                    "stage cannot commit before its predecessor",
                    stage=stage,
                    details={"committed_stages": current_names},
                )
            if index > 0:
                previous = state["stages"][STAGE_NAMES[index - 1]]
                if previous["checksum"] != upstream_checksum:
                    raise BusinessError(
                        E_STATE_INCONSISTENT,
                        "upstream snapshot checksum changed",
                        stage=stage,
                    )
            elif upstream_checksum is not None:
                raise BusinessError(
                    E_STATE_INCONSISTENT,
                    "ingest stage must not have an upstream checksum",
                    stage=stage,
                )
            expected_completed = self._expected_completed_ids(
                state, stage, payload
            )
            if completed_ids != expected_completed:
                raise BusinessError(
                    E_STATE_INCONSISTENT,
                    "partial stage commit is not allowed",
                    stage=stage,
                    details={
                        "expected_count": len(expected_completed),
                        "completed_count": len(completed_ids),
                    },
                )

            if requested_version is not None:
                if (
                    requested_version < state["next_version"]
                    or requested_version > state["next_version"]
                ):
                    raise BusinessError(
                        E_SNAPSHOT_ROLLBACK,
                        "snapshot version is not the next monotonic version",
                        stage=stage,
                        details={
                            "requested_version": requested_version,
                            "next_version": state["next_version"],
                        },
                    )
                version = requested_version
            else:
                version = state["next_version"]

            self._put_cache_object(stage, checksum, payload_bytes)
            self._preflight_cache_key(
                stage, cache_key, checksum, dependency_digest
            )
            snapshot = {
                "snapshot_schema": SNAPSHOT_SCHEMA,
                "stage": stage,
                "stage_index": index,
                "version": version,
                "status": "committed",
                "cache_key": cache_key,
                "upstream_checksum": upstream_checksum,
                "dependency_digest": dependency_digest,
                "content_checksum": checksum,
                "completed_sample_ids": completed_ids,
                "payload": payload,
            }
            snapshot_path = self._snapshot_path(stage, version)
            atomic_write_json(snapshot_path, snapshot)
            relative_snapshot = self._snapshot_relative(stage, version)
            entry = {
                "stage": stage,
                "version": version,
                "checksum": checksum,
                "cache_key": cache_key,
                "upstream_checksum": upstream_checksum,
                "dependency_digest": dependency_digest,
                "snapshot": relative_snapshot,
                "completed_count": len(completed_ids),
                "processed_count": processed_count,
                "output_count": output_count,
            }
            state["stages"][stage] = entry
            state["next_version"] = version + 1
            state["generation"] = int(state["generation"]) + 1
            state["last_commit"] = {"stage": stage, "version": version}
            self._write_state(state)
            self._publish_cache_key(
                stage, cache_key, checksum, dependency_digest
            )
            return {
                "entry": entry,
                "version": version,
                "checksum": checksum,
                "head": self.head_token(state),
            }

    def invalidate_from(
        self,
        stage: str,
        reason: str,
        expected_head: str | None = None,
    ) -> dict[str, Any]:
        if stage not in STAGE_NAMES:
            raise BusinessError(E_STAGE_ORDER, "unknown stage", stage=stage)
        index = STAGE_NAMES.index(stage)
        with self.commit_lock:
            state = self.load_manifest()
            if expected_head is not None and self.head_token(state) != expected_head:
                raise BusinessError(
                    E_STAGE_BUSY,
                    "pipeline state changed before invalidation",
                    stage=stage,
                )
            removed = [
                name
                for name in STAGE_NAMES[index:]
                if name in state["stages"]
            ]
            if not removed:
                return {
                    "invalidated": False,
                    "removed_stages": [],
                    "head": self.head_token(state),
                }
            state["stages"] = {
                name: state["stages"][name]
                for name in STAGE_NAMES[:index]
                if name in state["stages"]
            }
            state["generation"] = int(state["generation"]) + 1
            state["last_invalidation"] = {
                "from_stage": stage,
                "reason": reason,
                "removed_stages": removed,
            }
            self._write_state(state)
            return {
                "invalidated": True,
                "removed_stages": removed,
                "head": self.head_token(state),
            }

    def read_snapshot(
        self,
        stage: str,
        entry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if stage not in STAGE_NAMES:
            raise BusinessError(E_STAGE_ORDER, "unknown stage", stage=stage)
        if entry is None:
            state = self.load_manifest()
            if stage not in state["stages"]:
                raise BusinessError(
                    E_STAGE_ORDER,
                    "stage has no committed snapshot",
                    stage=stage,
                )
            entry = state["stages"][stage]
        version = int(entry.get("version", 0))
        snapshot_rel = entry.get("snapshot")
        if not isinstance(snapshot_rel, str):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot path is missing",
                stage=stage,
                details={"version": version},
            )
        snapshot_path = self.workspace.root / snapshot_rel
        try:
            snapshot = read_json_file(snapshot_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"cannot read snapshot: {exc}",
                stage=stage,
                details={"version": version},
            ) from exc
        if not isinstance(snapshot, dict):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot must be an object",
                stage=stage,
                details={"version": version},
            )
        if (
            snapshot.get("snapshot_schema") != SNAPSHOT_SCHEMA
            or snapshot.get("stage") != stage
            or snapshot.get("stage_index") != STAGE_NAMES.index(stage)
            or snapshot.get("version") != version
            or snapshot.get("status") != "committed"
        ):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot identity or status mismatch",
                stage=stage,
                details={"version": version},
            )
        payload = snapshot.get("payload")
        checksum = snapshot.get("content_checksum")
        if not isinstance(payload, dict) or not isinstance(checksum, str):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot payload or checksum is missing",
                stage=stage,
                details={"version": version},
            )
        actual_checksum = sha256_bytes(canonical_json_bytes(payload))
        if actual_checksum != checksum or checksum != entry.get("checksum"):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot content checksum mismatch",
                stage=stage,
                details={
                    "version": version,
                    "expected": checksum,
                    "actual": actual_checksum,
                },
            )
        completed_ids = snapshot.get("completed_sample_ids")
        expected_ids = payload.get("completed_sample_ids")
        if (
            not isinstance(completed_ids, list)
            or completed_ids != sorted(set(completed_ids))
            or completed_ids != expected_ids
        ):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot completed_sample_ids are inconsistent",
                stage=stage,
                details={"version": version},
            )
        return snapshot
