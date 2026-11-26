from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from prosody_diag.adjudicate import (
    enforce_conflict_threshold,
    index_boundary_truth,
    judge_candidate,
    judgments_to_ledger_rows,
)
from prosody_diag.audio import extract_acoustic_record, rank_candidates
from prosody_diag.canonical import canonical_hash, read_json, read_jsonl
from prosody_diag.config import Config
from prosody_diag.errors import DiagnosticError
from prosody_diag.errorspec import (
    build_frame_deviation,
    check_tolerances,
    summarize_error_spectrum,
    summarize_truth_observation_details,
)
from prosody_diag.pipeline import DiagnosticPipeline
from prosody_diag.precision import PrecisionContext
from prosody_diag.samplegen import generate_sample_workspace
from prosody_diag.state import StateStore
from prosody_diag.trees import (
    flatten_condition_tree,
    infer_candidate,
    load_decision_tree,
    migrate_v1_to_v2,
    migrate_v2_to_v1,
    parse_v1_tree,
    write_builtin_trees,
    write_v2_package,
)
from prosody_diag.validation import (
    load_tasks,
    validate_boundary_references,
    validate_revision_watermark,
    validate_task_records,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class DiagnosticTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix="prosody-test-")
        cls.root = Path(cls.temp.name)
        cls.sample = cls.root / "sample"
        cls.cfg = Config()
        generate_sample_workspace(cls.sample, cls.cfg)
        write_builtin_trees(cls.sample, cls.cfg)
        cls.tasks = load_tasks(cls.sample)
        cls.truth = read_jsonl(cls.sample / "frame_truth.jsonl")
        cls.boundary_truth = read_jsonl(cls.sample / "boundary_truth.jsonl")
        cls.truth_by_utterance: dict[str, list[dict]] = {}
        for row in cls.truth:
            cls.truth_by_utterance.setdefault(row["utterance_id"], []).append(row)
        cls.v1 = load_decision_tree(cls.sample / "trees" / "v1" / "prosody_tree_v1.json")
        cls.v2 = load_decision_tree(cls.sample / "trees" / "v2")
        cls.trees = {"v1": cls.v1, "v2": cls.v2}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def acoustic_records(self) -> list[dict]:
        return [
            extract_acoustic_record(
                task,
                self.sample / "audio" / f"{task['speaker_id']}.pcm",
                self.truth_by_utterance[task["utterance_id"]],
                self.cfg,
            )
            for task in self.tasks
        ]


class DeterministicAcousticTests(DiagnosticTestBase):
    def test_sample_generation_is_byte_reproducible(self) -> None:
        second = self.root / "sample-second"
        generate_sample_workspace(second, self.cfg)
        for relative in ("tasks.jsonl", "frame_truth.jsonl", "boundary_truth.jsonl"):
            self.assertEqual(
                (self.sample / relative).read_bytes(),
                (second / relative).read_bytes(),
                relative,
            )
        for speaker in ("SPK01", "SPK02", "SPK03"):
            self.assertEqual(
                (self.sample / "audio" / f"{speaker}.pcm").read_bytes(),
                (second / "audio" / f"{speaker}.pcm").read_bytes(),
            )

    def test_exact_frame_counts_and_extraction_reproducibility(self) -> None:
        for task in self.tasks:
            record = extract_acoustic_record(
                task,
                self.sample / "audio" / f"{task['speaker_id']}.pcm",
                self.truth_by_utterance[task["utterance_id"]],
                self.cfg,
            )
            expected_samples = (task["end_ms"] - task["start_ms"]) * self.cfg.sample_rate // 1000
            self.assertEqual(record["sample_count"], expected_samples)
            self.assertEqual(
                record["frame_count"],
                1 + (expected_samples - self.cfg.frame_length_samples) // self.cfg.frame_shift_samples,
            )
            self.assertEqual(record["frame_count"], len(self.truth_by_utterance[task["utterance_id"]]))
            again = extract_acoustic_record(
                task,
                self.sample / "audio" / f"{task['speaker_id']}.pcm",
                self.truth_by_utterance[task["utterance_id"]],
                self.cfg,
            )
            self.assertEqual(canonical_hash(record), canonical_hash(again))

    def test_frame_count_mismatch_code(self) -> None:
        task = self.tasks[0]
        with self.assertRaises(DiagnosticError) as ctx:
            extract_acoustic_record(
                task,
                self.sample / "audio" / f"{task['speaker_id']}.pcm",
                [],
                self.cfg,
            )
        self.assertEqual(ctx.exception.code, "E_FRAME_COUNT_MISMATCH")

    def test_tie_break_order(self) -> None:
        candidates = [
            {"score": 0.5, "frame_index": 9, "boundary_type": "z"},
            {"score": 0.9, "frame_index": 5, "boundary_type": "z"},
            {"score": 0.9, "frame_index": 4, "boundary_type": "pause_offset"},
            {"score": 0.9, "frame_index": 4, "boundary_type": "f0_reset"},
        ]
        ranked = rank_candidates(candidates)
        self.assertEqual(
            [(item["frame_index"], item["boundary_type"]) for item in ranked],
            [(4, "f0_reset"), (4, "pause_offset"), (5, "z"), (9, "z")],
        )


