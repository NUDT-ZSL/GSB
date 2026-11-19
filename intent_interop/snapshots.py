"""Immutable snapshot repository with monotonic version guards."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config_io import read_config_document
from .errors import IntentError
from .models import ConfigDocument, IntentSpec

_SNAPSHOT_VERSION_RE = re.compile(
    r"(?:\"snapshot_version\"\s*:\s*|snapshot_version\s*[:=]\s*)(\d+)"
)


def _semantic_spec(spec: IntentSpec) -> IntentSpec:
    return spec.with_changes(line_no=0)


def _snapshot_diff(
    previous: ConfigDocument | None,
    current: ConfigDocument,
) -> dict[str, Any]:
    if previous is None:
        return {
            "added_intents": list(current.intent_labels),
            "removed_intents": [],
            "changed_intents": [],
            "added_labels": sorted(current.controlled_labels),
            "removed_labels": [],
        }
    old_map = {spec.intent: _semantic_spec(spec) for spec in previous.intents}
    new_map = {spec.intent: _semantic_spec(spec) for spec in current.intents}
    old_labels = set(old_map)
    new_labels = set(new_map)
    changed = sorted(
        label
        for label in old_labels & new_labels
        if old_map[label] != new_map[label]
    )
    return {
        "added_intents": sorted(new_labels - old_labels),
        "removed_intents": sorted(old_labels - new_labels),
        "changed_intents": changed,
        "added_labels": sorted(current.controlled_labels - previous.controlled_labels),
        "removed_labels": sorted(previous.controlled_labels - current.controlled_labels),
    }


@dataclass(frozen=True)
class Snapshot:
    document: ConfigDocument
    generation: int

    @property
    def version(self) -> int:
        return self.document.snapshot_version

    @property
    def checksum(self) -> str | None:
        return self.document.checksum

    def summary(self) -> dict[str, Any]:
        summary = self.document.snapshot_summary()
        summary["generation"] = self.generation
        return summary


@dataclass(frozen=True)
class SnapshotLoadResult:
    snapshot: Snapshot
    replaced: bool
    reason: str
    previous_version: int | None
    previous_checksum: str | None
    diff: dict[str, Any]

    def to_report(self) -> dict[str, Any]:
        return {
            "replaced": self.replaced,
            "replacement_mode": "whole_snapshot",
            "reason": self.reason,
            "previous_version": self.previous_version,
            "previous_checksum": self.previous_checksum,
            "current": self.snapshot.summary(),
            "diff": self.diff,
        }


class SnapshotStore:
    """Stores one immutable snapshot per namespace."""

    def __init__(self) -> None:
        self._snapshots: dict[str, Snapshot] = {}

    def current(self, namespace: str = "default") -> Snapshot | None:
        return self._snapshots.get(namespace)

    def install_document(
        self,
        document: ConfigDocument,
        *,
        namespace: str = "default",
    ) -> SnapshotLoadResult:
        current = self._snapshots.get(namespace)
        if current is None:
            snapshot = Snapshot(document=document, generation=1)
            self._snapshots[namespace] = snapshot
            return SnapshotLoadResult(
                snapshot=snapshot,
                replaced=True,
                reason="initial",
                previous_version=None,
                previous_checksum=None,
                diff=_snapshot_diff(None, document),
            )

        if document.snapshot_version < current.version:
            raise IntentError(
                "E_SNAPSHOT_ROLLBACK",
                "拒绝用旧版本快照覆盖新版本",
                stage="snapshot",
                version=document.snapshot_version,
                current_version=current.version,
                attempted_version=document.snapshot_version,
                current_checksum=current.checksum,
                attempted_checksum=document.checksum,
                namespace=namespace,
            )

        if (
            document.snapshot_version == current.version
            and document.checksum == current.checksum
        ):
            return SnapshotLoadResult(
                snapshot=current,
                replaced=False,
                reason="unchanged",
                previous_version=current.version,
                previous_checksum=current.checksum,
                diff=_snapshot_diff(current.document, document),
            )

        if document.snapshot_version == current.version:
            reason = "checksum_changed_same_version"
        else:
            reason = "version_changed"
        diff = _snapshot_diff(current.document, document)
        snapshot = Snapshot(document=document, generation=current.generation + 1)
        self._snapshots[namespace] = snapshot
        return SnapshotLoadResult(
            snapshot=snapshot,
            replaced=True,
            reason=reason,
            previous_version=current.version,
            previous_checksum=current.checksum,
            diff=diff,
        )

    def load_file(
        self,
        path: str | Path,
        *,
        namespace: str = "default",
        verify_checksum: bool = True,
    ) -> SnapshotLoadResult:
        try:
            document = read_config_document(path, verify_checksum=verify_checksum)
        except IntentError as exc:
            checksum_damage = (
                exc.code == "E_CONFIG_INVALID"
                and exc.details.get("field") == "checksum"
            )
            if exc.code != "E_FORMAT_CORRUPT" and not checksum_damage:
                raise
            raise self._snapshot_corrupt_from(path, exc) from exc
        return self.install_document(document, namespace=namespace)

    @staticmethod
    def _snapshot_corrupt_from(path: str | Path, exc: IntentError) -> IntentError:
        version: int | None = None
        field_offset = 0
        try:
            raw = Path(path).read_bytes()
        except OSError:
            raw = b""
        text = raw.decode("utf-8", errors="replace")
        match = _SNAPSHOT_VERSION_RE.search(text)
        if match:
            version = int(match.group(1))
            field_offset = match.start(1)
        details = exc.details
        if isinstance(details.get("field_offset"), int):
            field_offset = details["field_offset"]
        return IntentError(
            "E_SNAPSHOT_CORRUPT",
            "快照内容损坏",
            stage="snapshot",
            version=version,
            field_offset=field_offset,
            cause_code=exc.code,
            cause=exc.to_record(),
        )

    def load_inline(
        self,
        text: str,
        *,
        namespace: str = "default",
        source_path: str | None = None,
    ) -> SnapshotLoadResult:
        from .config_io import parse_config_text

        document = parse_config_text(text, source_path=source_path)
        return self.install_document(document, namespace=namespace)
