"""强制终止后的恢复与半写入清理。

恢复路径识别：
  - 无引用孤片：磁盘上存在但没有任何已提交对象引用的分片文件；
  - 半提交对象：state='committing' 的对象行及其分片清单；
  - 半提交元数据：state='committing' 的元数据行。
只清理或补齐未完成部分，已提交数据不受影响。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from .chunkstore import ChunkStore
from .db import Database


@dataclass
class RecoveryReport:
    aborted_objects: List[str] = field(default_factory=list)
    removed_half_meta: List[str] = field(default_factory=list)
    removed_orphan_chunks: List[str] = field(default_factory=list)
    removed_stale_chunk_rows: List[str] = field(default_factory=list)

    def to_dict(self):
        return {
            "aborted_objects": self.aborted_objects,
            "removed_half_meta": self.removed_half_meta,
            "removed_orphan_chunks": self.removed_orphan_chunks,
            "removed_stale_chunk_rows": self.removed_stale_chunk_rows,
        }


def recover(db: Database, store: ChunkStore) -> RecoveryReport:
    report = RecoveryReport()
    with db.tx() as conn:
        # 1. 半提交对象：中止并清除其分片清单
        rows = conn.execute(
            "SELECT bucket, object_key, version FROM objects WHERE state = 'committing'"
        ).fetchall()
        for bucket, key, version in rows:
            conn.execute(
                "DELETE FROM object_chunks WHERE bucket=? AND object_key=? AND version=?",
                (bucket, key, version))
            conn.execute(
                "UPDATE objects SET state='aborted' WHERE bucket=? AND object_key=? AND version=?",
                (bucket, key, version))
            report.aborted_objects.append(f"{bucket}/{key}@{version}")
        # 2. 半提交元数据：直接清除
        rows = conn.execute(
            "SELECT bucket, mkey FROM bucket_meta WHERE state = 'committing'"
        ).fetchall()
        for bucket, mkey in rows:
            conn.execute(
                "DELETE FROM bucket_meta WHERE bucket=? AND mkey=? AND state='committing'",
                (bucket, mkey))
            report.removed_half_meta.append(f"{bucket}/{mkey}")
        # 3. 已提交对象引用的摘要集合
        referenced = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT oc.digest FROM object_chunks oc "
                "JOIN objects o ON o.bucket=oc.bucket AND o.object_key=oc.object_key "
                "AND o.version=oc.version WHERE o.state='committed'"
            ).fetchall()
        }
        known = {
            row[0] for row in conn.execute("SELECT digest FROM chunks").fetchall()
        }
        # 4. 数据库登记但磁盘缺失的登记行：清除
        for digest in sorted(known):
            if not store.exists(digest):
                conn.execute("DELETE FROM chunks WHERE digest=?", (digest,))
                report.removed_stale_chunk_rows.append(digest)
        # 5. 磁盘孤片：存在但无已提交对象引用
        for digest in store.list_digests():
            if digest not in referenced:
                store.remove(digest)
                conn.execute("DELETE FROM chunks WHERE digest=?", (digest,))
                report.removed_orphan_chunks.append(digest)
    return report