class ValidationAndFormatTests(DiagnosticTestBase):
    def test_duplicate_utterance_and_bad_profiles(self) -> None:
        duplicate = [dict(self.tasks[0]), dict(self.tasks[0])]
        with self.assertRaises(DiagnosticError) as ctx:
            validate_task_records(duplicate)
        self.assertEqual(ctx.exception.code, "E_DUPLICATE_UTTERANCE_ID")

        bad_profile = [dict(self.tasks[0], precision_profile="nope")]
        with self.assertRaises(DiagnosticError) as ctx:
            validate_task_records(bad_profile)
        self.assertEqual(ctx.exception.code, "E_PRECISION_PROFILE_UNKNOWN")

        with self.assertRaises(DiagnosticError) as ctx:
            Config(frame_length_ms=0).validate()
        self.assertEqual(ctx.exception.code, "E_INVALID_FRAME_CONFIG")

    def test_revision_rollback_and_reference_mismatch(self) -> None:
        with self.assertRaises(DiagnosticError) as ctx:
            validate_revision_watermark(
                self.tasks, {"decision_tree_rev": 99, "logical_clock_ms": 10**20}
            )
        self.assertEqual(ctx.exception.code, "E_TREE_VERSION_ROLLBACK")

        records = self.acoustic_records()
        bad_reference = [dict(self.boundary_truth[0], frame_index=9999)]
        with self.assertRaises(DiagnosticError) as ctx:
            validate_boundary_references(records, bad_reference)
        self.assertEqual(ctx.exception.code, "E_REFERENCE_MISMATCH")

    def test_unknown_version_and_corrupt_formats(self) -> None:
        unknown = self.root / "unknown_tree.json"
        unknown.write_text(json.dumps({"version": "9.9"}), encoding="utf-8")
        with self.assertRaises(DiagnosticError) as ctx:
            load_decision_tree(unknown)
        self.assertEqual(ctx.exception.code, "E_VERSION_UNSUPPORTED")

        with self.assertRaises(DiagnosticError) as ctx:
            parse_v1_tree({"format": "x", "version": "1.0"})
        self.assertEqual(ctx.exception.code, "E_FORMAT_CORRUPT")

        bad_lines = {
            "tree_id": "bad",
            "version": "1.0",
            "rule_lines": ["R001|0|pause_norm|>=|0.2|B1"],
        }
        with self.assertRaises(DiagnosticError) as ctx:
            parse_v1_tree(bad_lines)
        self.assertEqual(ctx.exception.code, "E_FORMAT_CORRUPT")

        regressed = {
            "tree_id": "bad-time",
            "version": "1.0",
            "rule_lines": [
                "R001|1.0|pause_norm|>=|0.2|B1|0.8",
                "R002|0.5|pause_norm|>=|0.3|B2|0.9",
            ],
        }
        with self.assertRaises(DiagnosticError) as ctx:
            parse_v1_tree(regressed)
        self.assertEqual(ctx.exception.code, "E_FORMAT_CORRUPT")

    def test_v2_timestamp_regression_is_corrupt(self) -> None:
        package = self.root / "bad-v2"
        manifest = {
            "format": "prosody-decision-tree-shards",
            "version": "2.0",
            "tree_id": "bad-v2",
            "shards": ["s0.json"],
            "sqlite": "meta/tree.db",
        }
        shard = {
            "shard": "s0",
            "rules": [
                {
                    "rule_id": "R001",
                    "time_ms": 1000,
                    "all_of": [{"feature": "pause_norm", "op": ">=", "threshold": 0.2}],
                    "label": "B1",
                    "confidence": 0.8,
                }
            ],
        }
        meta = [
            {
                "rule_id": "R001",
                "generation": 1,
                "active": 1,
                "tombstone": 0,
                "tombstone_reason": None,
                "first_seen_ms": 2000,
                "updated_ms": 1000,
            }
        ]
        write_v2_package(package, manifest, [{"shard_file": "s0.json", "payload": shard}], meta)
        with self.assertRaises(DiagnosticError) as ctx:
            load_decision_tree(package)
        self.assertEqual(ctx.exception.code, "E_FORMAT_CORRUPT")


