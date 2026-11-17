"""Determinism and business-rule tests for the evidence pipeline."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline import injection as inj_mod
from pipeline.config import load_fusion_config
from pipeline.engine import PipelineEngine
from pipeline.errors import (E_CONFIG_MISMATCH, E_IMAGE_TRUNCATED,
                             E_NON_FINITE_FEATURE, E_RELATION_DANGLING,
                             E_SAMPLE_INVALID, PipelineError)
from pipeline.fusion import fuse_round
from pipeline.hashing import canonical_json, content_hash
from pipeline.samples import (generate_assertions, generate_image,
                              generate_sample_tree, load_sample)
from pipeline.textrel import (align_relations, load_relation_table,
                              text_features, validate_relations)
from pipeline.vision import quantize_features, visual_pipeline

CONFIG = os.path.join(ROOT, "configs", "fusion_config.json")
SERIAL_CONFIG = os.path.join(ROOT, "configs", "fusion_config_serial.json")
VARIANT_CONFIG = os.path.join(ROOT,
                              "configs", "fusion_config_quant4_temp05.json")
RELATIONS = os.path.join(ROOT, "configs", "relations.json")
INJECTIONS = os.path.join(ROOT, "configs", "injections.json")
EMPTY_INJECTIONS = os.path.join(ROOT, "configs", "injections_empty.json")


class FixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pipeline-test-")
        cls.input_dir = os.path.join(cls.tmp, "samples")
        cls.config = load_fusion_config(CONFIG)
        cls.names = generate_sample_tree(
            cls.input_dir, 8, cls.config.image_width,
            cls.config.image_height, cls.config.rng_seed,
            cls.config.grid_rows, cls.config.grid_cols)
        cls.relations = load_relation_table(RELATIONS)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _engine(self, injections, work_dir):
        rel_hash = content_hash([r.to_dict() for r in self.relations])
        injs = inj_mod.load_injection_config(injections)
        inj_hash = content_hash([i.to_dict() for i in injs])
        return PipelineEngine(self.config, self.relations, injs, work_dir,
                              rel_hash, inj_hash)

    def _run(self, injection_path, work_dir, config_path=CONFIG):
        cfg = load_fusion_config(config_path)
        rels = load_relation_table(RELATIONS)
        injs = inj_mod.load_injection_config(injection_path)
        engine = PipelineEngine(
            cfg, rels, injs, work_dir,
            content_hash([r.to_dict() for r in rels]),
            content_hash([i.to_dict() for i in injs]))
        report, version = engine.run(self.input_dir)
        return report, version

    # ------------------------------------------------------------------
    def test_sample_generation_is_deterministic(self):
        png_a = generate_image(0, 64, 64, self.config.rng_seed)
        png_b = generate_image(0, 64, 64, self.config.rng_seed)
        self.assertEqual(png_a, png_b)
        doc_a = generate_assertions("sample-000", 0, 4, self.config.rng_seed)
        doc_b = generate_assertions("sample-000", 0, 4, self.config.rng_seed)
        self.assertEqual(canonical_json(doc_a), canonical_json(doc_b))

    def test_visual_features_deterministic_and_quantization_sensitive(self):
        sample = load_sample(os.path.join(self.input_dir, self.names[0]),
                             64, 64)
        first = visual_pipeline(sample.image, self.config)
        second = visual_pipeline(sample.image, self.config)
        self.assertTrue(np.array_equal(first, second))
        variant = load_fusion_config(VARIANT_CONFIG)
        coarse = visual_pipeline(sample.image, variant)
        self.assertFalse(np.allclose(first, coarse))
        unique = sorted(np.unique(coarse).tolist())
        self.assertEqual(len(unique), 4)
        self.assertTrue(np.allclose(unique, [0.0, 1 / 3, 2 / 3, 1.0]))
        with self.assertRaises(ValueError):
            quantize_features(first, 1)

    def test_invalid_png_header_and_size(self):
        bad_dir = os.path.join(self.tmp, "bad-header")
        good_dir = os.path.join(self.input_dir, self.names[1])
        shutil.copytree(good_dir, bad_dir)
        with open(os.path.join(bad_dir, "image.png"), "wb") as handle:
            handle.write(b"NOTPNG" + b"\x00" * 32)
        with self.assertRaises(PipelineError) as ctx:
            load_sample(bad_dir, 64, 64)
        self.assertEqual(ctx.exception.code, E_SAMPLE_INVALID)

    def test_missing_assertion_file(self):
        bad_dir = os.path.join(self.tmp, "missing-json")
        shutil.copytree(os.path.join(self.input_dir, self.names[0]), bad_dir)
        os.unlink(os.path.join(bad_dir, "assertions.json"))
        with self.assertRaises(PipelineError) as ctx:
            load_sample(bad_dir, 64, 64)
        self.assertEqual(ctx.exception.code, E_SAMPLE_INVALID)

    def test_static_dangling_relations_are_reported_not_associated(self):
        sample = load_sample(os.path.join(self.input_dir, self.names[0]),
                             64, 64)
        ids = {a["assertion_id"] for a in sample.assertions["assertions"]}
        dangling = validate_relations(self.relations, ids,
                                      self.config.region_count)
        ids_dangling = {d["relation_id"] for d in dangling}
        self.assertIn("rel-900", ids_dangling)
        self.assertIn("rel-901", ids_dangling)
        usable, reported = align_relations(self.relations, ids,
                                           self.config.region_count)
        self.assertEqual({d["relation_id"] for d in reported}, ids_dangling)
        self.assertTrue(all(r.relation_id not in ids_dangling for r in usable))
        with self.assertRaises(PipelineError) as ctx:
            align_relations(self.relations, ids,
                            self.config.region_count, strict=True)
        self.assertEqual(ctx.exception.code, E_RELATION_DANGLING)

    def test_text_features_are_stable(self):
        sample = load_sample(os.path.join(self.input_dir, self.names[0]),
                             64, 64)
        first = text_features(sample.assertions, self.config.feature_dim)
        second = text_features(sample.assertions, self.config.feature_dim)
        self.assertEqual(set(first), set(second))
        for key in first:
            self.assertTrue(np.array_equal(first[key], second[key]))

    def test_non_finite_feature_rejected_at_fusion(self):
        sample = load_sample(os.path.join(self.input_dir, self.names[0]),
                             64, 64)
        visual = visual_pipeline(sample.image, self.config)
        visual[0, 0] = float("inf")
        text = text_features(sample.assertions, self.config.feature_dim)
        with self.assertRaises(PipelineError) as ctx:
            fuse_round(visual, text, self.relations[:3], self.config, 0)
        self.assertEqual(ctx.exception.code, E_NON_FINITE_FEATURE)

    def test_failure_isolation_and_injection_codes(self):
        report, version = self._run(INJECTIONS,
                                    os.path.join(self.tmp, "inj"))
        codes = {(f["sample"], f["code"]) for f in report["failures"]
                 if f["sample"]}
        self.assertIn(("sample-001", E_IMAGE_TRUNCATED), codes)
        self.assertIn(("sample-004", E_NON_FINITE_FEATURE), codes)
        self.assertIn(("sample-005", E_RELATION_DANGLING), codes)
        hit_ids = {h["injection_id"] for h in report["injection_hits"]}
        self.assertEqual(
            hit_ids, {"INJ-001", "INJ-002", "INJ-003",
                      "INJ-004", "INJ-005", "INJ-006"})
        failed_samples = {f["sample"] for f in report["failures"]
                          if f["sample"]}
        for item in report["samples"]:
            self.assertNotIn(item["sample"], failed_samples)
        self.assertEqual(version, self.config.max_rounds)

    def test_concurrency_byte_identical(self):
        parallel, _ = self._run(EMPTY_INJECTIONS,
                                os.path.join(self.tmp, "par"))
        serial, _ = self._run(EMPTY_INJECTIONS,
                              os.path.join(self.tmp, "ser"),
                              config_path=SERIAL_CONFIG)
        self.assertEqual(canonical_json(parallel), canonical_json(serial))

    def test_resume_byte_identical(self):
        full, _ = self._run(INJECTIONS, os.path.join(self.tmp, "full"))
        work = os.path.join(self.tmp, "resume")
        os.environ["PIPELINE_INTERRUPT_AFTER_ROUND"] = "1"
        try:
            from pipeline.cli import main
            exit_code = main(["run", "--input-dir", self.input_dir,
                              "--config", CONFIG, "--relations", RELATIONS,
                              "--injections", INJECTIONS, "--work-dir", work,
                              "--report",
                              os.path.join(self.tmp, "partial.json")])
            self.assertEqual(exit_code, 0)
        finally:
            os.environ.pop("PIPELINE_INTERRUPT_AFTER_ROUND", None)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "partial.json")))
        resumed, _ = self._run(INJECTIONS, work)
        self.assertEqual(canonical_json(full), canonical_json(resumed))

    def test_config_mismatch_on_resume(self):
        work = os.path.join(self.tmp, "mismatch")
        self._run(EMPTY_INJECTIONS, work)
        rels = load_relation_table(RELATIONS)
        injs = inj_mod.load_injection_config(EMPTY_INJECTIONS)
        variant = load_fusion_config(VARIANT_CONFIG)
        engine = PipelineEngine(
            variant, rels, injs, work,
            content_hash([r.to_dict() for r in rels]),
            content_hash([i.to_dict() for i in injs]))
        with self.assertRaises(PipelineError) as ctx:
            engine.run(self.input_dir)
        self.assertEqual(ctx.exception.code, E_CONFIG_MISMATCH)


if __name__ == "__main__":
    unittest.main(verbosity=2)
