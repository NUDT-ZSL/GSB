"""Monotonic config snapshots, whole replacement, and stale-result rejection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import atomic_write_json, read_json, sealed, verify_sealed
from .config import ConfigSnapshot
from .errors import (
    E_CHECKSUM_MISMATCH,
    E_STALE_RESULT,
    E_VERSION_REGRESSION,
    BusinessError,
)


class SnapshotStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.current_path = self.root / "current.json"

    def publish(self, snapshot: ConfigSnapshot) -> dict[str, Any]:
        current = self.current()
        if current is not None:
            current_version = int(current["version"])
            current_checksum = str(current["checksum"])
            if snapshot.version < current_version:
                raise BusinessError(
                    E_VERSION_REGRESSION,
                    "refusing to replace a newer config snapshot",
                    details={
                        "incoming_version": snapshot.version,
                        "current_version": current_version,
                    },
                )
            if snapshot.version == current_version:
                if snapshot.checksum != current_checksum:
                    raise BusinessError(
                        E_CHECKSUM_MISMATCH,
                        "same config version has a different checksum",
                        details={
                            "version": snapshot.version,
                            "incoming_checksum": snapshot.checksum,
                            "current_checksum": current_checksum,
                        },
                    )
                return current

        envelope = sealed(
            {
                "snapshot_schema": "config-snapshot/1.0",
                "version": snapshot.version,
                "checksum": snapshot.checksum,
                "config": snapshot.payload,
            }
        )
        snapshot_path = self.root / f"config-v{snapshot.version}.json"
        atomic_write_json(snapshot_path, envelope, canonical=True)
        pointer = {
            "snapshot_schema": "config-snapshot-pointer/1.0",
            "version": snapshot.version,
            "checksum": snapshot.checksum,
            "snapshot_file": snapshot_path.name,
        }
        atomic_write_json(self.current_path, pointer, canonical=True)
        return envelope

    def current(self) -> dict[str, Any] | None:
        if not self.current_path.exists():
            return None
        pointer = read_json(self.current_path)
        snapshot_path = self.root / str(pointer["snapshot_file"])
        envelope = verify_sealed(
            read_json(snapshot_path), context=f"config snapshot v{pointer['version']}"
        )
        if envelope["version"] != pointer["version"]:
            raise BusinessError(
                E_CHECKSUM_MISMATCH,
                "snapshot pointer version does not match snapshot body",
            )
        return envelope

    def assert_fresh(
        self,
        manifest: dict[str, Any],
        snapshot: ConfigSnapshot,
    ) -> None:
        config_block = manifest.get("config", {})
        mismatches: list[str] = []
        if config_block.get("version") != snapshot.version:
            mismatches.append("version")
        if config_block.get("checksum") != snapshot.checksum:
            mismatches.append("checksum")
        expected_versions = {
            stage: snapshot.stage_version(stage)
            for stage in ("quality", "neardup", "consistency", "sampling", "manifest")
        }
        actual_versions = manifest.get("stage_config_versions", {})
        for stage, expected in expected_versions.items():
            if actual_versions.get(stage) != expected:
                mismatches.append(f"stage_version:{stage}")
        if mismatches:
            raise BusinessError(
                E_STALE_RESULT,
                "manifest was produced by a different config snapshot or dependency version",
                details={"mismatches": sorted(mismatches)},
            )


def publish_run_manifest(target: str | Path, manifest: dict[str, Any]) -> None:
    """Whole-file replacement with a monotonic anti-rollback guard."""
    path = Path(target)
    if path.exists():
        existing = verify_sealed(read_json(path), context="existing run manifest")
        incoming_version = int(manifest["config"]["version"])
        existing_version = int(existing["config"]["version"])
        if existing_version > incoming_version:
            raise BusinessError(
                E_VERSION_REGRESSION,
                "old run manifest cannot overwrite a newer one",
                details={
                    "incoming_version": incoming_version,
                    "existing_version": existing_version,
                },
            )
        if (
            existing_version == incoming_version
            and existing["config"]["checksum"] != manifest["config"]["checksum"]
        ):
            raise BusinessError(
                E_CHECKSUM_MISMATCH,
                "same manifest version has a different config checksum",
            )
    atomic_write_json(path, manifest, canonical=True)


def check_run_manifest_guard(target: str | Path, snapshot: ConfigSnapshot) -> None:
    path = Path(target)
    if not path.exists():
        return
    existing = verify_sealed(read_json(path), context="existing run manifest")
    existing_version = int(existing["config"]["version"])
    if existing_version > snapshot.version:
        raise BusinessError(
            E_VERSION_REGRESSION,
            "old run manifest cannot overwrite a newer one",
            details={
                "incoming_version": snapshot.version,
                "existing_version": existing_version,
            },
        )
    if (
        existing_version == snapshot.version
        and existing["config"]["checksum"] != snapshot.checksum
    ):
        raise BusinessError(
            E_CHECKSUM_MISMATCH,
            "same manifest version has a different config checksum",
        )
