"""需求 7：强制终止后的恢复与半写入清理。"""
import hashlib

from chunkmeta.samples import ChunkDecl, ObjectManifest

from .conftest import make_bucket, make_manifest


def simulate_crash(service, bucket_name="media-store"):
    """模拟进程在分片落盘与元数据提交之间被强制终止。"""
    chunks = []
    for seq, payload in enumerate([b"orphan-payload-a", b"orphan-payload-b"]):
        chunks.append(ChunkDecl(seq=seq, content=payload, size=len(payload),
                                sha256=hashlib.sha256(payload).hexdigest()))
    manifest = ObjectManifest(object_key="crash/victim.bin", version=1,
                              chunks=chunks)
    for chunk in chunks:
        service.chunks.put(chunk.content)  # 分片已落盘
    with service.db.tx() as conn:  # 元数据半提交
        conn.execute(
            "INSERT INTO objects(bucket, object_key, version, state, commit_clock,"
            " chunk_count, total_bytes) VALUES (?, ?, ?, 'committing', NULL, ?, ?)",
            (bucket_name, manifest.object_key, manifest.version,
             len(chunks), sum(c.size for c in chunks)))
        conn.execute(
            "INSERT INTO bucket_meta(bucket, mkey, mvalue, mtype, clock,"
            " meta_version, state) VALUES (?, 'half.meta', '\"x\"', 'str', 0, 1,"
            " 'committing')", (bucket_name,))
    return manifest


def test_recovery_cleans_orphans_and_half_commits(service):
    committed = make_manifest()
    service.ingest(committed, make_bucket())
    crashed = simulate_crash(service)
    files_before = service.chunks.count_files()
    report = service.recover()
    assert "media-store/crash/victim.bin@1" in report.aborted_objects
    assert "media-store/half.meta" in report.removed_half_meta
    assert set(report.removed_orphan_chunks) == {c.sha256 for c in crashed.chunks}
    assert service.chunks.count_files() == files_before - 2
    # 已提交对象不受影响
    full = service.read("media-store", committed.object_key)
    assert full.data == b"".join(c.content for c in committed.chunks)


def test_read_after_recovery_matches_uninterrupted(service):
    crashed = simulate_crash(service)
    service.recover()
    service.ingest(crashed, make_bucket())
    result = service.read("media-store", crashed.object_key)
    assert result.data == b"".join(c.content for c in crashed.chunks)


def test_recovery_is_idempotent(service):
    simulate_crash(service)
    first = service.recover()
    second = service.recover()
    assert first.aborted_objects
    assert second.aborted_objects == []
    assert second.removed_orphan_chunks == []
