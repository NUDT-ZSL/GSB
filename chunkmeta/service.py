"""StoreService：分片落盘、元数据提交、读取装配与缓存裁决的编排层。

多进程一致性：分片文件物化幂等（内容寻址 + 原子落盘），
元数据提交经文件锁 + BEGIN IMMEDIATE 事务串行化；
同一对象同一内容版本只提交一次，重复提交幂等。
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .assembly import AssemblyOutcome, assemble_read
from .cache import MetadataCache
from .chunkstore import ChunkStore
from .db import Database
from .errors import E_OBJECT_NOT_COMMITTED, StoreError
from .fixedpoint import to_fixed
from .recovery import recover
from .samples import BucketDecl, ObjectManifest, StoreConfig

if os.name == "nt":
    import msvcrt

    def _lock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    def _unlock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(path):
    fh = open(path, "a+b")
    try:
        _lock(fh)
        yield
    finally:
        _unlock(fh)
        fh.close()


@dataclass
class IngestResult:
    bucket: str
    object_key: str
    version: int
    idempotent: bool          # 重复提交（已存在已提交版本）
    commit_clock: int
    chunk_digests: List[str]
    meta_versions: dict = field(default_factory=dict)
    evicted: List[str] = field(default_factory=list)


@dataclass
class ReadResult:
    data: bytes
    assembly: AssemblyOutcome
    cache_divergence: dict
    cache_mode: str


class StoreService:
    def __init__(self, root_dir, config: StoreConfig):
        self.root = Path(root_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.chunks = ChunkStore(self.root / "chunkdata")
        self.db = Database(self.root / "meta.db",
                           clock_step_ms=config.clock_step_ms,
                           regression_threshold_ms=config.clock_regression_threshold_ms)
        self.cache = MetadataCache(capacity=config.cache_max_entries,
                                   scale=config.fixed_scale,
                                   batch_threshold=config.assembly_cache_threshold)
        self.lock_path = self.root / "store.lock"

    def close(self):
        self.db.close()

    def recover(self):
        return recover(self.db, self.chunks)

    def ingest(self, manifest: ObjectManifest, bucket: BucketDecl) -> IngestResult:
        """分片落盘（事务外，崩溃安全）+ 元数据单事务提交。"""
        digests = [self.chunks.put(chunk.content) for chunk in manifest.chunks]
        total_bytes = sum(chunk.size for chunk in manifest.chunks)
        with file_lock(self.lock_path):
            with self.db.tx() as conn:
                clock = self.db.tick_clock(bucket.clock_ms)
                row = conn.execute(
                    "SELECT state FROM objects WHERE bucket=? AND object_key=? AND version=?",
                    (bucket.bucket, manifest.object_key, manifest.version)).fetchone()
                if row and row[0] == "committed":
                    return IngestResult(
                        bucket=bucket.bucket, object_key=manifest.object_key,
                        version=manifest.version, idempotent=True,
                        commit_clock=clock, chunk_digests=digests)
                conn.execute(
                    "INSERT OR REPLACE INTO objects"
                    "(bucket, object_key, version, state, commit_clock, chunk_count, total_bytes)"
                    " VALUES (?, ?, ?, 'committing', NULL, ?, ?)",
                    (bucket.bucket, manifest.object_key, manifest.version,
                     len(manifest.chunks), total_bytes))
                conn.execute(
                    "DELETE FROM object_chunks WHERE bucket=? AND object_key=? AND version=?",
                    (bucket.bucket, manifest.object_key, manifest.version))
                for chunk, digest in zip(manifest.chunks, digests):
                    conn.execute(
                        "INSERT INTO object_chunks(bucket, object_key, version, seq, digest, size)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (bucket.bucket, manifest.object_key, manifest.version,
                         chunk.seq, digest, chunk.size))
                    conn.execute(
                        "INSERT OR IGNORE INTO chunks(digest, size, state, clock)"
                        " VALUES (?, ?, 'materialized', ?)",
                        (digest, chunk.size, clock))
                meta_versions = {}
                evicted: List[str] = []
                for mkey, mvalue in bucket.metadata.items():
                    prev = conn.execute(
                        "SELECT meta_version FROM bucket_meta WHERE bucket=? AND mkey=?",
                        (bucket.bucket, mkey)).fetchone()
                    meta_version = (prev[0] + 1) if prev else 1
                    conn.execute(
                        "INSERT OR REPLACE INTO bucket_meta"
                        "(bucket, mkey, mvalue, mtype, clock, meta_version, state)"
                        " VALUES (?, ?, ?, ?, ?, ?, 'committed')",
                        (bucket.bucket, mkey, json.dumps(mvalue, sort_keys=True),
                         type(mvalue).__name__, clock, meta_version))
                    meta_versions[mkey] = meta_version
                    evicted.extend(self.cache.put(mkey, mvalue, clock))
                    entry = self.cache.entries.get(mkey)
                    if entry is not None:
                        prev_entry = conn.execute(
                            "SELECT entry_version FROM cache_entries WHERE bucket=? AND mkey=?",
                            (bucket.bucket, mkey)).fetchone()
                        entry_version = (prev_entry[0] + 1) if prev_entry else 1
                        conn.execute(
                            "INSERT OR REPLACE INTO cache_entries"
                            "(bucket, mkey, occupancy_fixed, occupancy_decimal, last_clock, entry_version)"
                            " VALUES (?, ?, ?, ?, ?, ?)",
                            (bucket.bucket, mkey, entry.occupancy_fixed,
                             str(entry.occupancy_dec), clock, entry_version))
                conn.execute(
                    "UPDATE objects SET state='committed', commit_clock=?"
                    " WHERE bucket=? AND object_key=? AND version=?",
                    (clock, bucket.bucket, manifest.object_key, manifest.version))
        return IngestResult(
            bucket=bucket.bucket, object_key=manifest.object_key,
            version=manifest.version, idempotent=False,
            commit_clock=clock, chunk_digests=digests,
            meta_versions=meta_versions, evicted=evicted)

    def _committed_chunks(self, bucket: str, key: str, version: Optional[int]):
        if version is None:
            row = self.db.conn.execute(
                "SELECT MAX(version) FROM objects WHERE bucket=? AND object_key=? AND state='committed'",
                (bucket, key)).fetchone()
            version = row[0] if row and row[0] is not None else None
        if version is None:
            raise StoreError(E_OBJECT_NOT_COMMITTED,
                             "object not committed", bucket=bucket, object_key=key)
        state = self.db.conn.execute(
            "SELECT state FROM objects WHERE bucket=? AND object_key=? AND version=?",
            (bucket, key, version)).fetchone()
        if not state or state[0] != "committed":
            raise StoreError(E_OBJECT_NOT_COMMITTED,
                             "object not committed",
                             bucket=bucket, object_key=key, version=version)
        rows = self.db.conn.execute(
            "SELECT seq, digest, size FROM object_chunks"
            " WHERE bucket=? AND object_key=? AND version=? ORDER BY seq",
            (bucket, key, version)).fetchall()
        return rows, version

    def _reserve_read_budget(self):
        """在事务内推进时钟并为本次读取预留窗口预算。"""
        budget_fixed = to_fixed(self.config.read_latency_budget_ms,
                                self.config.fixed_scale, stage="budget").fixed
        cap = budget_fixed * self.config.budget_window_factor
        with self.db.tx():
            clock = self.db.tick_clock()
            lease = self.db.reserve_budget(window=clock, amount_fixed=budget_fixed,
                                           cap_fixed=cap)
        return lease, clock

    def read(self, bucket: str, key: str, version: Optional[int] = None) -> ReadResult:
        rows, version = self._committed_chunks(bucket, key, version)
        t0 = time.perf_counter()
        self.cache.touch(key, self.db.watermark())
        cache_lookup_ms = (time.perf_counter() - t0) * 1000.0
        lease, clock = self._reserve_read_budget()
        try:
            outcome = assemble_read(rows, self.chunks, self.config,
                                    cache_lookup_ms=cache_lookup_ms)
        finally:
            self.db.release_budget(lease)
        _fixed_seq, _dec_seq, divergence, cache_mode = self.cache.dual_path_order()
        return ReadResult(data=outcome.data, assembly=outcome,
                          cache_divergence=divergence.to_dict(),
                          cache_mode=cache_mode)

    def range_read(self, bucket: str, key: str, start: int, length: int,
                   version: Optional[int] = None) -> ReadResult:
        if start < 0 or length < 0:
            raise StoreError("E_BAD_RANGE", "invalid range", start=start, length=length)
        rows, version = self._committed_chunks(bucket, key, version)
        offsets = []
        pos = 0
        for seq, digest, size in rows:
            offsets.append((seq, digest, size, pos))
            pos += size
        end = start + length
        needed = [item for item in offsets
                  if item[3] < end and item[3] + item[2] > start]
        lease, clock = self._reserve_read_budget()
        try:
            outcome = assemble_read(
                [(seq, digest, size) for seq, digest, size, _ in needed],
                self.chunks, self.config, cache_lookup_ms=0.0)
        finally:
            self.db.release_budget(lease)
        base = needed[0][3] if needed else 0
        slice_start = start - base
        data = outcome.data[slice_start:slice_start + length]
        _fixed_seq, _dec_seq, divergence, cache_mode = self.cache.dual_path_order()
        return ReadResult(data=data, assembly=outcome,
                          cache_divergence=divergence.to_dict(),
                          cache_mode=cache_mode)
