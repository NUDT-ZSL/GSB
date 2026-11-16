"""Atomic training snapshots and restart-point discovery."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .atomic import atomic_write_json, canonical_json, read_json
from .errors import E_CONFIG_MISMATCH, E_SNAPSHOT_INVALID, Failure

SNAPSHOT_SCHEMA_VERSION = 1


def _checksum(payload: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "checksum"}
    return hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()


class SnapshotStore:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.snapshots_dir = self.state_dir / "snapshots"
        self.pointer_path = self.state_dir / "latest.json"
        self.progress_path = self.state_dir / "progress.json"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

    def _scheme_dir(self, config_hash: str) -> Path:
        directory = self.snapshots_dir / config_hash
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _versions(self, config_hash: str) -> list[int]:
        directory = self.snapshots_dir / config_hash
        if not directory.exists():
            return []
        versions: list[int] = []
        for path in directory.glob("*.json"):
            try:
                versions.append(int(path.stem))
            except ValueError:
                continue
        return sorted(versions)

    def next_version(self, config_hash: str) -> int:
        versions = self._versions(config_hash)
        return (versions[-1] + 1) if versions else 1

    def write_snapshot(
        self,
        *,
        config_schema_version: int,
        config_hash: str,
        cache_key: str,
        training_fingerprint: str,
        scheme_name: str,
        scheme_type: str,
        round_number: int,
        status: str,
        weights: np.ndarray,
        intercept: float,
        feature_names: Iterable[str],
        loss_trajectory: list[dict[str, Any]],
        optimizer_state: dict[str, Any],
        data_fingerprint: str,
        commit_pointer: bool = True,
    ) -> dict[str, Any]:
        version = self.next_version(config_hash)
        payload: dict[str, Any] = {
            "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
            "config_schema_version": config_schema_version,
            "snapshot_version": version,
            "config_hash": config_hash,
            "cache_key": cache_key,
            "training_fingerprint": training_fingerprint,
            "scheme_name": scheme_name,
            "scheme_type": scheme_type,
            "round": round_number,
            "status": status,
            "feature_names": list(feature_names),
            "coefficients": {
                "intercept": float(intercept),
                "weights": [float(value) for value in np.asarray(weights, dtype=np.float64)],
            },
            "loss_trajectory": loss_trajectory,
            "optimizer_state": optimizer_state,
            "data_fingerprint": data_fingerprint,
        }
        payload["checksum"] = _checksum(payload)
        directory = self._scheme_dir(config_hash)
        snapshot_path = directory / f"{version:06d}.json"
        atomic_write_json(snapshot_path, payload, indent=None)

        if commit_pointer:
            self.commit_pointer(payload)
        return payload

    def commit_pointer(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        config_hash = str(snapshot["config_hash"])
        version = int(snapshot["snapshot_version"])
        snapshot_path = self.snapshots_dir / config_hash / f"{version:06d}.json"
        relative_path = str(snapshot_path.relative_to(self.state_dir))
        pointer = {
            "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
            "snapshot_version": version,
            "config_hash": config_hash,
            "cache_key": snapshot.get("cache_key"),
            "scheme": snapshot.get("scheme_name"),
            "round": snapshot.get("round"),
            "status": snapshot.get("status"),
            "snapshot_file": relative_path,
            "checksum": snapshot.get("checksum"),
        }
        atomic_write_json(self.pointer_path, pointer, indent=2)
        self.write_progress(status=str(snapshot.get("status", "unknown")), pointer=pointer)
        return pointer

    def write_progress(self, *, status: str, pointer: dict[str, Any]) -> None:
        progress = {
            "status": status,
            "config_hash": pointer.get("config_hash"),
            "cache_key": pointer.get("cache_key"),
            "scheme": pointer.get("scheme"),
            "round": pointer.get("round"),
            "snapshot_version": pointer.get("snapshot_version"),
            "snapshot_file": pointer.get("snapshot_file"),
        }
        atomic_write_json(self.progress_path, progress, indent=2)

    def read_pointer(self) -> tuple[dict[str, Any] | None, list[Failure]]:
        failures: list[Failure] = []
        if not self.pointer_path.exists():
            return None, failures
        try:
            pointer = read_json(self.pointer_path)
            if not isinstance(pointer, dict):
                raise ValueError("latest snapshot pointer is not an object")
            return pointer, failures
        except Exception as exc:
            failures.append(
                Failure(
                    business_code=E_SNAPSHOT_INVALID,
                    phase="snapshot_restore",
                    stage="snapshot_pointer_read",
                    message=f"latest snapshot pointer is invalid: {exc}",
                    exception_type=type(exc).__name__,
                )
            )
            return None, failures

    def read_progress(self) -> dict[str, Any] | None:
        try:
            if not self.progress_path.exists():
                return None
            value = read_json(self.progress_path)
            return value if isinstance(value, dict) else None
        except Exception:
            return None

    def audit_pointer(self, current_config_hashes: Iterable[str]) -> list[Failure]:
        """Reject restoration when the global latest pointer is an unknown version.

        Pointers belonging to another currently configured scheme are normal in
        multi-scheme runs and are not reported as mismatches.
        """

        pointer, failures = self.read_pointer()
        if pointer is None:
            return failures
        known = set(current_config_hashes)
        pointer_hash = pointer.get("config_hash")
        if pointer_hash and pointer_hash not in known:
            failures.append(
                Failure(
                    business_code=E_CONFIG_MISMATCH,
                    phase="snapshot_restore",
                    stage="config_hash_check",
                    message=(
                        "latest persisted snapshot belongs to a different regularization "
                        "configuration; restoration was refused"
                    ),
                    exception_type="ConfigMismatchError",
                    scheme=pointer.get("scheme"),
                    details={
                        "snapshot_config_hash": pointer_hash,
                        "snapshot_version": pointer.get("snapshot_version"),
                        "current_config_hashes": sorted(known),
                    },
                )
            )
        return failures

    def _validate_snapshot(
        self,
        payload: Any,
        *,
        expected_config_hash: str,
        expected_config_schema_version: int,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("snapshot is not an object")
        stored_checksum = payload.get("checksum")
        if not isinstance(stored_checksum, str) or stored_checksum != _checksum(payload):
            raise ValueError("snapshot checksum mismatch")
        if payload.get("snapshot_schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("unsupported snapshot schema version")
        if payload.get("config_schema_version") != expected_config_schema_version:
            raise ValueError("snapshot config schema version mismatch")
        if payload.get("config_hash") != expected_config_hash:
            raise ValueError("snapshot config hash mismatch")
        coefficients = payload.get("coefficients")
        if not isinstance(coefficients, dict):
            raise ValueError("snapshot coefficients are missing")
        weights = coefficients.get("weights")
        feature_names = payload.get("feature_names")
        if not isinstance(weights, list) or not isinstance(feature_names, list) or len(weights) != len(feature_names):
            raise ValueError("snapshot coefficient shape mismatch")
        if not isinstance(payload.get("loss_trajectory"), list):
            raise ValueError("snapshot loss trajectory is missing")
        round_number = payload.get("round")
        if not isinstance(round_number, int) or round_number < 0:
            raise ValueError("snapshot round is invalid")
        return payload

    def load_latest(
        self,
        *,
        config_hash: str,
        config_schema_version: int,
        known_config_hashes: Iterable[str],
    ) -> tuple[dict[str, Any] | None, list[Failure]]:
        """Load the newest complete snapshot for one configuration.

        The newest file is selected by version, not by pointer alone.  This
        recovers the crash window where a snapshot was atomically replaced but
        the pointer had not yet been updated.
        """

        failures: list[Failure] = []
        pointer, pointer_failures = self.read_pointer()
        failures.extend(pointer_failures)
        known = set(known_config_hashes)
        versions = self._versions(config_hash)

        if not versions:
            if pointer is not None:
                pointer_hash = pointer.get("config_hash")
                if pointer_hash and pointer_hash != config_hash and pointer_hash not in known:
                    failures.append(
                        Failure(
                            business_code=E_CONFIG_MISMATCH,
                            phase="snapshot_restore",
                            stage="config_hash_check",
                            message="snapshot configuration hash does not match the current scheme; restore refused",
                            exception_type="ConfigMismatchError",
                            scheme=pointer.get("scheme"),
                            details={
                                "snapshot_config_hash": pointer_hash,
                                "current_config_hash": config_hash,
                                "snapshot_version": pointer.get("snapshot_version"),
                            },
                        )
                    )
            return None, failures

        directory = self.snapshots_dir / config_hash
        for version in reversed(versions):
            path = directory / f"{version:06d}.json"
            try:
                payload = read_json(path)
                return (
                    self._validate_snapshot(
                        payload,
                        expected_config_hash=config_hash,
                        expected_config_schema_version=config_schema_version,
                    ),
                    failures,
                )
            except Exception as exc:
                failures.append(
                    Failure(
                        business_code=E_SNAPSHOT_INVALID,
                        phase="snapshot_restore",
                        stage="snapshot_validate",
                        message=f"snapshot {path.name} is invalid and was skipped: {exc}",
                        exception_type=type(exc).__name__,
                        details={"snapshot_file": str(path), "snapshot_version": version},
                    )
                )
        return None, failures
