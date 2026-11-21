from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest

from igeval import EvaluatorConfig, QualityEvaluator, build_demo_samples
from igeval.pipeline import run_pipeline
from igeval.samples import render_image
from igeval.snapshot import make_snapshot
from igeval.util import canonical_json
from igeval.validation import prepare_sample


class DeterminismTests(unittest.TestCase):
    def test_renderer_and_pipeline_are_byte_stable(self) -> None:
        sample = build_demo_samples(1)[0]
        first_image = render_image(sample.seed, sample.width, sample.height)
        second_image = render_image(sample.seed, sample.width, sample.height)
        self.assertEqual(first_image.tobytes(), second_image.tobytes())

        config = EvaluatorConfig(workers=1)
        snapshot = make_snapshot()
        prepared = prepare_sample(sample, config)
        first = run_pipeline(prepared, config, snapshot)
        second = run_pipeline(prepared, config, snapshot)
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(first["score_digest"], second["score_digest"])

        with tempfile.TemporaryDirectory() as runtime:
            report = QualityEvaluator(
                [sample], config=config, snapshot=snapshot, runtime_dir=runtime
            ).evaluate_batch()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["results"][0]["score_digest"], first["score_digest"])

    def test_batch_query_and_cache_share_the_same_scores(self) -> None:
        samples = build_demo_samples(6)
        config = EvaluatorConfig(workers=1, latency_budget_ms=1_000.0)
        with tempfile.TemporaryDirectory() as runtime:
            evaluator = QualityEvaluator(samples, config=config, runtime_dir=runtime)
            batch = evaluator.evaluate_batch()
            self.assertEqual(batch["status"], "ok")
            by_id = {item["sample_id"]: item for item in batch["results"]}

            for sample_id, expected in by_id.items():
                cold = evaluator.query(sample_id, use_cache=False)
                warm = evaluator.query(sample_id, use_cache=True)
                hit = evaluator.query(sample_id, use_cache=True)
                self.assertEqual(cold["status"], "ok")
                self.assertEqual(warm["status"], "ok")
                self.assertEqual(hit["status"], "ok")
                self.assertIn(warm["cache"]["status"], {"stored", "hit"})
                self.assertEqual(hit["cache"]["status"], "hit")
                self.assertEqual(expected["score_digest"], cold["score_digest"])
                self.assertEqual(cold["score_digest"], warm["score_digest"])
                self.assertEqual(warm["score_digest"], hit["score_digest"])
                for stage in ("preprocess", "semantic", "structure", "quality"):
                    self.assertEqual(
                        expected["stages"][stage]["score"],
                        hit["stages"][stage]["score"],
                    )

            check = evaluator.self_check(batch)
            self.assertEqual(check["status"], "ok", check)
            self.assertEqual(check["code"], None)

    def test_ranking_ties_use_ascending_sample_id(self) -> None:
        base = build_demo_samples(1)[0]
        left = dataclasses.replace(base, sample_id="tie-z")
        right = dataclasses.replace(base, sample_id="tie-a")
        config = EvaluatorConfig(workers=1)
        with tempfile.TemporaryDirectory() as runtime:
            report = QualityEvaluator(
                [left, right], config=config, runtime_dir=runtime
            ).evaluate_batch()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["ranking"][0]["sample_id"], "tie-a")
        self.assertEqual(report["ranking"][1]["sample_id"], "tie-z")
        self.assertEqual(report["results"][0]["rank"], 1)
        self.assertEqual(report["results"][1]["rank"], 2)

    def test_self_check_reports_first_mismatching_stage(self) -> None:
        with tempfile.TemporaryDirectory() as runtime:
            evaluator = QualityEvaluator(
                build_demo_samples(2),
                config=EvaluatorConfig(workers=1, latency_budget_ms=1_000.0),
                runtime_dir=runtime,
            )
            batch = evaluator.evaluate_batch()
        tampered = json.loads(json.dumps(batch))
        target = tampered["results"][0]
        target["stages"]["preprocess"]["score"] = 0.123456789
        target["stages"]["semantic"]["score"] = 0.223456789
        check = evaluator.self_check(tampered)
        self.assertEqual(check["status"], "error")
        self.assertEqual(check["code"], "E_INCONSISTENT")
        self.assertEqual(check["category"], "score_mismatch")
        self.assertEqual(check["stage"], "preprocess")
        self.assertEqual(check["sample_id"], target["sample_id"])


if __name__ == "__main__":
    unittest.main()

