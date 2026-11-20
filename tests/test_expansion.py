from xretrieval.bridge import BridgeResource
from xretrieval.canon import canonical_bytes


def test_candidates_sorted_by_confidence_then_term(paths):
    resource = BridgeResource.load(paths["v1"])
    for src, tgt in (("en", "de"), ("en", "fr"), ("de", "en")):
        cands = resource.expand_term(src, "c0003_%s" % src, tgt)
        keys = [(-c["confidence"], c["candidate"]) for c in cands]
        assert keys == sorted(keys)


def test_max_candidates_per_term(paths, tmp_path):
    resource = BridgeResource.load(paths["v1"])
    assert all(len(resource.expand_term("en", f"c{i:04d}_en", "de")) <= 3
               for i in range(10))


def test_min_confidence_filter(paths, tmp_path):
    from conftest import tweak_resource
    tweaked = tweak_resource(paths["v1"], tmp_path / "r.json",
                             min_confidence=0.8)
    resource = BridgeResource.load(tweaked)
    for i in range(10):
        for cand in resource.expand_term("en", f"c{i:04d}_en", "de"):
            assert cand["confidence"] >= 0.8


def test_same_language_identity(paths):
    resource = BridgeResource.load(paths["v1"])
    assert resource.expand_term("en", "c0001_en", "en") == [
        {"candidate": "c0001_en", "confidence": 1.0}]


def test_expansion_byte_reproducible(paths):
    resource = BridgeResource.load(paths["v1"])
    a = canonical_bytes(resource.expand_query(
        "en", ["c0001_en", "c0002_en"], ["de", "fr", "zh"])[0])
    b = canonical_bytes(BridgeResource.load(paths["v1"]).expand_query(
        "en", ["c0001_en", "c0002_en"], ["de", "fr", "zh"])[0])
    assert a == b


def test_expansion_limit_degrades(paths, tmp_path):
    from conftest import tweak_resource
    tweaked = tweak_resource(paths["v1"], tmp_path / "r.json",
                             max_expansion_candidates=2)
    resource = BridgeResource.load(tweaked)
    entries, degraded = resource.expand_query(
        "en", ["c0001_en", "c0002_en"], ["de", "fr"])
    assert len(entries) == 2
    assert degraded == [{"category": "EXPANSION_TRUNCATED", "dropped":
                         degraded[0]["dropped"]}]
    assert degraded[0]["dropped"] > 0
