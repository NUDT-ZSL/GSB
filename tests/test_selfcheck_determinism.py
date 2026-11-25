"""跨模块一致性自检与样本生成确定性。"""

from __future__ import annotations
import json

from sr_pipeline.errors import BusinessError, ErrorCode
from sr_pipeline.samplegen import generate_samples
from sr_pipeline.selfcheck import check_invalidation_coverage

from ._base import SampleCase


class SelfCheckTest(SampleCase):
    def test_invalidation_coverage_partial(self) -> None:
        affected = {"q1", "q2", "q3"}
        check_invalidation_coverage(
            affected, recomputed={"q1", "q2", "q3"}, reused={"q4", "q5"}
        )

    def test_invalidation_overlap_rejected(self) -> None:
        with self.assertRaises(BusinessError) as caught:
            check_invalidation_coverage(
                {"q1"}, recomputed={"q1"}, reused={"q1"}
            )
        self.assertEqual(caught.exception.code, ErrorCode.SELF_CHECK_FAILED)

    def test_invalidation_missing_rejected(self) -> None:
        with self.assertRaises(BusinessError) as caught:
            check_invalidation_coverage(
                {"q1", "q2"}, recomputed={"q1"}, reused=set()
            )
        self.assertEqual(caught.exception.code, ErrorCode.SELF_CHECK_FAILED)


class SampleDeterminismTest(SampleCase):
    def test_generation_byte_stable(self) -> None:
        manifest_first = generate_samples(self.sample_dir / "a")
        manifest_second = generate_samples(self.sample_dir / "b")
        for name in ("docs", "queries", "term_weights", "recall_config"):
            file_name = manifest_first["files"][name]["path"]
            left = (self.sample_dir / "a" / file_name).read_bytes()
            right = (self.sample_dir / "b" / file_name).read_bytes()
            self.assertEqual(left, right)
        self.assertEqual(
            manifest_first["files"]["docs"]["sha256"],
            manifest_second["files"]["docs"]["sha256"],
        )

    def test_manifest_self_describes(self) -> None:
        manifest = json.loads(
            (self.sample_dir / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["generator"], "sr_pipeline.samplegen")
        self.assertEqual(manifest["params"]["seed"], 20261126)
