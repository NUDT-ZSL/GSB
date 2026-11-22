import json
import os
import subprocess
import sys
import threading

import pytest

from pipeline.builder import BuildConfig, IndexBuilder
from pipeline.encoding import chunk_postings, decode_block, encode_block
from pipeline.errors import (BuildBusyError, ConfigInvalidError,
                             InconsistentError, ResourceLimitError,
                             ScoreMismatchError, SnapshotCorruptError,
                             SnapshotRollbackError)
from pipeline.index_reader import IndexService, IndexSnapshotView
from pipeline.sample_gen import generate_docs, generate_queries
from pipeline.snapshot import SnapshotStore
from pipeline.verify import (run_full_verification,
                             verify_recompute_reuse)


@pytest.fixture()
def sample(tmp_path):
    docs = str(tmp_path / "docs.jsonl")
    queries = str(tmp_path / "queries.jsonl")
    generate_docs(docs, num_docs=120, seed=42)
    generate_queries(queries, num_queries=15, seed=123)
    return docs, queries


def build(store_dir, docs, **cfg):
    return IndexBuilder(store_dir, BuildConfig(**cfg)).build(docs)


def test_build_is_deterministic(sample, tmp_path):
    docs, _ = sample
    s1 = build(str(tmp_path / "s1"), docs)
    s2 = build(str(tmp_path / "s2"), docs)
    assert s1 == s2
    b1 = open(str(tmp_path / "s1" / "index.json"), "rb").read()
    b2 = open(str(tmp_path / "s2" / "index.json"), "rb").read()
    assert b1 == b2


def test_block_roundtrip_and_independent_decode():
    postings = [(1, 2), (5, 1), (9, 3), (40, 1), (1000, 7)]
    for size in (1, 2, 3, 5, 100):
        chunks = chunk_postings(postings, size)
        decoded = []
        for chunk in chunks:
            blob = encode_block(chunk)
            part, end = decode_block(blob)
            assert end == len(blob)
            assert part == chunk
            decoded.extend(part)
        assert decoded == postings


def test_invalid_block_size():
    with pytest.raises(ConfigInvalidError) as e:
        chunk_postings([(1, 1)], 0)
    assert e.value.code == "E_CONFIG_INVALID"
    with pytest.raises(ConfigInvalidError):
        BuildConfig(block_size=-1)


def test_scores_match_baseline(sample, tmp_path):
    docs, queries = sample
    store = str(tmp_path / "store")
    build(store, docs, block_size=4)
    service = IndexService(os.path.join(store, "index.json"))
    with open(queries) as fh:
        for line in fh:
            q = json.loads(line)
            res = service.query_checked(q["query_id"], q["terms"],
                                        q["top_k"], 1e-9)
            assert len(res) <= q["top_k"]
            scores = [r["score"] for r in res]
            assert scores == sorted(scores, reverse=True)
            for r in res:
                assert set(r["matched_terms"]) <= set(q["terms"])


def test_score_mismatch_detected(sample, tmp_path, monkeypatch):
    docs, _ = sample
    store = str(tmp_path / "store")
    build(store, docs)
    service = IndexService(os.path.join(store, "index.json"))
    monkeypatch.setattr(service, "query",
                        lambda terms, k: [{"doc_id": 1, "score": 999.0,
                                           "matched_terms": []}])
    with pytest.raises(ScoreMismatchError) as e:
        service.query_checked(7, ["term001"], 5, 1e-9)
    assert e.value.code == "E_SCORE_MISMATCH"
    assert e.value.query_id == 7 and e.value.doc_id == 1
    assert e.value.delta > 1e-9


def run_build_subprocess(store, docs, kill_at=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(__file__))
    if kill_at:
        env["INDEX_PIPELINE_KILL_AT"] = kill_at
    else:
        env.pop("INDEX_PIPELINE_KILL_AT", None)
    code = (
        "from pipeline.builder import IndexBuilder, BuildConfig;"
        "import sys;"
        "IndexBuilder(sys.argv[1], BuildConfig(block_size=4)).build(sys.argv[2])"
    )
    return subprocess.run([sys.executable, "-c", code, store, docs],
                          env=env, capture_output=True)


@pytest.mark.parametrize("kill_at", [
    "ingest:doc", "ingest:pre_commit",
    "compress:doc", "compress:pre_commit",
    "stats:pre_commit",
])
def test_crash_recovery_matches_uninterrupted(sample, tmp_path, kill_at):
    docs, _ = sample
    crashed = str(tmp_path / "crashed")
    proc = run_build_subprocess(crashed, docs, kill_at=kill_at)
    assert proc.returncode == 99
    proc2 = run_build_subprocess(crashed, docs)
    assert proc2.returncode == 0, proc2.stderr.decode()
    clean = str(tmp_path / "clean")
    assert run_build_subprocess(clean, docs).returncode == 0
    assert open(os.path.join(crashed, "index.json"), "rb").read() == \
           open(os.path.join(clean, "index.json"), "rb").read()


def test_committed_stages_not_recomputed(sample, tmp_path):
    docs, _ = sample
    store = str(tmp_path / "store")
    proc = run_build_subprocess(store, docs, kill_at="stats:pre_commit")
    assert proc.returncode == 99
    versions_before = {}
    for stage in ("ingest", "compress"):
        rec = SnapshotStore(store).load(stage)
        versions_before[stage] = rec["header"]["version"]
    assert run_build_subprocess(store, docs).returncode == 0
    store2 = SnapshotStore(store)
    for stage, ver in versions_before.items():
        assert store2.load(stage)["header"]["version"] == ver


