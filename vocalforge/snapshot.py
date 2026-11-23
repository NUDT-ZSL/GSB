"""Whole-snapshot versioning with monotonic per-section checksums."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AppError
from .util import atomic_write_text, canonical_json, digest_payload

SECTION_NAMES = ("embedding_stats", "precision_config", "error_spectrum_stats")


@dataclass(frozen=True)
class Snapshot:
    version: int
    parent_version: int
    dataset_digest: str
    sections: dict[str, dict[str, Any]]
    checksum: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "parent_version": self.parent_version,
            "dataset_digest": self.dataset_digest,
            "sections": self.sections,
            "checksum": self.checksum,
        }


def _build_snapshot(
    version: int,
    parent_version: int,
    dataset_digest: str,
    sections: dict[str, dict[str, Any]],
) -> Snapshot:
    payload = {
        "version": version,
        "parent_version": parent_version,
        "dataset_digest": dataset_digest,
        "sections": sections,
    }
    return Snapshot(
        version=version,
        parent_version=parent_version,
        dataset_digest=dataset_digest,
        sections=sections,
        checksum=digest_payload(payload),
    )


class SnapshotStore:
    def __init__(self, root: Path, dataset_digest: str) -> None:
        self.root = root
        self.dataset_digest = dataset_digest
        self.current_path = root / "current_snapshot.json"
        self.archive = root / "snapshots"
        self.archive.mkdir(parents=True, exist_ok=True)
        self.current = self._load_or_none()

    def _load_or_none(self) -> Snapshot | None:
        if not self.current_path.exists():
            return None
        try:
            payload = json.loads(self.current_path.read_text(encoding="utf-8"))
            if payload.get("dataset_digest") != self.dataset_digest:
                return None
            sections = payload.get("sections", {})
            snapshot = _build_snapshot(
                int(payload["version"]),
                int(payload["parent_version"]),
                str(payload["dataset_digest"]),
                sections,
            )
            if snapshot.checksum != payload.get("checksum"):
                return None
            return snapshot
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def bootstrap(self, payloads: dict[str, Any]) -> Snapshot:
        if self.current is not None:
            return self.current
        sections = self._section_payloads(payloads, previous=None)
        snapshot = _build_snapshot(
            version=1,
            parent_version=0,
            dataset_digest=self.dataset_digest,
            sections=sections,
        )
        self._persist(snapshot)
        self.current = snapshot
        return snapshot

    def _section_payloads(
        self,
        payloads: dict[str, Any],
        previous: Snapshot | None,
    ) -> dict[str, dict[str, Any]]:
        sections: dict[str, dict[str, Any]] = {}
        for name in SECTION_NAMES:
            if name not in payloads:
                raise AppError(
                    "E_INPUT_INVALID",
                    f"Snapshot section {name!r} is missing.",
                    stage="snapshot.commit",
                    field=name,
                )
            payload = payloads[name]
            checksum = digest_payload(payload)
            old = previous.sections.get(name) if previous is not None else None
            if old is not None and old.get("checksum") == checksum:
                version = int(old["version"])
            else:
                version = int(old["version"]) + 1 if old is not None else 1
            sections[name] = {
                "version": version,
                "checksum": checksum,
                "payload": payload,
            }
        return sections

    def commit(
        self,
        payloads: dict[str, Any],
        expected_version: int | None = None,
    ) -> Snapshot:
        if self.current is None:
            return self.bootstrap(payloads)
        if expected_version is not None and expected_version != self.current.version:
            raise AppError(
                "E_SNAPSHOT_ROLLBACK",
                "Snapshot base version is stale; whole-snapshot replacement was rejected.",
                stage="snapshot.commit",
                field="expected_version",
                details={
                    "expected_version": expected_version,
                    "current_version": self.current.version,
                },
            )
        sections = self._section_payloads(payloads, previous=self.current)
        snapshot = _build_snapshot(
            version=self.current.version + 1,
            parent_version=self.current.version,
            dataset_digest=self.dataset_digest,
            sections=sections,
        )
        if snapshot.version <= self.current.version:
            raise AppError(
                "E_SNAPSHOT_ROLLBACK",
                "A snapshot version must move forward.",
                stage="snapshot.commit",
                field="version",
                details={
                    "current_version": self.current.version,
                    "new_version": snapshot.version,
                },
            )
        self._persist(snapshot)
        self.current = snapshot
        return snapshot

    def rollback(self, target_version: int) -> None:
        current = self.current.version if self.current is not None else 0
        raise AppError(
            "E_SNAPSHOT_ROLLBACK",
            "Rollback is not permitted; snapshots are replaced atomically in forward order.",
            stage="snapshot.rollback",
            field="target_version",
            details={
                "target_version": target_version,
                "current_version": current,
                "policy": "monotonic_forward_only",
            },
        )

    def _persist(self, snapshot: Snapshot) -> None:
        text = json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        atomic_write_text(self.current_path, text + "\n")
        atomic_write_text(
            self.archive / f"v{snapshot.version:06d}.json", text + "\n"
        )

    def verify(self) -> dict[str, Any]:
        if self.current is None:
            return {"ok": False, "reason": "no_current_snapshot"}
        for name in SECTION_NAMES:
            section = self.current.sections.get(name)
            if section is None:
                return {"ok": False, "reason": "missing_section", "section": name}
            if section["checksum"] != digest_payload(section["payload"]):
                return {"ok": False, "reason": "section_checksum", "section": name}
        expected = _build_snapshot(
            self.current.version,
            self.current.parent_version,
            self.current.dataset_digest,
            self.current.sections,
        )
        if expected.checksum != self.current.checksum:
            return {"ok": False, "reason": "snapshot_checksum"}
        return {
            "ok": True,
            "version": self.current.version,
            "checksum": self.current.checksum,
            "sections": {
                name: {
                    "version": self.current.sections[name]["version"],
                    "checksum": self.current.sections[name]["checksum"],
                }
                for name in SECTION_NAMES
            },
        }

    def summary(self) -> dict[str, Any]:
        if self.current is None:
            return {"version": 0, "sections": {}}
        return {
            "version": self.current.version,
            "parent_version": self.current.parent_version,
            "checksum": self.current.checksum,
            "sections": {
                name: {
                    "version": self.current.sections[name]["version"],
                    "checksum": self.current.sections[name]["checksum"],
                }
                for name in SECTION_NAMES
            },
            "archive_path": str(self.archive),
        }


def snapshot_signature(snapshot: Snapshot) -> str:
    return canonical_json(
        {
            "version": snapshot.version,
            "checksum": snapshot.checksum,
            "sections": {
                name: snapshot.sections[name]["checksum"] for name in SECTION_NAMES
            },
        }
    )
