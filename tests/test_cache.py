"""Content-addressable cache semantics."""
import numpy as np
import pytest

from quant_conv_pipeline import nodes
from quant_conv_pipeline.cache import ContentCache, serialize_artifact
from quant_conv_pipeline.errors import Code, PipelineError
from quant_conv_pipeline.sample_gen import default_jobs


def _cache(tmp_path):
    return ContentCache(str(tmp_path / "cache"))


def test_same_key_reuse_different_key_recompute(tmp_path):
    cache = _cache(tmp_path)
    job = default_jobs(2)[0]
    payload = nodes.payload_quant_input_prep(job)
    key1, art1, reused1 = nodes.run_node(cache, nodes.QUANT_INPUT_PREP, payload)
    key2, art2, reused2 = nodes.run_node(cache, nodes.QUANT_INPUT_PREP, payload)
    assert key1 == key2 and reused1 is False and reused2 is True
    assert art1["q_input"].tobytes() == art2["q_input"].tobytes()
    changed = dict(job, quant_scale=job["quant_scale"] * 1.01)
    key3, _, reused3 = nodes.run_node(
        cache, nodes.QUANT_INPUT_PREP, nodes.payload_quant_input_prep(changed))
    assert key3 != key1 and reused3 is False


def test_key_covers_required_ingredients(tmp_path):
    job = default_jobs(2)[0]
    base = nodes.payload_quant_input_prep(job)
    variants = []
    for field, value in (("quant_scale", 0.5), ("zero_point", 1),
                         ("block_size", 8), ("seed", 123)):
        variants.append(
            nodes.payload_quant_input_prep(dict(job, **{field: value})))
    keys = [ContentCache.key_for(nodes.QUANT_INPUT_PREP, p)
            for p in [base] + variants]
    assert len(set(keys)) == len(keys)


def test_upstream_hash_changes_downstream_key(tmp_path):
    job = default_jobs(2)[0]
    wp = {"job_id": job["job_id"], "weight_revision": 1,
          "weight_scale": 0.01, "weight_zero_point": 0}
    p1 = nodes.payload_weight_quant(job, wp, "upstream-A")
    p2 = nodes.payload_weight_quant(job, wp, "upstream-B")
    assert ContentCache.key_for(nodes.WEIGHT_QUANT, p1) != \
        ContentCache.key_for(nodes.WEIGHT_QUANT, p2)


def test_entry_replayable_by_key(tmp_path):
    cache = _cache(tmp_path)
    job = default_jobs(2)[0]
    key, art, _ = nodes.run_node(cache, nodes.QUANT_INPUT_PREP,
                                 nodes.payload_quant_input_prep(job))
    replayed, ok = nodes.replay(cache, key)
    assert ok
    assert replayed["q_input"].tobytes() == art["q_input"].tobytes()


def test_replay_missing_entry_raises_lineage_broken(tmp_path):
    cache = _cache(tmp_path)
    with pytest.raises(PipelineError) as ei:
        nodes.replay(cache, "0" * 64)
    assert ei.value.code == Code.E_LINEAGE_BROKEN


def test_serialization_deterministic():
    art = {"b": np.arange(6, dtype=np.int32).reshape(2, 3),
           "a": np.array([[1.5, -2.5]], dtype=np.float64)}
    assert serialize_artifact(art) == serialize_artifact(art)
