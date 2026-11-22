"""需求 2：内容寻址原子落盘与重复内容复用。"""
import hashlib

from chunkmeta.chunkstore import ChunkStore


def test_put_is_content_addressed(tmp_path):
    store = ChunkStore(tmp_path)
    content = b"chunk-payload"
    digest = store.put(content)
    assert digest == hashlib.sha256(content).hexdigest()
    assert store.get(digest) == content


def test_duplicate_content_reuses_single_file(tmp_path):
    store = ChunkStore(tmp_path)
    content = b"same-content"
    d1 = store.put(content)
    d2 = store.put(content)
    assert d1 == d2
    assert store.count_files() == 1


def test_no_temp_files_left(tmp_path):
    store = ChunkStore(tmp_path)
    store.put(b"x" * 32)
    assert list(store.tmp_dir.iterdir()) == []
