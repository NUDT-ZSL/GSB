from __future__ import annotations

import contextlib
import fcntl
import json
import os
import threading
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_dumps, sha256_obj
from .errors import (
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    BusinessError,
)


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class SnapshotStore:
    """Atomic, versioned snapshot bundle store.

    A single current pointer selects a complete immutable bundle containing
    statistics, model and batch snapshots. Category versions and checksums are
    carried inside the bundle, so readers never observe incremental patches.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.bundles_dir = self.root / "bundles"
        self.bundles_dir.mkdir(parents=True, exist_ok=True)
        self.current_path = self.root / "current.json"
        self.lock_path = self.root / "commit.lock"
        with _THREAD_LOCKS_GUARD:
            self._thread_lock = _THREAD_LOCKS.setdefault(
                str(self.lock_path.resolve()), threading.Lock()
            )

    def _busy(self) -> None:
        raise BusinessError(
            code=E_STAGE_BUSY,
            message="another snapshot commit is active",
            details={"stage": "snapshot_commit", "blocking": False},
        )

    @contextlib.contextmanager
    def commit_guard(self) -> Iterator[None]:
        if not self._thread_lock.acquire(blocking=False):
            self._busy()
        descriptor = None
        try:
            descriptor = os.open(
                self.lock_path, os.O_CREAT | os.O_RDWR, 0o600
            )
            try:
                fcntl.flock(
                    descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB
                )
            except OSError:
                self._busy()
            yield
        finally:
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
            self._thread_lock.release()

    def _read_json(self, path: Path, category: str, version: int | None) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BusinessError(
                code=E_SNAPSHOT_CORRUPT,
                message=f"snapshot {category} is unreadable or invalid",
                details={
                    "category": category,
                    "version": version,
                    "path": str(path),
                    "reason": str(exc),
                },
            ) from exc

    def current_manifest(self) -> dict[str, Any] | None:
        if not self.current_path.exists():
            return None
        manifest = self._read_json(self.current_path, "bundle", None)
        required = {
            "bundle_version",
            "bundle_checksum",
            "bundle_file",
            "categories",
        }
        if not required.issubset(manifest):
            raise BusinessError(
                code=E_SNAPSHOT_CORRUPT,
                message="snapshot current manifest is incomplete",
                details={"category": "bundle", "version": manifest.get("bundle_version")},
            )
        return manifest

    def _bundle_path(self, version: int) -> Path:
        return self.bundles_dir / f"{version:020d}.json"

    def load_bundle(self, version: int | None = None) -> dict[str, Any] | None:
        manifest = self.current_manifest()
        if manifest is None:
            return None
        current_version = int(manifest["bundle_version"])
        if version is None:
            selected_version = current_version
            selected_file = self.root / str(manifest["bundle_file"])
        else:
            if version > current_version:
                raise BusinessError(
                    code=E_SNAPSHOT_CORRUPT,
                    message="requested snapshot version is not current or committed",
                    details={
                        "category": "bundle",
                        "version": int(version),
                        "current_version": current_version,
                    },
                )
            selected_version = int(version)
            selected_file = self._bundle_path(selected_version)
        bundle = self._read_json(selected_file, "bundle", selected_version)
        if int(bundle.get("bundle_version", -1)) != selected_version:
            raise BusinessError(
                code=E_SNAPSHOT_CORRUPT,
                message="snapshot bundle version mismatch",
                details={
                    "category": "bundle",
                    "version": selected_version,
                    "found_version": bundle.get("bundle_version"),
                },
            )
        categories = bundle.get("categories", {})
        for category, record in categories.items():
            category_version = record.get("version")
            checksum = record.get("checksum")
            payload = record.get("payload")
            if int(category_version) > current_version:
                raise BusinessError(
                    code=E_SNAPSHOT_CORRUPT,
                    message="snapshot category version is ahead of current bundle",
                    details={
                        "category": category,
                        "version": category_version,
                        "current_version": current_version,
                    },
                )
            if sha256_obj(payload) != checksum:
                raise BusinessError(
                    code=E_SNAPSHOT_CORRUPT,
                    message=f"snapshot {category} checksum mismatch",
                    details={
                        "category": category,
                        "version": category_version,
                        "expected_checksum": checksum,
                        "actual_checksum": sha256_obj(payload),
                    },
                )
        bundle_checksum = sha256_obj(categories)
        expected_checksum = (
            bundle.get("bundle_checksum")
            if version is not None
            else manifest.get("bundle_checksum")
        )
        if bundle_checksum != expected_checksum:
            raise BusinessError(
                code=E_SNAPSHOT_CORRUPT,
                message="snapshot bundle checksum mismatch",
                details={
                    "category": "bundle",
                    "version": selected_version,
                    "expected_checksum": expected_checksum,
                    "actual_checksum": bundle_checksum,
                },
            )
        return bundle

    def _write_new_bundle(
        self, version: int, categories: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        path = self._bundle_path(version)
        if path.exists():
            raise BusinessError(
                code=E_SNAPSHOT_ROLLBACK,
                message="refusing to overwrite an existing snapshot version",
                details={
                    "category": "bundle",
                    "requested_version": version,
                },
            )
        bundle = {
            "bundle_version": version,
            "bundle_checksum": sha256_obj(categories),
            "categories": categories,
        }
        text = canonical_dumps(bundle)
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{id(text)}")
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return bundle

    def _write_current(self, bundle: dict[str, Any]) -> dict[str, Any]:
        version = int(bundle["bundle_version"])
        manifest = {
            "bundle_version": version,
            "bundle_checksum": bundle["bundle_checksum"],
            "bundle_file": str(self._bundle_path(version).relative_to(self.root)),
            "categories": {
                category: {
                    "version": record["version"],
                    "checksum": record["checksum"],
                }
                for category, record in bundle["categories"].items()
            },
        }
        text = canonical_dumps(manifest)
        tmp = self.current_path.with_name(
            f".{self.current_path.name}.tmp-{os.getpid()}-{id(text)}"
        )
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.current_path)
        return manifest

    def commit_bundle(
        self,
        payloads: dict[str, Any],
        *,
        requested_version: int | None = None,
    ) -> dict[str, Any]:
        required = {"statistics", "model", "batch"}
        if set(payloads) != required:
            raise ValueError(
                f"snapshot bundle requires exactly {sorted(required)}"
            )
        with self.commit_guard():
            current = self.current_manifest()
            current_version = (
                int(current["bundle_version"]) if current is not None else 0
            )
            if requested_version is not None:
                if int(requested_version) <= current_version:
                    raise BusinessError(
                        code=E_SNAPSHOT_ROLLBACK,
                        message="snapshot version rollback is not allowed",
                        details={
                            "category": "bundle",
                            "requested_version": int(requested_version),
                            "current_version": current_version,
                        },
                    )
                new_version = int(requested_version)
            else:
                new_version = current_version + 1
            categories = {
                category: {
                    "category": category,
                    "version": new_version,
                    "checksum": sha256_obj(payload),
                    "payload": payload,
                }
                for category, payload in payloads.items()
            }
            bundle = self._write_new_bundle(new_version, categories)
            manifest = self._write_current(bundle)
            return {
                "bundle_version": new_version,
                "bundle_checksum": bundle["bundle_checksum"],
                "categories": manifest["categories"],
            }

    def restore(self, version: int) -> None:
        current = self.current_manifest()
        current_version = (
            int(current["bundle_version"]) if current is not None else 0
        )
        if int(version) <= current_version:
            raise BusinessError(
                code=E_SNAPSHOT_ROLLBACK,
                message="snapshot rollback is not allowed",
                details={
                    "category": "bundle",
                    "requested_version": int(version),
                    "current_version": current_version,
                },
            )
        raise BusinessError(
            code=E_SNAPSHOT_CORRUPT,
            message="snapshot restore target has not been committed",
            details={
                "category": "bundle",
                "version": int(version),
                "current_version": current_version,
            },
        )

    def status(self) -> dict[str, Any] | None:
        manifest = self.current_manifest()
        if manifest is None:
            return None
        bundle = self.load_bundle()
        return {
            "bundle_version": manifest["bundle_version"],
            "bundle_checksum": manifest["bundle_checksum"],
            "categories": manifest["categories"],
            "loaded": bundle is not None,
        }
