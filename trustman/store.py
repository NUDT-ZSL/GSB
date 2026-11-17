"""Snapshot candidate persistence and deterministic conflict resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .atomic import atomic_write_json, read_json_file
from .errors import E_SCHEMA
from .models import (
    SNAPSHOT_STORE_SCHEMA,
    Rejection,
    RevocationEntry,
    Snapshot,
    is_strict_int,
    snapshot_content_fingerprint,
)


SNAPSHOT_STORE_PATH = Path("state") / "snapshots.json"


def _snapshot_store_path(workspace: str | Path) -> Path:
    return Path(workspace) / SNAPSHOT_STORE_PATH


def snapshot_to_store_dict(snapshot: Snapshot) -> dict:
    return {
        "version": snapshot.version,
        "policy_version": snapshot.policy_version,
        "trusted_roots": list(snapshot.trusted_roots),
        "revoked_entries": [entry.as_dict() for entry in snapshot.revoked_entries],
        "observed_at_ms": snapshot.observed_at_ms,
        "content_fingerprint": snapshot.content_fingerprint,
        "source_file": snapshot.source_file,
        "entry_index": snapshot.entry_index,
    }


def _snapshot_from_store_dict(value: object) -> Snapshot:
    if not isinstance(value, dict):
        raise ValueError("snapshot state entry must be an object")
    required = {
        "version",
        "policy_version",
        "trusted_roots",
        "revoked_entries",
        "observed_at_ms",
        "content_fingerprint",
        "source_file",
        "entry_index",
    }
    if set(value) != required:
        raise ValueError("snapshot state entry has invalid fields")
    version = value["version"]
    policy = value["policy_version"]
    roots = value["trusted_roots"]
    revocations = value["revoked_entries"]
    observed_at = value["observed_at_ms"]
    fingerprint = value["content_fingerprint"]
    source_file = value["source_file"]
    entry_index = value["entry_index"]
    if not is_strict_int(version) or version < 1:
        raise ValueError("invalid snapshot version")
    if not isinstance(policy, str):
        raise ValueError("invalid snapshot policy")
    if not isinstance(roots, list) or not all(
        isinstance(root, str) for root in roots
    ):
        raise ValueError("invalid trusted roots")
    if not is_strict_int(observed_at):
        raise ValueError("invalid observed_at_ms")
    if not isinstance(source_file, str) or not is_strict_int(entry_index):
        raise ValueError("invalid snapshot source")
    if not isinstance(revocations, list):
        raise ValueError("invalid revocations")
    parsed_revocations: list[RevocationEntry] = []
    for entry in revocations:
        if not isinstance(entry, dict) or set(entry) != {
            "cert_id",
            "revoked_at_ms",
            "reason",
        }:
            raise ValueError("invalid revocation entry")
        if (
            not isinstance(entry["cert_id"], str)
            or not is_strict_int(entry["revoked_at_ms"])
            or not isinstance(entry["reason"], str)
        ):
            raise ValueError("invalid revocation entry values")
        parsed_revocations.append(
            RevocationEntry(
                entry["cert_id"], entry["revoked_at_ms"], entry["reason"]
            )
        )
    expected = snapshot_content_fingerprint(
        version=version,
        policy_version=policy,
        trusted_roots=roots,
        revoked_entries=parsed_revocations,
        observed_at_ms=observed_at,
    )
    if fingerprint != expected:
        raise ValueError("snapshot content fingerprint mismatch")
    return Snapshot(
        version=version,
        policy_version=policy,
        trusted_roots=tuple(sorted(roots)),
        revoked_entries=tuple(
            sorted(parsed_revocations, key=lambda item: item.cert_id)
        ),
        observed_at_ms=observed_at,
        content_fingerprint=fingerprint,
        source_file=source_file,
        entry_index=entry_index,
    )


def load_snapshot_candidates(
    workspace: str | Path,
) -> tuple[list[Snapshot], list[Rejection]]:
    path = _snapshot_store_path(workspace)
    if not path.exists():
        return [], []
    try:
        document = read_json_file(path)
    except (OSError, UnicodeDecodeError, ValueError):
        return [], [_store_rejection("invalid_json", "snapshot state is not valid JSON")]
    if not isinstance(document, dict):
        return [], [_store_rejection("invalid_document", "snapshot state must be an object")]
    if document.get("schema") != SNAPSHOT_STORE_SCHEMA:
        return [], [
            _store_rejection(
                "invalid_document_schema",
                f"snapshot state schema must be {SNAPSHOT_STORE_SCHEMA}",
            )
        ]
    if set(document) != {"schema", "snapshots"} or not isinstance(
        document.get("snapshots"), list
    ):
        return [], [
            _store_rejection(
                "invalid_document",
                "snapshot state must contain only schema and snapshots",
            )
        ]
    candidates: list[Snapshot] = []
    rejections: list[Rejection] = []
    for index, value in enumerate(document["snapshots"]):
        try:
            candidates.append(_snapshot_from_store_dict(value))
        except ValueError as exc:
            rejections.append(
                Rejection(
                    scope="snapshot",
                    source_file=SNAPSHOT_STORE_PATH.name,
                    entry_index=index,
                    code=E_SCHEMA,
                    reason="invalid_snapshot_state",
                    message=str(exc),
                )
            )
    return candidates, rejections


def _store_rejection(reason: str, message: str) -> Rejection:
    return Rejection(
        scope="snapshot",
        source_file=SNAPSHOT_STORE_PATH.name,
        entry_index=-1,
        code=E_SCHEMA,
        reason=reason,
        message=message,
    )


def merge_snapshot_candidates(
    existing: Iterable[Snapshot], new: Iterable[Snapshot]
) -> list[Snapshot]:
    unique: dict[tuple[int, str], Snapshot] = {}
    for candidate in list(existing) + list(new):
        key = (candidate.version, candidate.content_fingerprint)
        current = unique.get(key)
        if current is None or (
            candidate.source_file,
            candidate.entry_index,
        ) < (current.source_file, current.entry_index):
            unique[key] = candidate
    return sorted(
        unique.values(),
        key=lambda item: (
            item.version,
            item.content_fingerprint,
            item.source_file,
            item.entry_index,
        ),
    )


def save_snapshot_candidates(
    workspace: str | Path, candidates: Iterable[Snapshot]
) -> None:
    payload = {
        "schema": SNAPSHOT_STORE_SCHEMA,
        "snapshots": [
            snapshot_to_store_dict(snapshot)
            for snapshot in merge_snapshot_candidates([], candidates)
        ],
    }
    atomic_write_json(_snapshot_store_path(workspace), payload, pretty=True)
