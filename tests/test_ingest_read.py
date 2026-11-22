"""需求 2/4/8：落盘登记、整读与范围读、幂等提交与异常链路错误码。"""
import pytest

from chunkmeta.errors import (
    E_CHUNK_MISSING,
    E_DIGEST_MISMATCH,
    E_OBJECT_NOT_COMMITTED,
    StoreError,
)

from .conftest import make_bucket, make_manifest


def expected_bytes(manifest):
    return b"".join(c.content for c in manifest.chunks)


def test_ingest_registers_manifest(service):
    manifest = make_manifest()
    result = service.ingest(manifest, make_bucket())
    assert not result.idempotent
    rows = service.db.conn.execute(
        "SELECT seq, digest, size FROM object_chunks ORDER BY seq").fetchall()
    assert [r[0] for r in rows] == [0, 1, 2]
    assert [r[1] for r in rows] == [c.sha256 for c in manifest.chunks]
    assert [r[2] for r in rows] == [c.size for c in manifest.chunks]


def test_full_and_range_read(service):
    manifest = make_manifest(chunk_count=5)
    service.ingest(manifest, make_bucket())
    expect = expected_bytes(manifest)
    full = service.read("media-store", manifest.object_key)
    assert full.data == expect
    part = service.range_read("media-store", manifest.object_key, 7, 23)
    assert part.data == expect[7:30]
    tail = service.range_read("media-store", manifest.object_key,
                              len(expect) - 5, 5)
    assert tail.data == expect[-5:]


def test_reingest_is_idempotent(service):
    manifest = make_manifest()
    service.ingest(manifest, make_bucket())
    files_before = service.chunks.count_files()
    again = service.ingest(manifest, make_bucket())
    assert again.idempotent
    assert service.chunks.count_files() == files_before
    count = service.db.conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
    assert count == 1


def test_meta_version_advances_per_commit(service):
    manifest = make_manifest()
    r1 = service.ingest(manifest, make_bucket())
    r2 = service.ingest(make_manifest(key="docs/b.txt", version=1), make_bucket())
    assert r1.meta_versions["owner"] == 1
    assert r2.meta_versions["owner"] == 2  # 各轮提交识别版本变化


def test_read_uncommitted_object(service):
    with pytest.raises(StoreError) as exc:
        service.read("media-store", "ghost.bin")
    assert exc.value.code == E_OBJECT_NOT_COMMITTED
    assert exc.value.details["object_key"] == "ghost.bin"


def test_chunk_missing_maps_to_error(service):
    manifest = make_manifest()
    service.ingest(manifest, make_bucket())
    service.chunks.remove(manifest.chunks[0].sha256)
    with pytest.raises(StoreError) as exc:
        service.read("media-store", manifest.object_key)
    assert exc.value.code == E_CHUNK_MISSING
    assert exc.value.details["digest"] == manifest.chunks[0].sha256


def test_digest_mismatch_maps_to_error(service):
    manifest = make_manifest()
    service.ingest(manifest, make_bucket())
    digest = manifest.chunks[1].sha256
    path = service.chunks._path_for(digest)
    path.write_bytes(b"tampered")
    with pytest.raises(StoreError) as exc:
        service.read("media-store", manifest.object_key)
    assert exc.value.code == E_DIGEST_MISMATCH
    assert exc.value.details["expected"] == digest
