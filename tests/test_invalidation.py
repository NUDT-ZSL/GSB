"""要求 5：三类变更的定向失效闭包与重算/复用集合。"""

from statediag.cache import CommitCache, content_key
from statediag.consistency import verify_partition
from statediag.ingestion import ingest
from statediag.invalidation import plan_invalidation
from statediag.model import TransformConfig

from tests.support import SampleCase


class InvalidationTest(SampleCase):
    def setUp(self):
        self.base = ingest(self.tasks_path, self.state_dim)
        self.old_by_id = {r["task"].task_id: r["task"] for r in self.base}
        self.cfg = TransformConfig(1, "float64", "half-even", 1e-12, 1e-9, 0)
        self.all_ids = [r["task"].task_id for r in self.base]

    def _committed_keys(self):
        cache = CommitCache()
        for record in self.base:
            task = record["task"]
            cache.put(content_key(task, self.cfg), "checksum", {}, {})
        return {entry["key"] for entry in cache._entries.values()}

    def test_revision_bump_only_suffix_recomputed(self):
        new_records = ingest(self.revision_path, self.state_dim)
        plan = plan_invalidation(
            [r["task"] for r in new_records], self.old_by_id,
            self.cfg, self.cfg, self._committed_keys(),
        )
        self.assertEqual(plan.kind, "revision_bump")
        self.assertEqual(plan.changed_task_ids, ("t03", "t04", "t05"))
        self.assertEqual(set(plan.recompute_task_ids), {"t03", "t04", "t05"})
        self.assertEqual(set(plan.reuse_task_ids), {"t00", "t01", "t02"})
        verify_partition(plan, self.all_ids)

    def test_algebra_bump_invalidates_all(self):
        cfg2 = TransformConfig(2, "float64", "half-even", 1e-12, 1e-9, 0)
        plan = plan_invalidation(
            [r["task"] for r in self.base], self.old_by_id,
            self.cfg, cfg2, set(),
        )
        self.assertEqual(plan.kind, "algebra_bump")
        self.assertEqual(set(plan.affected_task_ids), set(self.all_ids))
        self.assertEqual(plan.reuse_task_ids, ())
        verify_partition(plan, self.all_ids)

    def test_block_size_change_suffix_closure(self):
        new_records = ingest(self.block_path, self.state_dim)
        plan = plan_invalidation(
            [r["task"] for r in new_records], self.old_by_id,
            self.cfg, self.cfg, self._committed_keys(),
        )
        self.assertEqual(plan.kind, "block_size_change")
        self.assertEqual(plan.changed_task_ids, ("t02",))
        self.assertEqual(
            set(plan.affected_task_ids), {"t02", "t03", "t04", "t05"}
        )
        # t02 内容键变化必须重算；其余后缀内容未变且已提交，可复用。
        self.assertEqual(plan.recompute_task_ids, ("t02",))
        verify_partition(plan, self.all_ids)

    def test_partitions_disjoint_and_cover(self):
        new_records = ingest(self.revision_path, self.state_dim)
        plan = plan_invalidation(
            [r["task"] for r in new_records], self.old_by_id,
            self.cfg, self.cfg, set(),
        )
        self.assertEqual(
            set(plan.recompute_task_ids) & set(plan.reuse_task_ids), set()
        )
        self.assertEqual(
            set(plan.recompute_task_ids) | set(plan.reuse_task_ids),
            set(self.all_ids),
        )
