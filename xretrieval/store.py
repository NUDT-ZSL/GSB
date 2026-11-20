"""Versioned snapshot persistence and commit journal.

State directory layout:

    state/
      BUILD.lock                       build-batch mutual exclusion
      CURRENT.json                     query-visible committed view (atomic)
      bridge/v000001-<checksum16>.json
      partitions/<lang>/v000001.json
      commits/commit-<seq>-<hash>.json

Partition and bridge snapshots are committed to the journal as soon as they
are fully written (crash recovery granularity). CURRENT.json is swapped
atomically only when a whole build batch completes, so queries never
observe a half-built batch or mixed bridge versions.
"""

import json
import os
import re
from pathlib import Path

from .canon import canonical_text, checksum, short_checksum
from .errors import E_BUILD_BUSY, E_SNAPSHOT_CORRUPT, XError

COMMIT_RE = re.compile(r"commit-(\d{8})-([0-9a-f]{12})\.json")


def _write_atomic(path, text):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class BuildLock:
    """Non-blocking build-batch lock. Second acquirer gets E_BUILD_BUSY."""

    def __init__(self, state_dir):
        self._path = Path(state_dir) / "BUILD.lock"
        self._fd = None

    def acquire(self, batch_id):
        try:
            self._fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise XError(E_BUILD_BUSY,
                         "another build batch is active",
                         lock=self._path.name)
        os.write(self._fd, batch_id.encode("utf-8"))
        return self

    def release(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


class StateStore:
    def __init__(self, state_dir):
        self.root = Path(state_dir)
        self.commits_dir = self.root / "commits"
        self.bridge_dir = self.root / "bridge"
        self.partitions_dir = self.root / "partitions"
        self.current_path = self.root / "CURRENT.json"

    def ensure_dirs(self):
        for path in (self.commits_dir, self.bridge_dir, self.partitions_dir):
            path.mkdir(parents=True, exist_ok=True)

    # -- commit journal -------------------------------------------------------

    def _commit_names(self):
        if not self.commits_dir.is_dir():
            return []
        return sorted(os.listdir(self.commits_dir))

    def _next_commit_seq(self):
        seqs = [int(m.group(1)) for name in self._commit_names()
                for m in [COMMIT_RE.match(name)] if m]
        return (max(seqs) + 1) if seqs else 1

    def append_commit(self, record):
        self.ensure_dirs()
        record = dict(record)
        record["commit_seq"] = self._next_commit_seq()
        name = f"commit-{record['commit_seq']:08d}-{short_checksum(record, 12)}.json"
        _write_atomic(self.commits_dir / name, canonical_text(record))
        return record

    def load_commits(self):
        records = []
        for name in self._commit_names():
            match = COMMIT_RE.match(name)
            if not match:
                continue
            records.append(_read_json(self.commits_dir / name))
        records.sort(key=lambda r: r["commit_seq"])
        return records

    # -- snapshots ------------------------------------------------------------

    def bridge_snapshot_path(self, snapshot_version, content_checksum):
        return self.bridge_dir / f"v{snapshot_version:06d}-{content_checksum[:16]}.json"

    def partition_snapshot_path(self, lang, partition_version):
        return self.partitions_dir / lang / f"v{partition_version:06d}.json"

    def commit_bridge_snapshot(self, content):
        """Persist a bridge snapshot (whole replacement, never incremental)."""
        self.ensure_dirs()
        path = self.bridge_snapshot_path(content["snapshot_version"],
                                         content["checksum"])
        _write_atomic(path, canonical_text(content))
        self.append_commit({
            "kind": "bridge",
            "snapshot_version": content["snapshot_version"],
            "resource_version": content["resource_version"],
            "resource_checksum": checksum(content["resource"]),
            "checksum": content["checksum"],
            "snapshot_file": str(path.relative_to(self.root)),
        })
        return path

    def commit_partition_snapshot(self, content):
        """Persist a partition snapshot; only called when the partition is
        fully built (all of its documents processed)."""
        self.ensure_dirs()
        path = self.partition_snapshot_path(content["lang"],
                                            content["partition_version"])
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(path, canonical_text(content))
        self.append_commit({
            "kind": "partition",
            "lang": content["lang"],
            "partition_version": content["partition_version"],
            "bridge_resource_version": content["bridge_resource_version"],
            "checksum": content["checksum"],
            "snapshot_file": str(path.relative_to(self.root)),
        })
        return path

    def commit_batch(self, bridge_ref, partition_refs):
        """Record the batch commit and atomically flip CURRENT.json."""
        record = self.append_commit({
            "kind": "batch",
            "bridge": bridge_ref,
            "partitions": partition_refs,
        })
        current = {
            "bridge": bridge_ref,
            "partitions": partition_refs,
            "commit_seq": record["commit_seq"],
        }
        current["checksum"] = checksum(
            {k: v for k, v in current.items() if k != "checksum"})
        _write_atomic(self.current_path, canonical_text(current))
        return current

    # -- reading --------------------------------------------------------------

    def load_current(self):
        if not self.current_path.exists():
            return None
        current = _read_json(self.current_path)
        body = {k: v for k, v in current.items() if k != "checksum"}
        if checksum(body) != current.get("checksum"):
            raise XError(E_SNAPSHOT_CORRUPT,
                         "CURRENT.json checksum mismatch",
                         snapshot="CURRENT", version=current.get("commit_seq"))
        return current

    def load_snapshot(self, relative_file, expect_checksum, snapshot_name,
                      version):
        path = self.root / relative_file
        if not path.exists():
            raise XError(E_SNAPSHOT_CORRUPT,
                         f"snapshot file missing: {relative_file}",
                         snapshot=snapshot_name, version=version)
        content = _read_json(path)
        body = {k: v for k, v in content.items() if k != "checksum"}
        if checksum(body) != content.get("checksum"):
            raise XError(E_SNAPSHOT_CORRUPT,
                         f"snapshot {snapshot_name} v{version} checksum mismatch",
                         snapshot=snapshot_name, version=version)
        if expect_checksum and content["checksum"] != expect_checksum:
            raise XError(E_SNAPSHOT_CORRUPT,
                         f"snapshot {snapshot_name} v{version} does not match "
                         f"commit record",
                         snapshot=snapshot_name, version=version)
        return content

    def committed_view(self):
        """Reconstruct the journaled committed state (recovery view).

        Returns (bridges, partitions) where bridges maps snapshot_version to
        its commit record and partitions maps lang to its latest partition
        commit record.
        """
        bridges = {}
        partitions = {}
        for record in self.load_commits():
            if record["kind"] == "bridge":
                bridges[record["snapshot_version"]] = record
            elif record["kind"] == "partition":
                lang = record["lang"]
                if (lang not in partitions or
                        record["partition_version"] >
                        partitions[lang]["partition_version"]):
                    partitions[lang] = record
        return bridges, partitions
