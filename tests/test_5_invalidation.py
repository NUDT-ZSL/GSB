import unittest

from tests.helpers import generate_all, make_studio


class InvalidationTests(unittest.TestCase):
    def test_targeted_noise_only_recomputes_one_block(self):
        studio, _ = make_studio()
        generate_all(studio)
        studio.apply_revision_event("evt_rev2_targeted_noise")
        result = studio.generate("clip_0003", parallel=False)
        prop = result["propagation"]

        self.assertEqual([k["ordinal"] for k in prop["recomputed_keyframes"]], [5])
        self.assertEqual(prop["recomputed_blocks"], [1])
        self.assertEqual(prop["recomputed_segments"], [4, 5])
        self.assertEqual(set(prop["reused_blocks"]), {0, 2, 3})
        self.assertEqual(len(prop["reused_keyframes"]), 10)

        audit = studio.cross_clip_cache_audit("clip_0003")
        self.assertEqual(len(audit), len(studio.tasks) - 1)
        self.assertTrue(all(item["artifacts_intact"] for item in audit))

    def test_prompt_revision_recomputes_whole_clip_but_not_others(self):
        studio, _ = make_studio()
        generate_all(studio)
        studio.apply_revision_event("evt_rev2_prompt_embedding")
        prop = studio.generate("clip_0004", parallel=False)["propagation"]
        self.assertEqual(len(prop["recomputed_keyframes"]), 9)
        self.assertEqual(prop["reused_keyframes"], [])
        audit = studio.cross_clip_cache_audit("clip_0004")
        self.assertTrue(all(item["cache_state"] == "reused" for item in audit))

    def test_rerun_after_revision_is_exact_cache_hit(self):
        studio, _ = make_studio()
        generate_all(studio)
        studio.apply_revision_event("evt_rev2_targeted_noise")
        studio.generate("clip_0003", parallel=False)
        second = studio.generate("clip_0003", parallel=False)["propagation"]
        self.assertEqual(second["recomputed_keyframes"], [])
        self.assertTrue(all(k["reuse"] == "exact_hit" for k in second["reused_keyframes"]))

    def test_applied_revision_persists_across_studio_instances(self):
        studio, root = make_studio()
        generate_all(studio)
        studio.apply_revision_event("evt_rev2_targeted_noise")
        studio.generate("clip_0003", parallel=False)

        from kfdiff.engine import Studio
        reloaded = Studio(root)
        self.assertEqual(reloaded.tasks["clip_0003"].revision, 2)
        result = reloaded.generate("clip_0003", parallel=False)
        self.assertEqual(result["propagation"]["recomputed_keyframes"], [])


if __name__ == "__main__":
    unittest.main()
