from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import yaml

from ctr_pipeline.config import load_config
from ctr_pipeline.errors import (
    E_BUDGET_EXHAUSTED,
    E_CONFIG_MISMATCH,
    E_GROUP_UNKNOWN,
    E_JSON_INVALID,
    E_REQ_EMPTY,
    E_REQ_QUOTA_EXCEEDED,
    E_WIDTH_INVALID,
    PipelineError,
)
from ctr_pipeline.loader import load_all, load_params
from ctr_pipeline.models import Candidate, InjectionSpec, RequestData
from ctr_pipeline.runner import run_pipeline
from ctr_pipeline.scheduler import plan_budget
from ctr_pipeline.stages import compute_request


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def sample_paths() -> dict[str, Path]:
    return {
        "config": SAMPLES / "config.yaml",
        "candidates": SAMPLES / "candidates.jsonl",
        "params": SAMPLES / "params.json",
        "injections": SAMPLES / "injections.json",
    }


def run_sample(tmp: Path, name: str, concurrency: int, **kwargs) -> tuple[dict, Path]:
    state = tmp / f"{name}_state"
    report_path = tmp / f"{name}_report.json"
    outcome = run_pipeline(
        config_path=SAMPLES / "config.yaml",
        candidates_path=SAMPLES / "candidates.jsonl",
        params_path=SAMPLES / "params.json",
        state_dir=state,
        report_path=report_path,
        concurrency_override=concurrency,
        **kwargs,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return report, state


class PipelineTests(unittest.TestCase):
    def test_loader_business_codes_and_global_validation(self) -> None:
        paths = sample_paths()
        cfg = load_config(paths["config"])
        params, failure = load_params(paths["params"], cfg)
        self.assertIsNone(failure)
        assert params is not None

        with tempfile.TemporaryDirectory() as temp_raw:
            temp = Path(temp_raw)
            candidates = temp / "bad.jsonl"
            candidates.write_text(
                '{"request_id":"","item_id":"x","recall_scores":[1,2],"dense_features":[0,0],"sparse_counts":[1]}\n'
                '{"request_id":"req-empty","candidates":[]}\n'
                '{"request_id":"req-bad"\n',
                encoding="utf-8",
            )
            load_result, _ = load_all(cfg, candidates, paths["params"])
            codes = {item.code for item in load_result.failures}
            self.assertEqual(load_result.requests, {})
            self.assertIn(E_REQ_EMPTY, codes)
            self.assertIn(E_JSON_INVALID, codes)

            raw_params = json.loads(paths["params"].read_text(encoding="utf-8"))
            raw_params["feature_groups"]["mystery"] = [0]
            bad_params = temp / "bad_params.json"
            bad_params.write_text(json.dumps(raw_params), encoding="utf-8")
            _, group_failure = load_params(bad_params, cfg)
            self.assertIsNotNone(group_failure)
            self.assertEqual(group_failure.code, E_GROUP_UNKNOWN)

            raw_config = yaml.safe_load(paths["config"].read_text(encoding="utf-8"))
            raw_config["fixed_point"]["width"] = 99
            raw_config["fixed_point"]["levels"] = 10**99
            bad_config = temp / "bad_config.yaml"
            bad_config.write_text(yaml.safe_dump(raw_config), encoding="utf-8")
            with self.assertRaises(PipelineError) as raised:
                load_config(bad_config)
            self.assertEqual(raised.exception.code, E_WIDTH_INVALID)

    def test_deterministic_zero_minmax_and_weight_contribution(self) -> None:
        paths = sample_paths()
        cfg = load_config(paths["config"])
        load_result, _ = load_all(cfg, paths["candidates"], paths["params"])
        assert load_result.params is not None
        request = load_result.requests["req-001"]
        first = compute_request(request, cfg, load_result.params, [])
        second = compute_request(request, cfg, load_result.params, [])
        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertEqual(first.request_report, second.request_report)

        modified_raw = copy.deepcopy(load_result.params.raw)
        modified_raw["feature_weights"][2] += 0.25
        modified = replace(
            load_result.params,
            raw=modified_raw,
            feature_weights=tuple(float(v) for v in modified_raw["feature_weights"]),
        )
        changed = compute_request(request, cfg, modified, [])
        self.assertTrue(changed.success)
        assert first.request_report is not None and changed.request_report is not None
        original_contributions = first.request_report["width_results"]["configured"]["candidates"][0][
            "group_contributions"
        ]
        changed_contributions = changed.request_report["width_results"]["configured"]["candidates"][0][
            "group_contributions"
        ]
        self.assertNotEqual(original_contributions, changed_contributions)

        candidates = (
            Candidate("req-zero", "a", (0.5, 0.5), (0.5, 2.0), (1,), 1),
            Candidate("req-zero", "b", (0.5, 0.5), (0.5, 2.0), (1,), 2),
        )
        zero_request = RequestData("req-zero", candidates, (1, 2))
        zero_outcome = compute_request(zero_request, cfg, load_result.params, [])
        self.assertTrue(zero_outcome.success)
        normalize = zero_outcome.stage_artifacts["normalize"]
        for stat in normalize["stats"]:
            self.assertTrue(stat["denominator_zero"])
            self.assertEqual(stat["denominator_zero_candidates"], ["a", "b"])
        for candidate in normalize["candidates"]:
            self.assertEqual(candidate["normalized_features"], [0.0, 0.0, 0.0, 0.0, 0.0])
            self.assertEqual(candidate["zero_denominator_features"], [0, 1, 2, 3, 4])

    def test_budget_and_quota_plan_are_stable(self) -> None:
        paths = sample_paths()
        cfg = load_config(paths["config"])
        load_result, _ = load_all(cfg, paths["candidates"], paths["params"])
        requests = load_result.requests
        plan = plan_budget(requests, replace(cfg, global_budget=52, per_request_quota=7))
        by_id = {item.request_id: item for item in plan.plans}
        self.assertEqual(by_id["req-003"].failure.code, E_REQ_QUOTA_EXCEEDED)
        self.assertEqual(by_id["req-013"].failure.code, E_BUDGET_EXHAUSTED)
        self.assertEqual(by_id["req-014"].failure.code, E_BUDGET_EXHAUSTED)
        self.assertEqual(by_id["req-013"].ready_queue_position, 13)
        self.assertEqual(by_id["req-014"].ready_queue_position, 14)
        self.assertEqual(plan.consumed, 52)
        self.assertEqual(plan.remaining, 0)
        self.assertEqual(
            [event["sequence"] for event in plan.curve], list(range(1, 53))
        )
        again = plan_budget(requests, replace(cfg, global_budget=52, per_request_quota=7))
        self.assertEqual(plan.curve, again.curve)

    def test_serial_and_parallel_reports_are_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temp_raw:
            temp = Path(temp_raw)
            serial, serial_state = run_sample(temp, "serial", 1)
            parallel, parallel_state = run_sample(temp, "parallel", 4)
            self.assertEqual(serial["status"], "completed")
            self.assertEqual(parallel["status"], "completed")
            self.assertEqual(serial["determinism_digest"], parallel["determinism_digest"])
            self.assertEqual(serial["requests"], parallel["requests"])
            self.assertEqual(serial["failure_list"], parallel["failure_list"])
            self.assertEqual(serial["budget"], parallel["budget"])
            self.assertEqual(serial["stage_versions"], parallel["stage_versions"])
            self.assertGreaterEqual(
                serial["error_trace_summary"]["boundary_sensitive_sample_count"], 1
            )
            self.assertTrue(any(item["code"] == E_REQ_QUOTA_EXCEEDED for item in serial["failure_list"]))
            self.assertTrue(any(item["code"] == E_BUDGET_EXHAUSTED for item in serial["failure_list"]))
            self.assertTrue(all(item["hit"] for item in serial["injections"]))
            for item in serial["requests"]:
                configured = item["width_results"]["configured"]
                high = item["width_results"]["high"]
                self.assertEqual(configured["width"], 3)
                self.assertEqual(high["width"], 4)
                self.assertEqual(item["ranking"], configured["ranking"])
            self.assertFalse(any(serial_state.rglob("*.tmp")))
            self.assertFalse(any(parallel_state.rglob("*.tmp")))

    def test_graceful_exit_resume_and_config_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_raw:
            temp = Path(temp_raw)
            reference, _ = run_sample(temp, "reference", 4)
            interrupted, state = run_sample(temp, "graceful", 4, exit_after_requests=3)
            self.assertEqual(interrupted["status"], "interrupted")
            self.assertEqual(interrupted["snapshot_version"], 3)
            resumed = run_pipeline(
                config_path=SAMPLES / "config.yaml",
                candidates_path=SAMPLES / "candidates.jsonl",
                params_path=SAMPLES / "params.json",
                state_dir=state,
                report_path=temp / "resumed.json",
                concurrency_override=4,
            )
            self.assertEqual(resumed.status, "completed")
            resumed_report = json.loads((temp / "resumed.json").read_text(encoding="utf-8"))
            self.assertEqual(resumed_report["determinism_digest"], reference["determinism_digest"])

            raw_config = yaml.safe_load((SAMPLES / "config.yaml").read_text(encoding="utf-8"))
            raw_config["fusion"]["temperature"] = 0.71
            mismatch_config = temp / "mismatch.yaml"
            mismatch_config.write_text(yaml.safe_dump(raw_config), encoding="utf-8")
            mismatch = run_pipeline(
                config_path=mismatch_config,
                candidates_path=SAMPLES / "candidates.jsonl",
                params_path=SAMPLES / "params.json",
                state_dir=state,
                report_path=temp / "mismatch.json",
                concurrency_override=4,
            )
            self.assertEqual(mismatch.exit_code, 2)
            mismatch_report = json.loads((temp / "mismatch.json").read_text(encoding="utf-8"))
            self.assertEqual(mismatch_report["status"], "rejected")
            self.assertEqual(mismatch_report["failure_list"][0]["code"], E_CONFIG_MISMATCH)


if __name__ == "__main__":
    unittest.main()