class MigrationPrecisionAndAdjudicationTests(DiagnosticTestBase):
    def test_migration_equivalence_and_semantic_loss(self) -> None:
        package, report = migrate_v1_to_v2(self.v1)
        migrated_dir = self.root / "migrated-v2-test"
        from prosody_diag.trees import write_migrated_v2

        write_migrated_v2(migrated_dir, package)
        migrated_v2 = load_decision_tree(migrated_dir)
        self.assertEqual(
            canonical_hash(_rule_set_without_id(self.v1)),
            canonical_hash(_rule_set_without_id(migrated_v2)),
        )
        downgraded, downgrade_report = migrate_v2_to_v1(migrated_v2, strict_equivalence=True)
        reparsed = parse_v1_tree(downgraded)
        self.assertEqual(
            canonical_hash(_rule_set_without_id(migrated_v2)),
            canonical_hash(_rule_set_without_id(reparsed)),
        )
        self.assertGreaterEqual(len(report["completion_sources"]), 15)
        with self.assertRaises(DiagnosticError) as ctx:
            migrate_v2_to_v1(self.v2, strict_equivalence=True)
        self.assertEqual(ctx.exception.code, "E_SEMANTIC_LOSS")
        explicit, native_report = migrate_v2_to_v1(self.v2, tombstone_policy="discard")
        self.assertIn("R007", native_report["discarded_tombstone_ids"])
        self.assertTrue(explicit["rule_lines"])

    def test_nested_rule_flattening(self) -> None:
        paths = flatten_condition_tree(
            {
                "all_of": [
                    {"feature": "a", "op": ">=", "threshold": 1},
                    {
                        "any_of": [
                            {"feature": "b", "op": ">=", "threshold": 2},
                            {"feature": "c", "op": "<", "threshold": 3},
                        ]
                    },
                ]
            }
        )
        self.assertEqual(len(paths), 2)
        self.assertEqual([condition["feature"] for condition in paths[0]], ["a", "b"])
        self.assertEqual([condition["feature"] for condition in paths[1]], ["a", "c"])

    def test_precision_profiles_keep_paths_and_labels(self) -> None:
        for record in self.acoustic_records():
            signatures = {}
            for profile in ("fp64", "fp32", "q15"):
                context = PrecisionContext(profile)
                profile_signatures = []
                for candidate in record["candidates"]:
                    result = infer_candidate(self.v2, candidate, context, self.cfg)
                    profile_signatures.append(
                        (
                            candidate["candidate_id"],
                            result["label"],
                            result["selected_rule_id"],
                            tuple(
                                (condition["feature"], condition["result"])
                                for trace in result["decision_path"]
                                for condition in trace["conditions"]
                            ),
                        )
                    )
                signatures[profile] = profile_signatures
            self.assertEqual(signatures["fp64"], signatures["fp32"])
            self.assertEqual(signatures["fp64"], signatures["q15"])

    def test_conflict_overflow_and_ledger_recompute(self) -> None:
        truth_index = index_boundary_truth(self.boundary_truth)
        judgments = []
        for task, record in zip(self.tasks, self.acoustic_records()):
            for candidate in record["candidates"]:
                judgments.append(
                    judge_candidate(task, candidate, self.trees, truth_index, "fp64", self.cfg)
                )
        conflicts = enforce_conflict_threshold(judgments, self.cfg.conflict_threshold)
        self.assertGreater(len(conflicts), 0)
        with self.assertRaises(DiagnosticError) as ctx:
            enforce_conflict_threshold(judgments, 0)
        self.assertEqual(ctx.exception.code, "E_CONFLICT_OVERFLOW")
        rows = judgments_to_ledger_rows(judgments)
        self.assertEqual(
            canonical_hash(rows),
            canonical_hash(judgments_to_ledger_rows(judgments)),
        )

    def test_tolerance_failure_has_first_position(self) -> None:
        frame = self.acoustic_records()[0]["frames"][10]
        deviation = build_frame_deviation(frame, self.cfg, "q15")
        strict_cfg = Config(
            error_tolerances={**self.cfg.error_tolerances, "feature": 1e-12}
        )
        with self.assertRaises(DiagnosticError) as ctx:
            check_tolerances([deviation], [], strict_cfg)
        self.assertEqual(ctx.exception.code, "E_TOLERANCE_EXCEEDED")
        self.assertEqual(ctx.exception.details["level"], "feature")
        self.assertIn("first_position", ctx.exception.details)


