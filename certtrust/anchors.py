"""Trust-anchor snapshot management.

A snapshot declares the full trusted-root set, a policy version and
revocation entries. Snapshots replace each other wholesale. The effective
snapshot of a store is chosen by (snapshot_version, stable_key) only, so
the outcome never depends on arrival order or directory traversal order.
"""

from __future__ import annotations

import json
import re

from . import KNOWN_POLICY_VERSIONS, STRUCTURE_VERSION, errors
from .models import Snapshot
from .parsing import CERT_ID_RE, _is_int, _reject

POLICY_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def normalize_snapshot(raw, file_index, entry_index=0):
    """Validate one snapshot object; return (Snapshot | None, reject | None)."""
    if not isinstance(raw, dict):
        return None, _reject(file_index, entry_index, errors.E_SNAPSHOT_SCHEMA,
                             "snapshot entry must be an object")
    structure_version = raw.get("structure_version")
    if not _is_int(structure_version):
        return None, _reject(file_index, entry_index, errors.E_SNAPSHOT_SCHEMA,
                             "structure_version must be an integer")
    if structure_version != STRUCTURE_VERSION:
        return None, _reject(file_index, entry_index,
                             errors.E_SNAPSHOT_STRUCTURE_VERSION,
                             f"unsupported structure_version {structure_version}, "
                             f"expected {STRUCTURE_VERSION}")
    snapshot_version = raw.get("snapshot_version")
    if not _is_int(snapshot_version) or snapshot_version < 0:
        return None, _reject(file_index, entry_index, errors.E_SNAPSHOT_SCHEMA,
                             "snapshot_version must be a non-negative integer")
    policy_version = raw.get("policy_version")
    if not isinstance(policy_version, str) or not POLICY_VERSION_RE.match(policy_version):
        return None, _reject(file_index, entry_index, errors.E_SNAPSHOT_POLICY,
                             f"invalid policy_version format: {policy_version!r}")
    if policy_version not in KNOWN_POLICY_VERSIONS:
        return None, _reject(file_index, entry_index, errors.E_POLICY_UNKNOWN,
                             f"unknown policy_version {policy_version!r}")
    observed_at_ms = raw.get("observed_at_ms")
    if observed_at_ms is not None and not _is_int(observed_at_ms):
        return None, _reject(file_index, entry_index, errors.E_SNAPSHOT_OBS_TIME,
                             "observed_at_ms must be an integer millisecond "
                             "timestamp when present")
    trusted_roots = raw.get("trusted_roots")
    if (not isinstance(trusted_roots, list)
            or not all(isinstance(r, str) and CERT_ID_RE.match(r) for r in trusted_roots)):
        return None, _reject(file_index, entry_index, errors.E_SNAPSHOT_SCHEMA,
                             "trusted_roots must be a list of certificate identifiers")
    revocations_raw = raw.get("revocations", [])
    if not isinstance(revocations_raw, list):
        return None, _reject(file_index, entry_index, errors.E_SNAPSHOT_SCHEMA,
                             "revocations must be a list")
    revocations = {}
    for entry in revocations_raw:
        if (not isinstance(entry, dict)
                or not isinstance(entry.get("cert_id"), str)
                or not CERT_ID_RE.match(entry["cert_id"])
                or not _is_int(entry.get("revoked_at_ms"))):
            return None, _reject(file_index, entry_index, errors.E_SNAPSHOT_SCHEMA,
                                 f"malformed revocation entry: {entry!r}")
        revocations[entry["cert_id"]] = entry["revoked_at_ms"]
    snapshot = Snapshot(
        structure_version=structure_version,
        snapshot_version=snapshot_version,
        policy_version=policy_version,
        observed_at_ms=observed_at_ms,
        trusted_roots=frozenset(trusted_roots),
        revocations=tuple(sorted(revocations.items())),
    )
    return snapshot, None


def parse_snapshot_files(paths):
    """Parse snapshot files; returns (snapshots: list[Snapshot], rejects: list)."""
    snapshots = []
    rejects = []
    for file_index, path in enumerate(paths):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            rejects.append(_reject(file_index, None, errors.E_SNAPSHOT_SCHEMA,
                                   f"unreadable JSON file {path}: {exc}"))
            continue
        entries = data if isinstance(data, list) else [data]
        for entry_index, raw in enumerate(entries):
            snapshot, reject = normalize_snapshot(raw, file_index, entry_index)
            if reject:
                rejects.append(reject)
            else:
                snapshots.append(snapshot)
    return snapshots, rejects


def is_replacement(old: Snapshot, new: Snapshot) -> bool:
    """Any change to the trusted set or the policy version is a new version."""
    return (old.trusted_roots != new.trusted_roots
            or old.policy_version != new.policy_version
            or old.revocations != new.revocations)


class SnapshotStore:
    """Holds every accepted snapshot and resolves the effective one."""

    def __init__(self, snapshots=()):
        self._snapshots = {}
        for snap in snapshots:
            self.add(snap)

    def add(self, snapshot: Snapshot) -> bool:
        """Add a snapshot; returns True if the effective snapshot changed."""
        before = self.effective
        self._snapshots[snapshot.stable_key] = snapshot
        return self.effective is not before

    @property
    def effective(self) -> Snapshot | None:
        if not self._snapshots:
            return None
        return max(self._snapshots.values(), key=lambda s: s.sort_key)

    def __len__(self):
        return len(self._snapshots)

    def all(self):
        return sorted(self._snapshots.values(), key=lambda s: s.sort_key)
