from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from .canonical import canonical_json_bytes, sha256_json
from .config import config_fingerprint
from .errors import BusinessError, E_CACHE_CORRUPT, E_STAGE_BUSY


class CacheMiss(Exception):
    pass


@dataclass(frozen=True)
class CacheResult:
    key: str
    stage: str
    node_id: str
    payload: dict[str, Any]
    hit: bool
    entry: dict[str, Any]


def _normalize_upstream(
    upstream: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    normalized = [
        {"node": str(item["node"]), "key": str(item["key"])}
        for item in (upstream or [])
    ]
    return sorted(normalized, key=lambda item: (item["node"], item["key"]))


def make_cache_key(
    *,
    stage: str,
    node_id: str,
    input_digest: str,
    config: dict[str, Any],
    upstream: list[dict[str, str]] | None = None,
) -> str:
    material = {
        "schema_version": 1,
        "stage": stage,
        "node_id": node_id,
        "input_digest": input_digest,
        "extractor_versions": config["extractor_versions"],
        "clarification_threshold": config["clarification_threshold"],
        "config_fingerprint": config_fingerprint(config),
        "upstream": _normalize_upstream(upstream),
    }
    return sha256_json(material)


class CacheStore:
    """Content-addressed JSON artifact store with non-blocking writer locks."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.locks_dir = self.root / ".locks"
        self.locks_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        if not key or any(character not in "0123456789abcdef" for character in key):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache key must be a lowercase hexadecimal digest",
                node="cache",
                details={"key": key},
            )
        return self.root / f"{key}.json"

    def _lock_path(self, key: str) -> Path:
        return self.locks_dir / f"{key}.lock"

    @contextmanager
    def begin_write(self, key: str) -> Iterator[None]:
        """Acquire a non-blocking writer lock.

        A second writer receives E_STAGE_BUSY immediately, rather than waiting.
        """

        lock_path = self._lock_path(key)
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError as exc:
            raise BusinessError(
                E_STAGE_BUSY,
                "another task is already writing this cache stage",
                node="cache",
                details={"key": key, "lock": str(lock_path.name)},
            ) from exc
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            yield
        finally:
            os.close(descriptor)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def _entry_body(
        self,
        *,
        key: str,
        stage: str,
        node_id: str,
        input_digest: str,
        config: dict[str, Any],
        upstream: list[dict[str, str]] | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        body = {
            "schema_version": 1,
            "key": key,
            "stage": stage,
            "node_id": node_id,
            "input_digest": input_digest,
            "extractor_versions": config["extractor_versions"],
            "clarification_threshold": config["clarification_threshold"],
            "config_fingerprint": config_fingerprint(config),
            "upstream": _normalize_upstream(upstream),
            "payload_hash": sha256_json(payload),
            "payload": payload,
        }
        body["entry_checksum"] = sha256_json(body)
        return body

    def _write_entry_file(self, key: str, entry: dict[str, Any]) -> None:
        data = canonical_json_bytes(entry)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{key}.",
            suffix=".tmp",
            dir=self.root,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path_for(key))
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def commit_locked(self, key: str, entry: dict[str, Any]) -> None:
        self.verify_entry(key, entry)
        self._write_entry_file(key, entry)

    def put(
        self,
        *,
        stage: str,
        node_id: str,
        input_digest: str,
        config: dict[str, Any],
        upstream: list[dict[str, str]] | None,
        payload: dict[str, Any],
    ) -> str:
        key = make_cache_key(
            stage=stage,
            node_id=node_id,
            input_digest=input_digest,
            config=config,
            upstream=upstream,
        )
        entry = self._entry_body(
            key=key,
            stage=stage,
            node_id=node_id,
            input_digest=input_digest,
            config=config,
            upstream=upstream,
            payload=payload,
        )
        with self.begin_write(key):
            self.commit_locked(key, entry)
        return key

    def read_raw(self, key: str) -> bytes:
        path = self.path_for(key)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise CacheMiss(key) from exc
        except OSError as exc:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache entry cannot be read",
                node="cache",
                details={"key": key, "error": str(exc)},
            ) from exc

    def get_entry(self, key: str) -> dict[str, Any]:
        try:
            raw = self.read_raw(key)
        except CacheMiss:
            raise
        try:
            entry = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache entry is not valid JSON",
                node="cache",
                details={"key": key},
            ) from exc
        self.verify_entry(key, entry)
        return entry

    def verify_entry(self, key: str, entry: dict[str, Any]) -> None:
        if not isinstance(entry, dict):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache entry must be an object",
                node="cache",
                details={"key": key},
            )
        stored_checksum = entry.get("entry_checksum")
        body = {field: value for field, value in entry.items() if field != "entry_checksum"}
        expected_checksum = sha256_json(body)
        if stored_checksum != expected_checksum:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache entry checksum mismatch",
                node="cache",
                details={
                    "key": key,
                    "expected": expected_checksum,
                    "actual": stored_checksum,
                },
            )
        if entry.get("key") != key:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache entry key does not match its filename",
                node="cache",
                details={"key": key, "embedded_key": entry.get("key")},
            )
        payload = entry.get("payload")
        if sha256_json(payload) != entry.get("payload_hash"):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache payload checksum mismatch",
                node="cache",
                details={"key": key},
            )

    def has(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def get_or_compute(
        self,
        *,
        stage: str,
        node_id: str,
        input_digest: str,
        config: dict[str, Any],
        upstream: list[dict[str, str]] | None,
        compute: Callable[[], dict[str, Any]],
    ) -> CacheResult:
        key = make_cache_key(
            stage=stage,
            node_id=node_id,
            input_digest=input_digest,
            config=config,
            upstream=upstream,
        )
        if self.has(key):
            entry = self.get_entry(key)
            if (
                entry.get("stage") != stage
                or entry.get("node_id") != node_id
                or entry.get("input_digest") != input_digest
            ):
                raise BusinessError(
                    E_CACHE_CORRUPT,
                    "cache entry metadata does not match requested node",
                    node=node_id,
                    details={"key": key},
                )
            return CacheResult(
                key=key,
                stage=stage,
                node_id=node_id,
                payload=entry["payload"],
                hit=True,
                entry=entry,
            )

        payload = compute()
        key = self.put(
            stage=stage,
            node_id=node_id,
            input_digest=input_digest,
            config=config,
            upstream=upstream,
            payload=payload,
        )
        entry = self.get_entry(key)
        return CacheResult(
            key=key,
            stage=stage,
            node_id=node_id,
            payload=payload,
            hit=False,
            entry=entry,
        )

    def keys(self) -> list[str]:
        return sorted(path.stem for path in self.root.glob("*.json"))

    def stats(self) -> dict[str, Any]:
        keys = self.keys()
        sizes = [self.path_for(key).stat().st_size for key in keys]
        return {
            "entries": len(keys),
            "bytes": sum(sizes),
            "root": str(self.root),
        }

    def verify_all(self) -> list[str]:
        keys = self.keys()
        for key in keys:
            self.get_entry(key)
        return keys
