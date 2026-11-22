"""需求 6：规模自适应模式取舍与两模式逐字节一致。"""
from chunkmeta.assembly import assemble_read

from .conftest import make_bucket, make_config, make_manifest


def test_precise_mode_below_threshold(service):
    manifest = make_manifest(chunk_count=3)
    service.ingest(manifest, make_bucket())
    result = service.read("media-store", manifest.object_key)
    assert result.assembly.mode == "precise"
    assert result.assembly.rejected_mode == "batch"
    assert "threshold" in result.assembly.trigger
    assert not result.assembly.consistency_anomaly


def test_batch_mode_above_threshold(service):
    manifest = make_manifest(key="bin/big.bin", chunk_count=9)
    service.ingest(manifest, make_bucket())
    result = service.read("media-store", manifest.object_key)
    assert result.assembly.mode == "batch"
    assert result.assembly.rejected_mode == "precise"
    assert result.assembly.batch_error_bound_ms is not None
    assert not result.assembly.consistency_anomaly
    assert result.data == b"".join(c.content for c in manifest.chunks)


def test_both_modes_byte_identical(tmp_path):
    from chunkmeta.chunkstore import ChunkStore
    config = make_config()
    store = ChunkStore(tmp_path)
    manifest = make_manifest(chunk_count=7)
    rows = []
    for chunk in manifest.chunks:
        digest = store.put(chunk.content)
        rows.append((chunk.seq, digest, chunk.size))
    outcome = assemble_read(rows, store, config)
    assert not outcome.consistency_anomaly
    assert outcome.data == b"".join(c.content for c in manifest.chunks)
