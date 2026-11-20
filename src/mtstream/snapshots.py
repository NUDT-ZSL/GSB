"""Monotonic, whole-object snapshots for vocabularies and decode settings."""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, replace
from typing import Any, Callable

from .errors import (
    E_CACHE_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    fail,
)
from .models import DecodeConfig, TranslationCase
from .util import canonical_json, sha256_value

SNAPSHOT_KINDS = (
    "vocabulary",
    "alignment_table",
    "block_size",
    "decode_params",
    "source_sentence",
)


@dataclass(frozen=True)
class Snapshot:
    kind: str
    version: int
    checksum: str
    content: dict[str, Any]
    object_id: int

    def to_dict(self, include_content: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "version": self.version,
            "checksum": self.checksum,
            "object_id": self.object_id,
        }
        if include_content:
            payload["content"] = copy.deepcopy(self.content)
        return payload


def _content_checksum(content: dict[str, Any]) -> str:
    return sha256_value({"snapshot_v1": content})


class SnapshotStore:
    """An in-memory versioned snapshot registry.

    Replacing a snapshot rebinds the kind to a new immutable object; no part of
    an older object is patched in place.  A lower version is always rejected.
    """

    def __init__(self) -> None:
        self._records: dict[str, Snapshot] = {}
        self._lock = threading.RLock()
        self._next_object_id = 1

    @classmethod
    def from_case(
        cls, case: TranslationCase, config: DecodeConfig
    ) -> "SnapshotStore":
        store = cls()
        store.replace_snapshot(
            "vocabulary",
            {
                "source_vocab": list(case.source_vocab),
                "target_vocab": list(case.target_vocab),
                "pair_vocab": [
                    {"source": source, "target": target}
                    for source, target in case.pair_vocab
                ],
            },
            version=1,
        )
        store.replace_snapshot(
            "alignment_table",
            {
                "align_version": case.align_version,
                "alignments": [alignment.to_dict() for alignment in case.alignments],
            },
            version=1,
        )
        store.replace_snapshot(
            "block_size",
            {"block_size": config.block_size},
            version=1,
        )
        store.replace_snapshot(
            "decode_params",
            {
                key: value
                for key, value in config.to_dict().items()
                if key != "block_size"
            },
            version=1,
        )
        store.replace_snapshot(
            "source_sentence",
            {
                "case_id": case.case_id,
                "source_version": case.source_version,
                "source_tokens": list(case.source_tokens),
                "source_digest": case.source_digest,
            },
            version=1,
        )
        return store

    def _verify(self, snapshot: Snapshot) -> Snapshot:
        expected = _content_checksum(snapshot.content)
        if expected != snapshot.checksum:
            fail(
                E_CACHE_CORRUPT,
                "snapshot checksum verification failed",
                field=f"snapshot:{snapshot.kind}",
                data={
                    "entry_key": f"snapshot:{snapshot.kind}",
                    "version": snapshot.version,
                    "expected_checksum": snapshot.checksum,
                    "actual_checksum": expected,
                    "category": "checksum",
                },
            )
        return snapshot

    def replace_snapshot(
        self,
        kind: str,
        content: dict[str, Any],
        *,
        version: int | None = None,
    ) -> Snapshot:
        if kind not in SNAPSHOT_KINDS:
            fail(
                E_SNAPSHOT_ROLLBACK,
                "unknown snapshot kind",
                field=kind,
                data={"kind": kind},
            )
        with self._lock:
            current = self._records.get(kind)
            candidate_version = (
                current.version + 1 if current is not None and version is None else version
            )
            if candidate_version is None:
                candidate_version = 1
            candidate_version = int(candidate_version)
            checksum = _content_checksum(content)
            if current is not None:
                if candidate_version < current.version:
                    fail(
                        E_SNAPSHOT_ROLLBACK,
                        "snapshot version rollback is forbidden",
                        field=kind,
                        data={
                            "kind": kind,
                            "current_version": current.version,
                            "attempted_version": candidate_version,
                        },
                    )
                if (
                    candidate_version == current.version
                    and checksum != current.checksum
                ):
                    fail(
                        E_SNAPSHOT_ROLLBACK,
                        "same snapshot version cannot change content",
                        field=kind,
                        data={
                            "kind": kind,
                            "version": candidate_version,
                            "current_checksum": current.checksum,
                            "attempted_checksum": checksum,
                        },
                    )
                if (
                    candidate_version == current.version
                    and checksum == current.checksum
                ):
                    return current
            snapshot = Snapshot(
                kind=kind,
                version=candidate_version,
                checksum=checksum,
                content=copy.deepcopy(content),
                object_id=self._next_object_id,
            )
            self._next_object_id += 1
            # Whole-object replacement, never mutation of the prior record.
            self._records[kind] = snapshot
            return snapshot

    def update_from_case(
        self, case: TranslationCase, config: DecodeConfig
    ) -> dict[str, Snapshot]:
        changed: dict[str, Snapshot] = {}
        vocab_content = {
            "source_vocab": list(case.source_vocab),
            "target_vocab": list(case.target_vocab),
            "pair_vocab": [
                {"source": source, "target": target}
                for source, target in case.pair_vocab
            ],
        }
        alignment_content = {
            "align_version": case.align_version,
            "alignments": [alignment.to_dict() for alignment in case.alignments],
        }
        block_content = {"block_size": config.block_size}
        decode_content = {
            key: value
            for key, value in config.to_dict().items()
            if key != "block_size"
        }
        source_content = {
            "case_id": case.case_id,
            "source_version": case.source_version,
            "source_tokens": list(case.source_tokens),
            "source_digest": case.source_digest,
        }
        for kind, content in (
            ("vocabulary", vocab_content),
            ("alignment_table", alignment_content),
            ("block_size", block_content),
            ("decode_params", decode_content),
            ("source_sentence", source_content),
        ):
            current = self.get(kind)
            if current.checksum != _content_checksum(content):
                changed[kind] = self.replace_snapshot(kind, content)
        return changed

    def get(self, kind: str) -> Snapshot:
        with self._lock:
            snapshot = self._records.get(kind)
            if snapshot is None:
                fail(
                    E_CACHE_CORRUPT,
                    "snapshot is missing",
                    field=f"snapshot:{kind}",
                    data={
                        "entry_key": f"snapshot:{kind}",
                        "version": 0,
                        "category": "missing",
                    },
                )
            return self._verify(snapshot)

    def versions(self) -> dict[str, int]:
        with self._lock:
            return {
                kind: snapshot.version
                for kind, snapshot in sorted(self._records.items())
            }

    def checksums(self) -> dict[str, str]:
        with self._lock:
            return {
                kind: snapshot.checksum
                for kind, snapshot in sorted(self._records.items())
            }

    def object_ids(self) -> dict[str, int]:
        with self._lock:
            return {
                kind: snapshot.object_id
                for kind, snapshot in sorted(self._records.items())
            }

    def bundle_fingerprint(self) -> str:
        with self._lock:
            return sha256_value(
                {
                    kind: {
                        "version": snapshot.version,
                        "checksum": snapshot.checksum,
                    }
                    for kind, snapshot in sorted(self._records.items())
                }
            )

    def tamper_snapshot(
        self,
        kind: str,
        mutator: Callable[[dict[str, Any]], None],
    ) -> None:
        """Diagnostics only: leave the checksum stale to prove detection."""

        with self._lock:
            snapshot = self._records.get(kind)
            if snapshot is None:
                fail(
                    E_CACHE_CORRUPT,
                    "snapshot is missing",
                    field=f"snapshot:{kind}",
                    data={"entry_key": f"snapshot:{kind}", "version": 0},
                )
            content = copy.deepcopy(snapshot.content)
            mutator(content)
            self._records[kind] = replace(snapshot, content=content)

    def to_dict(self, include_content: bool = False) -> dict[str, Any]:
        with self._lock:
            return {
                "bundle_fingerprint": self.bundle_fingerprint(),
                "snapshots": {
                    kind: snapshot.to_dict(include_content=include_content)
                    for kind, snapshot in sorted(self._records.items())
                },
            }

