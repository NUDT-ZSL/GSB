"""Unit + integration tests for the match-verification component."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from matcher import (  # noqa: E402
    BusinessError,
    Config,
    Pipeline,
    SimulatedCrash,
)
from matcher.ingest import load_candidates, load_ground_truth  # noqa: E402
from matcher.ledger import read_ledger, read_v1, read_v2  # noqa: E402
from matcher.match import iou_detail, match_image  # noqa: E402
from matcher.migrate import status_multiset, verify_migration  # noqa: E402
from matcher.models import Candidate, GTBox  # noqa: E402
from matcher.replay import replay_row  # noqa: E402
from matcher.selfcheck import selfcheck  # noqa: E402
from matcher.snapshot import StageLock, SnapshotStore  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_samples(target):
    subprocess.run(
        [sys.executable, os.path.join(ROOT, "generate_samples.py"),
         "--out", target],
        check=True, capture_output=True,
    )


class WorkspaceMixin(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="matcher_test_")
        self.samples = os.path.join(self.root, "samples")
        make_samples(self.samples)
        self.cand = os.path.join(self.samples, "candidates.jsonl")
        self.gt = os.path.join(self.samples, "ground_truth.jsonl")
        self.gt2 = os.path.join(self.samples, "ground_truth_v2.jsonl")
        self.addCleanup(shutil.rmtree, self.root, True)

    def pipeline(self, **kwargs):
        return Pipeline(self.root, Config(**kwargs))


class TestIngest(WorkspaceMixin):
    def test_duplicate_identity(self):
        with open(self.cand) as fh:
            first = fh.readline()
        dup = os.path.join(self.root, "dup.jsonl")
        with open(dup, "w") as fh:
            fh.write(first)
            fh.write(first)
        with self.assertRaises(BusinessError) as ctx:
            load_candidates(dup, (0, 1, 2))
        self.assertEqual(ctx.exception.code, "E_DUP_IMAGE")
        self.assertEqual(ctx.exception.field, "image_id")

    def test_bad_box_and_class(self):
        bad = os.path.join(self.root, "bad.jsonl")
        with open(bad, "w") as fh:
            fh.write(json.dumps({"image_id": "a", "box_idx": 0, "x1": -1,
                                 "y1": 0, "x2": 5, "y2": 5, "score": 0.5,
                                 "class_id": 0}) + "\n")
        with self.assertRaises(BusinessError) as ctx:
            load_candidates(bad, (0, 1, 2))
        self.assertEqual(ctx.exception.code, "E_BAD_BOX")
        with open(bad, "w") as fh:
            fh.write(json.dumps({"image_id": "a", "box_idx": 0, "x1": 0,
                                 "y1": 0, "x2": 5, "y2": 5, "score": 0.5,
                                 "class_id": 9}) + "\n")
        with self.assertRaises(BusinessError) as ctx:
            load_candidates(bad, (0, 1, 2))
        self.assertEqual(ctx.exception.field, "class_id")

    def test_deterministic_intermediate(self):
        from matcher.ingest import build_intermediate
        cands = load_candidates(self.cand, (0, 1, 2))
        gts = load_ground_truth(self.gt, (0, 1, 2))
        _, ir1 = build_intermediate(cands, gts, (0, 1, 2))
        _, ir2 = build_intermediate(
            load_candidates(self.cand, (0, 1, 2)),
            load_ground_truth(self.gt, (0, 1, 2)), (0, 1, 2))
        self.assertEqual(ir1, ir2)


class TestMatch(unittest.TestCase):
    def test_greedy_assignment_and_audit(self):
        gt = GTBox("img", 0, 0.0, 0.0, 10.0, 10.0, 1, 0, False, 1)
        c_hi = Candidate("img", 0, 0.0, 0.0, 10.0, 10.0, 0.9, 1)
        c_lo = Candidate("img", 1, 1.0, 1.0, 11.0, 11.0, 0.5, 1)
        c_fp = Candidate("img", 2, 100.0, 100.0, 110.0, 110.0, 0.8, 1)
        im = match_image("img", [c_lo, c_hi, c_fp], [gt], 0.5)
        status = {r.cand_idx: r.status for r in im.rows if r.cand_idx is not None}
        self.assertEqual(status, {0: "TP", 1: "FP", 2: "FP"})
        self.assertTrue(all(c["result"] in ("accept", "reject")
                            for c in im.comparisons))
        self.assertGreater(len(im.comparisons), 0)
        inter, union, iou = iou_detail(c_hi, gt)
        self.assertEqual((inter, union, iou), (100.0, 100.0, 1.0))
        # FN for unmatched ground truth
        im2 = match_image("img", [], [gt], 0.5)
        self.assertEqual(im2.rows[0].status, "FN")
        self.assertEqual(im2.rows[0].gt_revision, 1)


class TestLedger(WorkspaceMixin):
    def test_dual_write_read_and_dispatch(self):
        p = self.pipeline()
        report = p.run(self.cand, self.gt)
        self.assertEqual(report["rows_v1"], report["rows_v2"])
        v1_version, doc_v1 = read_ledger(p.v1_path)
        v2_version, data_v2 = read_ledger(p.v2_dir)
        self.assertEqual((v1_version, v2_version), ("v1", "v2"))
        self.assertEqual(len(doc_v1["rows"]), len(data_v2["rows"]))
        row = data_v2["rows"][0]
        for field in ("t_abs_ms", "generation", "row_hash"):
            self.assertIn(field, row)
        self.assertIn("t_rel_s", doc_v1["rows"][0])

    def test_unknown_version(self):
        fake = os.path.join(self.root, "v9.json")
        with open(fake, "w") as fh:
            json.dump({"format": "match-ledger", "version": "v9",
                       "rows": []}, fh)
        with self.assertRaises(BusinessError) as ctx:
            read_ledger(fake)
        self.assertEqual(ctx.exception.code, "E_VERSION_UNSUPPORTED")

    def test_corrupt_format(self):
        bad = os.path.join(self.root, "bad.json")
        with open(bad, "w") as fh:
            json.dump({"format": "match-ledger", "version": "v1",
                       "rows": [{"image_id": "a", "status": "TP"}]}, fh)
        with self.assertRaises(BusinessError) as ctx:
            read_ledger(bad)
        self.assertEqual(ctx.exception.code, "E_FORMAT_CORRUPT")

    def test_byte_identical_rerun(self):
        p1 = self.pipeline()
        p1.run(self.cand, self.gt)
        with open(p1.v1_path, "rb") as fh:
            bytes1 = fh.read()
        other = tempfile.mkdtemp(prefix="matcher_test2_")
        self.addCleanup(shutil.rmtree, other, True)
        p2 = Pipeline(other, Config())
        p2.run(self.cand, self.gt)
        with open(p2.v1_path, "rb") as fh:
            self.assertEqual(bytes1, fh.read())


class TestMigration(WorkspaceMixin):
    def test_bidirectional_migration(self):
        p = self.pipeline()
        p.run(self.cand, self.gt)
        r1 = p.apply_update(migrate_to="v1")
        self.assertEqual(r1["migration"]["record"]["rule"],
                         "drop-v2-only-fields")
        self.assertTrue(os.path.exists(p.downgrade_log))
        r2 = p.apply_update(migrate_to="v2")
        sources = {s for _, _, s in r2["migration"]["provenance"]}
        self.assertIn("default:gen0", sources)
        self.assertIn("computed:candidate-content", sources)
        doc_v1 = read_v1(p.v1_path)
        data_v2 = read_v2(p.v2_dir)
        self.assertEqual(status_multiset(doc_v1["rows"]),
                         status_multiset(data_v2["rows"]))

    def test_migration_mismatch_detected(self):
        rows_a = [{"image_id": "i", "cand_idx": 0, "gt_idx": 0,
                   "status": "TP"}]
        rows_b = [{"image_id": "i", "cand_idx": 0, "gt_idx": 0,
                   "status": "FP"}]
        with self.assertRaises(BusinessError) as ctx:
            verify_migration(rows_a, rows_b, {}, {})
        self.assertEqual(ctx.exception.code, "E_MIGRATION_MISMATCH")


class TestInvalidation(WorkspaceMixin):
    def test_gt_revision_directed_invalidation(self):
        p = self.pipeline()
        p.run(self.cand, self.gt)
        r = p.apply_update(gt_path=self.gt2)
        self.assertEqual(r["affected_images"], ["img_0002", "img_0007"])
        self.assertEqual(r["images_reused"], 10)
        gens = p.state["generations"]
        self.assertEqual(gens["img_0002"], 1)
        self.assertEqual(gens["img_0000"], 0)

    def test_threshold_change_invalidates_all(self):
        p = self.pipeline()
        p.run(self.cand, self.gt)
        r = p.apply_update(iou_threshold=0.7)
        self.assertEqual(len(r["affected_images"]), 12)

    def test_revision_regress_rejected(self):
        p = self.pipeline()
        p.run(self.cand, self.gt)
        regress = os.path.join(self.root, "regress.jsonl")
        with open(self.gt) as fh:
            recs = [json.loads(l) for l in fh if l.strip()]
        recs[0]["revision"] = 0
        with open(regress, "w") as fh:
            for rec in recs:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
        with self.assertRaises(BusinessError) as ctx:
            p.apply_update(gt_path=regress)
        self.assertEqual(ctx.exception.code, "E_REVISION_REGRESS")
        self.assertEqual(ctx.exception.field, "revision")

    def test_recompute_limit(self):
        p = self.pipeline(max_recompute_images=1)
        p.run(self.cand, self.gt)
        with self.assertRaises(BusinessError) as ctx:
            p.apply_update(iou_threshold=0.7)
        self.assertEqual(ctx.exception.code, "E_RESOURCE_LIMIT")
        self.assertEqual(ctx.exception.field, "max_recompute_images")


class TestReplay(WorkspaceMixin):
    def test_row_level_replay(self):
        p = self.pipeline()
        p.run(self.cand, self.gt)
        data_v2 = read_v2(p.v2_dir)
        cands = {(c.image_id, c.box_idx): c
                 for c in load_candidates(self.cand, (0, 1, 2))}
        gts = {(g.image_id, g.box_idx): g
               for g in load_ground_truth(self.gt, (0, 1, 2))}
        tp = next(r for r in data_v2["rows"] if r["status"] == "TP")
        out = replay_row(tp, cands[(tp["image_id"], tp["cand_idx"])],
                         gts[(tp["image_id"], tp["gt_idx"])],
                         p.cfg.iou_threshold)
        self.assertEqual(out["status"], "TP")

    def test_replay_mismatch_detected(self):
        p = self.pipeline()
        p.run(self.cand, self.gt)
        with open(p.v1_path) as fh:
            doc = json.loads(fh.read())
        for row in doc["rows"]:
            if row["status"] == "TP":
                row["iou"] = 0.01
                break
        with open(p.v1_path, "w") as fh:
            json.dump(doc, fh)
        with self.assertRaises(BusinessError) as ctx:
            p._verify_all(_images(self.cand, self.gt))
        self.assertEqual(ctx.exception.code, "E_REPLAY_MISMATCH")

    def test_selfcheck_inconsistent(self):
        with self.assertRaises(BusinessError) as ctx:
            selfcheck([{"image_id": "i", "cand_idx": 0, "gt_idx": 0,
                        "status": "TP"}], [], {"rows": 1, "images": 1})
        self.assertEqual(ctx.exception.code, "E_INCONSISTENT")


def _images(cand_path, gt_path):
    from matcher.ingest import build_intermediate
    cands = load_candidates(cand_path, (0, 1, 2))
    gts = load_ground_truth(gt_path, (0, 1, 2))
    images, _ = build_intermediate(cands, gts, (0, 1, 2))
    return images


class TestSnapshotAndConcurrency(WorkspaceMixin):
    def test_snapshot_rollback_and_corrupt(self):
        p = self.pipeline()
        p.run(self.cand, self.gt)
        with self.assertRaises(BusinessError) as ctx:
            p.snapshots.update("ledger_v1", 0, p.v1_path)
        self.assertEqual(ctx.exception.code, "E_SNAPSHOT_ROLLBACK")
        with open(p.v1_path, "a") as fh:
            fh.write(" ")
        with self.assertRaises(BusinessError) as ctx:
            p.verify_snapshots()
        self.assertEqual(ctx.exception.code, "E_SNAPSHOT_CORRUPT")

    def test_stage_busy(self):
        p = self.pipeline()
        lock = StageLock(p.lock.lock_path)
        lock.acquire()
        try:
            with self.assertRaises(BusinessError) as ctx:
                p.run(self.cand, self.gt)
            self.assertEqual(ctx.exception.code, "E_STAGE_BUSY")
        finally:
            lock.release()

    def test_crash_recovery_skips_committed(self):
        p = self.pipeline(batch_size=4)
        with self.assertRaises(SimulatedCrash):
            p.run(self.cand, self.gt, crash_after_batch=1)
        p2 = Pipeline(self.root, Config(batch_size=4))
        report = p2.run(self.cand, self.gt)
        self.assertEqual(report["resumed_from_batch"], 1)
        self.assertEqual(report["images_recomputed"], 4)
        self.assertEqual(report["verification"]["selfcheck"]["status"],
                         "consistent")

    def test_batch_rollback_rejected(self):
        p = self.pipeline()
        p.run(self.cand, self.gt)
        with self.assertRaises(BusinessError) as ctx:
            p.committer.commit(0, {"matches": {}})
        self.assertEqual(ctx.exception.code, "E_SNAPSHOT_ROLLBACK")


class TestConfigAndLimits(WorkspaceMixin):
    def test_threshold_range(self):
        with self.assertRaises(BusinessError) as ctx:
            Config(iou_threshold=1.5).validate()
        self.assertEqual(ctx.exception.code, "E_THRESHOLD_RANGE")
        self.assertEqual(ctx.exception.field, "iou_threshold")

    def test_resident_candidate_limit(self):
        p = self.pipeline(max_resident_candidates=2, allow_degrade=False)
        with self.assertRaises(BusinessError) as ctx:
            p.run(self.cand, self.gt)
        self.assertEqual(ctx.exception.code, "E_RESOURCE_LIMIT")

    def test_degraded_mark(self):
        p = self.pipeline(max_resident_candidates=2, allow_degrade=True)
        report = p.run(self.cand, self.gt)
        self.assertIn("resident_candidates", report["degraded"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
