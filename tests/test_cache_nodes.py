"""业务要求 4：内容寻址缓存、缓存键组成与查询入口。"""

import unittest

import numpy as np

from rope_attn.cache import ContentCache
from rope_attn.engine import EngineContext, compute_layer
from rope_attn.nodes import build_cache_key, node_id
from tests._helpers import TempWorkspace, make_task


class ContentCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = TempWorkspace()

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def test_same_key_reuses_different_key_recomputes(self):
        cache = ContentCache(str(self.workspace.cache))
        task = make_task()
        array = np.arange(24, dtype=np.float32).reshape(2, 3, 4)
        key = build_cache_key(
            task, 0, "score", 1,
            {"a": "digest-a"}, {"up": "digest-up"}
        )
        first = cache.put(key, array)
        second = cache.lookup(key)
        self.assertIsNotNone(second)
        self.assertEqual(first.blob_hash, second.blob_hash)
        restored = cache.get_array(second)
        self.assertEqual(restored.tobytes(), array.tobytes())

        changed = build_cache_key(
            task, 0, "score", 1,
            {"a": "digest-b"}, {"up": "digest-up"}
        )
        self.assertIsNone(cache.lookup(changed))

    def test_key_includes_semantic_params(self):
        task = make_task()
        key = build_cache_key(task, 0, "score", 2, {}, {})
        payload = key.to_dict()
        self.assertEqual(payload["params"]["block_size"], task.block_size)
        self.assertEqual(payload["params"]["causal"], task.causal)
        self.assertEqual(payload["params"]["kv_revision"], task.kv_revision)
        self.assertEqual(payload["params"]["block_index"], 2)

    def test_inspect_by_case_and_block(self):
        config = self.workspace.config()
        cache = ContentCache(config.cache_dir)
        task = make_task()
        ctx = EngineContext(task, config, cache)
        compute_layer(ctx, 0)
        entries = cache.inspect_node(task.case_id, block=0)
        nodes = {item["node"] for item in entries}
        self.assertIn(node_id(task.case_id, 0, "score", 0), nodes)
        self.assertIn(node_id(task.case_id, 0, "stats", 0), nodes)
        for item in entries:
            self.assertTrue(item["key_hash"])
            self.assertTrue(item["blob_hash"])
            self.assertIn("key_payload", item)

    def test_manifest_content_verification(self):
        cache = ContentCache(str(self.workspace.cache))
        task = make_task()
        key = build_cache_key(task, 0, "q", None, {"x": "1"}, {})
        entry = cache.put(key, np.ones((2, 2), dtype=np.float32))
        result = cache.verify_entry(entry)
        self.assertTrue(result["content_ok"])


if __name__ == "__main__":
    unittest.main()
