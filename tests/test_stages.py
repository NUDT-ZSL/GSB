from __future__ import annotations

import copy
import unittest

from ifdata.config import DEFAULT_CONFIG
from ifdata.errors import (
    BusinessError,
    E_COMPARE_BUDGET,
    E_TEMPLATE_PLACEHOLDER,
    E_WORKER_FAILURE,
)
from ifdata.stages.dedup import run_dedup
from ifdata.stages.quality import run_quality
from ifdata.stages.sampling import run_sampling
from ifdata.stages.template import run_template


def raw_sample(sample_id: str = "s0001"):
    return {
        "sample_id": sample_id,
        "instruction": "说明本地流水线的恢复方式。",
        "input": "必须从最后已提交阶段继续。",
        "output": "读取校验和，验证连续版本，然后跳过已提交阶段。",
        "source_tag": "wiki",
        "quality_flag": "gold",
        "lang": "zh",
        "annotation_revision": 1_000_000,
        "annotation_revision_raw": "1.0.0",
        "__line_no": 1,
    }


def expanded_sample(sample_id: str, canonical: str, score: float):
    return {
        "sample_id": sample_id,
        "instruction": "instruction",
        "input": "input",
        "output": "output",
        "source_tag": "wiki",
        "quality_flag": "gold",
        "lang": "zh",
        "annotation_revision": 1,
        "annotation_revision_raw": "r1",
        "__line_no": 1,
        "template_id": "tpl",
        "normalized_instruction": canonical,
        "normalized_input": "",
        "canonical_text": canonical,
        "quality_score": score,
        "quality_tier": "gold",
        "quality_contributions": {},
    }


