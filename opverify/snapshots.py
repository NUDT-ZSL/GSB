from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .errors import OpVerifyError
from .hashing import atomic_write_json, read_json, sha256_json


SNAPSHOT_TYPES = ("ledger", "outputs", "parameters")


def make_envelope(
    snapshot_type: str,
    version: int,
    entries: Any,
    *,
    parent_version: int | None = None,
) -> dict[str, Any]:
    if snapshot_type not in SNAPSHOT_TYPES:
        raise ValueError(f"unknown snapshot type: {snapshot_type}")
    payload: dict[str, Any] = {
        "snapshot_type": snapshot_type,
        "version": int(version),
        "parent_version": parent_version,
        "entries": entries,
    }
    return {**payload, "checksum": sha256_json(payload)}


def verify_envelope(envelope: dict[str, Any]) -> bool:
    expected = envelope.get("checksum")
    payload = {key: value for key, value in envelope.items() if key != "checksum"}
    return isinstance(expected, str) and expected == sha256_json(payload)


class SnapshotStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root) / "snapshots"
        self.root.mkdir(parents=True, exist_ok=True)

    def versioned_path(self, snapshot_type: str, version: int) -> Path:
        return self.root / f"{snapshot_type}-v{int(version):08d}.json"

    def current_path(self, snapshot_type: str) -> Path:
        return self.root / f"{snapshot_type}-current.json"

    def write_versioned(self, envelope: dict[str, Any]) -> Path:
        if not verify_envelope(envelope):
            raise OpVerifyError("E_INCONSISTENT", "refusing to write snapshot with invalid checksum")
        path = self.versioned_path(envelope["snapshot_type"], int(envelope["version"]))
        atomic_write_json(path, envelope)
        return path

    def load_current(self, snapshot_type: str) -> dict[str, Any] | None:
        path = self.current_path(snapshot_type)
        if not path.exists():
            return None
        envelope = read_json(path)
        if not verify_envelope(envelope):
            raise OpVerifyError(
                "E_INCONSISTENT",
                "current snapshot checksum mismatch",
                field=f"$.snapshots.{snapshot_type}",
                details={"path": str(path)},
            )
        return envelope

    def load_version(self, snapshot_type: str, version: int) -> dict[str, Any]:
        path = self.versioned_path(snapshot_type, version)
        if not path.exists():
            raise OpVerifyError(
                "E_ARTIFACT_MISSING", f"snapshot version {version} is missing", details={"path": str(path)}
            )
        envelope = read_json(path)
        if not verify_envelope(envelope):
            raise OpVerifyError("E_INCONSISTENT", "versioned snapshot checksum mismatch", details={"path": str(path)})
        return envelope

    def replace_current(self, envelope: dict[str, Any]) -> Path:
        snapshot_type = envelope["snapshot_type"]
        new_version = int(envelope["version"])
        current = self.load_current(snapshot_type)
        if current is not None:
            current_version = int(current["version"])
            if new_version < current_version:
                raise OpVerifyError(
                    "E_SNAPSHOT_ROLLBACK",
                    "refusing to replace a newer snapshot with an older version",
                    field=f"$.snapshots.{snapshot_type}.version",
                    details={"submitted_version": new_version, "current_version": current_version},
                )
            if new_version == current_version and envelope["checksum"] != current["checksum"]:
                raise OpVerifyError(
                    "E_SNAPSHOT_ROLLBACK",
                    "version exists with different checksum; whole-version replacement required",
                    field=f"$.snapshots.{snapshot_type}.checksum",
                    details={
                        "version": new_version,
                        "current_checksum": current["checksum"],
                        "submitted_checksum": envelope["checksum"],
                    },
                )
        path = self.current_path(snapshot_type)
        atomic_write_json(path, envelope)
        return path

    def commit_bundle(self, envelopes: Iterable[dict[str, Any]]) -> dict[str, str]:
        envelope_list = list(envelopes)
        for envelope in envelope_list:
            self.write_versioned(envelope)
        paths: dict[str, str] = {}
        for envelope in envelope_list:
            path = self.replace_current(envelope)
            paths[envelope["snapshot_type"]] = str(path)
        versions = {int(envelope["version"]) for envelope in envelope_list}
        checksums = {envelope["snapshot_type"]: envelope["checksum"] for envelope in envelope_list}
        if len(versions) != 1:
            raise OpVerifyError("E_INCONSISTENT", "snapshot bundle versions do not match")
        manifest = {
            "version": versions.pop(),
            "checksums": checksums,
            "paths": paths,
        }
        atomic_write_json(self.root / "current-bundle.json", manifest)
        return paths

    def promote_version(self, version: int) -> dict[str, str]:
        envelopes = [self.load_version(snapshot_type, version) for snapshot_type in SNAPSHOT_TYPES]
        return self.commit_bundle(envelopes)
