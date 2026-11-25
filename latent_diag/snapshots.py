from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import EngineError
from .hashing import canonical_json_bytes, hash_json


DOMAINS = ("manifest", "budget", "ledger", "cache")


class SnapshotStore:
    """Whole-bundle, versioned snapshots with a non-blocking commit lock."""

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)
        self.root = self.state_dir / "snapshots"
        self.generations = self.root / "generations"
        self.lock_path = self.root / ".commit.lock"
        self.current_path = self.root / "CURRENT.json"
        self.generations.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

    def _envelope(self, domain: str, version: int, body: dict[str, Any]) -> dict[str, Any]:
        core = {"domain": domain, "version": version, "body": body}
        return {**core, "checksum": hash_json(core)}

    def _read_current_pointer(self) -> dict[str, Any] | None:
        if not self.current_path.exists():
            return None
        try:
            pointer = json.loads(self.current_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EngineError(
                "E_SNAPSHOT_CHECKSUM",
                "CURRENT snapshot pointer is unreadable",
                details={"path": str(self.current_path)},
            ) from exc
        expected = pointer.get("checksum")
        core = {
            key: value
            for key, value in pointer.items()
            if key not in {"checksum"}
        }
        if expected != hash_json(core):
            raise EngineError(
                "E_SNAPSHOT_CHECKSUM",
                "CURRENT snapshot pointer checksum mismatch",
                details={"path": str(self.current_path)},
            )
        return pointer

    def current_metadata(self) -> dict[str, Any] | None:
        return self._read_current_pointer()

    def load_domain(self, domain: str) -> dict[str, Any] | None:
        pointer = self._read_current_pointer()
        if pointer is None:
            return None
        domain_meta = pointer["domains"].get(domain)
        if domain_meta is None:
            raise EngineError(
                "E_SNAPSHOT_INCOMPLETE",
                f"snapshot bundle has no domain {domain}",
                details={"bundle_version": pointer.get("bundle_version")},
            )
        path = self.root / pointer["generation"] / f"{domain}.json"
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EngineError(
                "E_SNAPSHOT_CHECKSUM",
                f"snapshot file for {domain} is unreadable",
                details={"path": str(path)},
            ) from exc
        core = {
            "domain": envelope.get("domain"),
            "version": envelope.get("version"),
            "body": envelope.get("body"),
        }
        if envelope.get("checksum") != hash_json(core):
            raise EngineError(
                "E_SNAPSHOT_CHECKSUM",
                f"snapshot checksum mismatch for {domain}",
                details={"path": str(path)},
            )
        if envelope["domain"] != domain:
            raise EngineError(
                "E_SNAPSHOT_CHECKSUM",
                f"snapshot domain mismatch for {domain}",
                details={"path": str(path), "actual_domain": envelope["domain"]},
            )
        if (
            int(envelope["version"]) != int(domain_meta["version"])
            or envelope["checksum"] != domain_meta["checksum"]
        ):
            raise EngineError(
                "E_SNAPSHOT_CHECKSUM",
                f"snapshot metadata mismatch for {domain}",
                details={"path": str(path)},
            )
        return envelope

    def load_all(self) -> dict[str, dict[str, Any]]:
        pointer = self._read_current_pointer()
        if pointer is None:
            return {}
        return {domain: self.load_domain(domain) for domain in DOMAINS}

    @contextmanager
    def acquire_commit_lock(self) -> Iterator[None]:
        handle = self.lock_path.open("a+b")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise EngineError(
                    "E_STAGE_BUSY",
                    "another snapshot commit is in progress",
                    details={"lock": str(self.lock_path)},
                ) from exc
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def hold_commit_lock(self) -> Any:
        """Testing/CLI helper that returns an open locked file handle."""

        handle = self.lock_path.open("a+b")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle

    def commit_bundle(
        self,
        bodies: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        missing = set(DOMAINS) - set(bodies)
        extra = set(bodies) - set(DOMAINS)
        if missing or extra:
            raise EngineError(
                "E_SNAPSHOT_INCOMPLETE",
                "snapshot bundle must contain manifest, budget, ledger and cache",
                details={"missing": sorted(missing), "extra": sorted(extra)},
            )
        with self.acquire_commit_lock():
            pointer = self._read_current_pointer()
            previous_versions = (
                {
                    domain: int(meta["version"])
                    for domain, meta in pointer["domains"].items()
                }
                if pointer
                else {domain: 0 for domain in DOMAINS}
            )
            bundle_version = int(pointer["bundle_version"]) + 1 if pointer else 1
            generation_name = f"bundle-{bundle_version:08d}"
            generation_path = self.generations / generation_name
            if generation_path.exists():
                shutil.rmtree(generation_path)
            temp_generation = Path(
                tempfile.mkdtemp(prefix=".bundle-", dir=self.generations)
            )
            envelopes: dict[str, dict[str, Any]] = {}
            try:
                for domain in DOMAINS:
                    version = previous_versions.get(domain, 0) + 1
                    envelope = self._envelope(domain, version, bodies[domain])
                    envelopes[domain] = envelope
                    data = canonical_json_bytes(envelope)
                    path = temp_generation / f"{domain}.json"
                    with path.open("wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                if generation_path.exists():
                    shutil.rmtree(generation_path)
                os.replace(temp_generation, generation_path)
            finally:
                if temp_generation.exists():
                    shutil.rmtree(temp_generation, ignore_errors=True)

            pointer_core = {
                "bundle_version": bundle_version,
                "generation": f"generations/{generation_name}",
                "domains": {
                    domain: {
                        "version": envelopes[domain]["version"],
                        "checksum": envelopes[domain]["checksum"],
                    }
                    for domain in DOMAINS
                },
            }
            pointer = {**pointer_core, "checksum": hash_json(pointer_core)}
            fd, temp_name = tempfile.mkstemp(prefix=".current-", dir=self.root)
            os.close(fd)
            temp_path = Path(temp_name)
            try:
                with temp_path.open("wb") as handle:
                    handle.write(canonical_json_bytes(pointer))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self.current_path)
            finally:
                if temp_path.exists():
                    temp_path.unlink()
            return {**pointer, "domains": envelopes}

    def verify(self) -> dict[str, Any]:
        pointer = self._read_current_pointer()
        if pointer is None:
            return {"ok": False, "code": "E_SNAPSHOT_MISSING"}
        try:
            loaded = self.load_all()
        except EngineError as exc:
            return {"ok": False, **exc.to_dict()}
        versions = {
            domain: int(envelope["version"]) for domain, envelope in loaded.items()
        }
        return {
            "ok": True,
            "bundle_version": pointer["bundle_version"],
            "generation": pointer["generation"],
            "versions": versions,
            "checksums": {
                domain: envelope["checksum"] for domain, envelope in loaded.items()
            },
        }