class StageTests(unittest.TestCase):
    def test_template_missing_required_placeholder(self):
        templates = {
            "templates": [
                {
                    "template_id": "broken",
                    "lang": "zh",
                    "instruction_template": "静态指令",
                    "input_template": "输入：{input}",
                }
            ]
        }
        config = copy.deepcopy(DEFAULT_CONFIG)
        result = run_template([raw_sample()], templates, config)
        self.assertEqual(len(result["samples"]), 0)
        self.assertEqual(
            result["statuses"][0]["code"], E_TEMPLATE_PLACEHOLDER
        )
        self.assertEqual(result["completed_sample_ids"], ["s0001"])

    def test_template_unknown_placeholder(self):
        templates = {
            "templates": [
                {
                    "template_id": "broken",
                    "lang": "zh",
                    "instruction_template": "{missing} {instruction}",
                    "input_template": "{input}",
                }
            ]
        }
        config = copy.deepcopy(DEFAULT_CONFIG)
        result = run_template([raw_sample()], templates, config)
        self.assertEqual(
            result["statuses"][0]["code"], E_TEMPLATE_PLACEHOLDER
        )

    def test_quality_contributions_are_explicit_and_sum_to_score(self):
        sample = raw_sample()
        sample.update(
            {
                "template_id": "tpl",
                "normalized_instruction": "请解释恢复。",
                "normalized_input": "从提交点继续。",
                "canonical_text": "请解释恢复。 从提交点继续。",
            }
        )
        result = run_quality(
            [sample],
            copy.deepcopy(DEFAULT_CONFIG["quality"]),
            copy.deepcopy(DEFAULT_CONFIG),
        )
        scored = result["samples"][0]
        contributions = scored["quality_contributions"]
        self.assertEqual(
            set(contributions), {"length", "repetition", "noise"}
        )
        total = round(
            sum(
                item["contribution"]
                for item in contributions.values()
            ),
            6,
        )
        self.assertEqual(total, scored["quality_score"])
        for item in contributions.values():
            self.assertIn("weight", item)
            self.assertIn("component_score", item)
            self.assertIn("metrics", item)

    def test_dedup_clusters_cover_every_sample_once(self):
        samples = [
            expanded_sample("s1", "完全相同 的 规范 文本 用于 去重", 0.9),
            expanded_sample("s2", "完全相同 的 规范 文本 用于 去重", 0.8),
            expanded_sample("s3", "另一个 独立 的 规范 文本 内容", 0.7),
        ]
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["dedup"]["threshold"] = 0.7
        config["dedup"]["candidate_windows"] = [4]
        result = run_dedup(samples, config)
        self.assertEqual(len(result["samples"]), 2)
        self.assertEqual(len(result["removed_samples"]), 1)
        self.assertTrue(
            result["summary"]["all_samples_in_exactly_one_cluster"]
        )
        removed = result["removed_samples"][0]
        self.assertEqual(removed["sample_id"], "s2")
        self.assertEqual(removed["similar_neighbor_sample_id"], "s1")
        self.assertTrue(removed["hit_blocks"])
        self.assertGreaterEqual(removed["threshold_ratio"], 1.0)
        cluster_members = {
            member["sample_id"]
            for cluster in result["clusters"]
            for member in cluster["members"]
        }
        self.assertEqual(cluster_members, {"s1", "s2", "s3"})

    def test_dedup_budget_rejection_and_worker_failure(self):
        samples = [
            expanded_sample("s1", "完全相同 的 规范 文本 用于 去重", 0.9),
            expanded_sample("s2", "完全相同 的 规范 文本 用于 去重", 0.8),
        ]
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["dedup"]["candidate_windows"] = [4]
        config["runtime"]["compare_budget"] = 0
        with self.assertRaises(BusinessError) as context:
            run_dedup(samples, config)
        self.assertEqual(context.exception.code, E_COMPARE_BUDGET)
        config["runtime"]["compare_budget"] = 10
        with self.assertRaises(BusinessError) as context:
            run_dedup(samples, config, failpoints={"dedup:1": "boom"})
        self.assertEqual(context.exception.code, E_WORKER_FAILURE)
        self.assertEqual(context.exception.batch_id, "dedup-batch-0001")

    def test_sampling_seed_total_cap_and_relaxation(self):
        samples = [
            expanded_sample("s1", "a", 0.9),
            expanded_sample("s2", "b", 0.9),
            expanded_sample("s3", "c", 0.8),
            expanded_sample("s4", "d", 0.6),
        ]
        for index, sample in enumerate(samples):
            sample["source_tag"] = "wiki"
            sample["quality_tier"] = (
                ["gold", "gold", "silver", "bronze"][index]
            )
            sample["cluster_id"] = f"c{index}"
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["sampling"] = {
            "seed": 99,
            "total_target": 3,
            "allow_quality_relaxation": True,
            "max_tier_drop": 1,
            "tier_order": ["gold", "silver", "bronze"],
            "targets": [
                {"source_tag": "wiki", "quality_tier": "gold", "quota": 3}
            ],
        }
        config["priorities"] = [
            "progress",
            "memory",
            "sampling_target",
            "dedup_quality",
        ]
        first = run_sampling(samples, config)
        second = run_sampling(samples, config)
        self.assertEqual(
            [item["sample"]["sample_id"] for item in first["selected"]],
            [item["sample"]["sample_id"] for item in second["selected"]],
        )
        self.assertEqual(first["summary"]["total_selected"], 3)
        self.assertEqual(first["summary"]["total_shortfall"], 0)
        self.assertTrue(
            any(item["relaxed"] for item in first["selected"])
        )
        self.assertEqual(
            first["summary"]["seed"],
            99,
        )
        config["priorities"] = [
            "progress",
            "memory",
            "dedup_quality",
            "sampling_target",
        ]
        denied = run_sampling(samples, config)
        self.assertEqual(denied["summary"]["total_selected"], 2)
        self.assertTrue(
            any(
                item["status"] == "rejected"
                for item in denied["arbitration"]["decisions"]
            )
        )


if __name__ == "__main__":
    unittest.main()