class PipelineStateTests(DiagnosticTestBase):
    def test_revision_invalidation_targets_one_utterance(self) -> None:
        output = self.root / "pipeline-invalidation"
        DiagnosticPipeline(self.sample, output, self.cfg).run()
        overrides = {"revision_overrides": {"U002": 2}}
        plan = DiagnosticPipeline(
            self.sample,
            output,
            self.cfg,
            overrides=overrides,
            change_spec={"type": "revision", "utterance_id": "U002"},
        ).run()
        self.assertEqual(plan["recomputed_utterance_ids"], ["U002"])
        self.assertEqual(
            plan["reused_utterance_ids"],
            ["U001", "U003", "U004", "U005", "U006"],
        )
        trace = StateStore(output)
        self.assertTrue(trace.latest_path("report").exists())

    def test_resource_limits_record_degradations(self) -> None:
        output = self.root / "pipeline-resources"
        cfg = Config(
            max_utterances_per_run=2,
            max_resident_frames=10,
            max_cache_entries=1,
        )
        plan = DiagnosticPipeline(self.sample, output, cfg).run()
        categories = {item["category"] for item in plan["degradations"]}
        self.assertIn("batch_streaming", categories)
        self.assertIn("feature_streaming", categories)
        self.assertIn("cache_prune_threshold", categories)

    def test_error_spectrum_statistics_recompute_from_details(self) -> None:
        output = self.root / "spectrum-recompute"
        DiagnosticPipeline(self.sample, output, self.cfg).run()
        spectrum = read_json(output / "error_spectrum.json")
        recomputed = summarize_error_spectrum(
            spectrum["frame_deviations"], spectrum["candidate_deviations"]
        )
        self.assertEqual(canonical_hash(recomputed), canonical_hash(spectrum["summary"]))
        observation = summarize_truth_observation_details(
            spectrum["observation_frame_deviations"]
        )
        self.assertEqual(
            canonical_hash(observation),
            canonical_hash(spectrum["observation_vs_truth"]),
        )

    def test_stage_busy_code(self) -> None:
        output = self.root / "busy"
        store = StateStore(output)
        with store.commit_lock():
            with self.assertRaises(DiagnosticError) as ctx:
                StateStore(output).commit_snapshot("features", [])
        self.assertEqual(ctx.exception.code, "E_STAGE_BUSY")

    def test_interrupt_and_resume_match_control(self) -> None:
        control = self.root / "control"
        recovered = self.root / "recovered"
        control_proc = subprocess.run(
            [
                sys.executable,
                "run.py",
                "--mode",
                "full",
                "--sample-dir",
                str(self.sample),
                "--output-dir",
                str(control),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(control_proc.returncode, 0, control_proc.stderr)
        interrupt_proc = subprocess.run(
            [
                sys.executable,
                "run.py",
                "--mode",
                "full",
                "--sample-dir",
                str(self.sample),
                "--output-dir",
                str(recovered),
                "--interrupt-at",
                "adjudication:2",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(interrupt_proc.returncode, 37, interrupt_proc.stderr)
        resume_proc = subprocess.run(
            [
                sys.executable,
                "run.py",
                "--mode",
                "full",
                "--sample-dir",
                str(self.sample),
                "--output-dir",
                str(recovered),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(resume_proc.returncode, 0, resume_proc.stderr)
        self._assert_trees_match(control, recovered)

    def _assert_trees_match(self, control: Path, recovered: Path) -> None:
        ignore = {"last_run_plan.json"}
        control_files = {
            path.relative_to(control): path
            for path in control.rglob("*")
            if path.is_file() and path.name not in ignore
        }
        recovered_files = {
            path.relative_to(recovered): path
            for path in recovered.rglob("*")
            if path.is_file() and path.name not in ignore
        }
        self.assertEqual(set(control_files), set(recovered_files))
        for relative, control_path in control_files.items():
            self.assertEqual(
                control_path.read_bytes(),
                recovered_files[relative].read_bytes(),
                f"recovered file differs: {relative}",
            )


def _rule_set_without_id(tree: dict) -> dict:
    from prosody_diag.trees import canonical_tree_view

    view = canonical_tree_view(tree)
    view.pop("tree_id", None)
    return view


if __name__ == "__main__":
    unittest.main()