def test_snapshot_rollback_rejected(tmp_path):
    store = SnapshotStore(str(tmp_path / "store"))
    store.commit("ingest", 2, [1, 2], {"a": 1})
    with pytest.raises(SnapshotRollbackError) as e:
        store.commit("ingest", 1, [1], {"a": 1})
    assert e.value.code == "E_SNAPSHOT_ROLLBACK"
    with pytest.raises(SnapshotRollbackError):
        store.commit("ingest", 2, [1], {"a": 1})


def test_snapshot_corrupt_detected(tmp_path):
    root = str(tmp_path / "store")
    store = SnapshotStore(root)
    store.commit("compress", 1, [1], {"x": [1, 2, 3]})
    path = os.path.join(root, "stages", "compress.snap.json")
    data = open(path, "rb").read().replace(b"[1,2,3]", b"[1,2,4]")
    open(path, "wb").write(data)
    with pytest.raises(SnapshotCorruptError) as e:
        SnapshotStore(root).load("compress")
    assert e.value.code == "E_SNAPSHOT_CORRUPT"
    assert e.value.stage == "compress" and e.value.version == 1


def test_snapshot_unparseable_detected(tmp_path):
    root = str(tmp_path / "store")
    store = SnapshotStore(root)
    store.commit("stats", 3, [1], {"x": 1})
    path = os.path.join(root, "stages", "stats.snap.json")
    open(path, "wb").write(b"{not json")
    with pytest.raises(SnapshotCorruptError):
        SnapshotStore(root).load("stats")


def test_whole_replacement_on_new_version(sample, tmp_path):
    docs, _ = sample
    store = str(tmp_path / "store")
    build(store, docs)
    first = open(os.path.join(store, "index.json"), "rb").read()
    docs2 = str(tmp_path / "docs2.jsonl")
    generate_docs(docs2, num_docs=80, seed=7)
    build(store, docs2)
    second = open(os.path.join(store, "index.json"), "rb").read()
    assert first != second
    view = IndexSnapshotView(json.loads(second.decode()))
    assert view.stats["doc_count"] == 80


def test_build_busy_on_concurrent_commit(tmp_path):
    store = SnapshotStore(str(tmp_path / "store"))
    store._commit_lock.acquire()
    try:
        with pytest.raises(BuildBusyError) as e:
            store.commit("ingest", 1, [1], {})
        assert e.value.code == "E_BUILD_BUSY"
    finally:
        store._commit_lock.release()


def test_concurrent_queries_consistent(sample, tmp_path):
    docs, queries = sample
    store = str(tmp_path / "store")
    build(store, docs)
    service = IndexService(os.path.join(store, "index.json"))
    qs = [json.loads(l) for l in open(queries) if l.strip()]
    expected = [service.query(q["terms"], q["top_k"]) for q in qs]
    errors = []

    def worker():
        try:
            for _ in range(20):
                for q, exp in zip(qs, expected):
                    assert service.query(q["terms"], q["top_k"]) == exp
        except AssertionError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


def test_resource_limit_postings(sample, tmp_path):
    docs, _ = sample
    with pytest.raises(ResourceLimitError) as e:
        build(str(tmp_path / "store"), docs, max_postings=10)
    assert e.value.code == "E_RESOURCE_LIMIT"


def test_cache_limit_degrades_to_streaming(sample, tmp_path):
    docs, queries = sample
    store = str(tmp_path / "store")
    build(store, docs, block_size=4)
    service = IndexService(os.path.join(store, "index.json"),
                           max_cache_blocks=0)
    q = json.loads(open(queries).readline())
    res = service.query(q["terms"], q["top_k"])
    assert res
    assert "cache_block_limit" in service.degradation_categories
    assert service.degraded_terms
    service2 = IndexService(os.path.join(store, "index.json"))
    assert res == service2.query(q["terms"], q["top_k"])


def test_full_verification_passes(sample, tmp_path):
    docs, queries = sample
    store = str(tmp_path / "store")
    build(store, docs, block_size=4)
    report = run_full_verification(
        os.path.join(store, "index.json"), docs, queries)
    assert report["ok"] and report["terms_checked"] > 0


def test_verification_detects_tampered_block(sample, tmp_path):
    docs, queries = sample
    store = str(tmp_path / "store")
    build(store, docs, block_size=4)
    index_path = os.path.join(store, "index.json")
    payload = json.loads(open(index_path).read())
    term = sorted(payload["terms"])[0]
    blk = payload["terms"][term]["blocks"][0]
    raw = bytearray.fromhex(blk["data"])
    raw[-1] ^= 0x01
    blk["data"] = raw.hex()
    open(index_path, "w").write(json.dumps(payload))
    with pytest.raises((InconsistentError, ConfigInvalidError)):
        run_full_verification(index_path, docs, queries)


def test_recompute_reuse_sets():
    assert verify_recompute_reuse(["a", "b"], ["c"], ["a", "b", "c"])
    with pytest.raises(InconsistentError) as e:
        verify_recompute_reuse(["a", "b"], ["b"], ["a", "b"])
    assert e.value.category == "recompute_reuse_overlap"
    with pytest.raises(InconsistentError) as e:
        verify_recompute_reuse(["a"], [], ["a", "b"])
    assert e.value.category == "recompute_reuse_coverage"
