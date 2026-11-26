from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from dsn_workbench.config import ConfigBundle, read_config, write_v1
from dsn_workbench.errors import E_PARALLEL_MISMATCH, WorkbenchError
from dsn_workbench.pipeline import (
    PipelineOptions,
    SimulatedCrash,
    _verify_serial_parallel,
    run_pipeline,
)
from dsn_workbench.report import write_diagnostic_report
from dsn_workbench.sample import generate_sample
from dsn_workbench.state import Ledger
from dsn_workbench.tasks import load_tasks
from dsn_workbench.util import atomic_write_text


class PipelineAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sample = self.root / "sample"
        self.sample_meta = generate_sample(self.sample, seed=731, count=16)
        self.v1 = Path(self.sample_meta["v1_config"])
        self.v2 = Path(self.sample_meta["v2_config"])
        self.tasks = Path(self.sample_meta["tasks"])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run(
        self,
        config: Path,
        *,
        name: str,
        state_name: str | None = None,
        workers: int = 2,
        batch_size: int = 3,
        **option_kwargs,
    ):
        root = self.root / (state_name or name)
        return run_pipeline(
            config,
            self.tasks,
            cache_dir=root / "cache",
            state_dir=root / "state",
            options=PipelineOptions(
                workers_override=workers,
                batch_size_override=batch_size,
                **option_kwargs,
            ),
        )

    def test_parallel_matches_serial_byte_for_byte(self) -> None:
        parallel = self._run(self.v1, name="parallel", workers=3, batch_size=2)
        serial = self._run(self.v1, name="serial", workers=1, batch_size=5)
        self.assertEqual(parallel.report["output_sha256"], serial.report["output_sha256"])
        verification = parallel.report["parallel_verification"]
        self.assertEqual(verification["status"], "verified")
        self.assertEqual(
            verification["serial_sha256"], verification["parallel_sha256"]
        )
        self.assertIsNone(verification["first_difference"])
        self.assertEqual(
            parallel.report["reference_metrics"]["token_list_matches"], 16
        )
        self.assertEqual(
            parallel.report["reference_metrics"]["pos_tag_list_matches"], 16
        )
        self.assertEqual(
            [output["sentence_id"] for output in parallel.outputs],
            sorted(
                [output["sentence_id"] for output in parallel.outputs],
                key=lambda value: int(value[1:]),
            ),
        )

    def test_v1_and_v2_configs_produce_same_business_output(self) -> None:
        v1 = self._run(self.v1, name="v1-run", workers=2)
        v2 = self._run(self.v2, name="v2-run", workers=2)
        self.assertEqual(v1.report["output_sha256"], v2.report["output_sha256"])
        self.assertEqual(
            [item["tokens"] for item in v1.outputs],
            [item["tokens"] for item in v2.outputs],
        )

    def test_per_sentence_domain_and_locale_select_scoped_behavior(self) -> None:
        bundle = read_config(self.v1)
        data = copy.deepcopy(bundle.data)
        data["domain_tags"]["legal"] = list(data["domain_tags"]["finance"])
        data["domain_pos_table"]["legal"] = {}
        config_path = self.root / "multi-domain.json"
        write_v1(
            ConfigBundle(data, 1, "test-multi-domain", bundle.provenance),
            config_path,
        )
        tasks = [
            {
                "sentence_id": "a-finance",
                "raw_text": "支付宝。",
                "token_list": ["支付宝", "。"],
                "pos_tag_list": ["ORG", "PUNCT"],
                "lang": "zh-CN",
                "domain": "finance",
            },
            {
                "sentence_id": "b-legal",
                "raw_text": "支付宝。",
                "token_list": ["支", "付", "宝", "。"],
                "pos_tag_list": ["UNK", "UNK", "UNK", "UNK"],
                "lang": "zh-CN",
                "domain": "legal",
            },
        ]
        tasks_path = self.root / "multi-domain-tasks.jsonl"
        atomic_write_text(
            tasks_path,
            "".join(
                json.dumps(task, ensure_ascii=False) + "\n" for task in tasks
            ),
        )
        run_root = self.root / "multi-domain-run"
        run = run_pipeline(
            config_path,
            tasks_path,
            cache_dir=run_root / "cache",
            state_dir=run_root / "state",
            options=PipelineOptions(workers_override=1),
        )
        finance = next(
            output
            for output in run.outputs
            if output["sentence_id"] == "a-finance"
        )
        legal = next(
            output
            for output in run.outputs
            if output["sentence_id"] == "b-legal"
        )
        self.assertEqual(finance["tokens"][0]["text"], "支付宝")
        self.assertEqual(finance["tokens"][0]["source"], "dictionary")
        self.assertEqual(
            [token["text"] for token in legal["tokens"]],
            ["支", "付", "宝", "。"],
        )
        self.assertTrue(
            all(token["source"] == "unknown" for token in legal["tokens"])
        )
        self.assertTrue(
            all(
                token["pos_tag"] in data["domain_tags"]["legal"]
                for token in legal["tokens"]
            )
        )
        manifest = self._read_json(run_root / "state" / "manifest.json")
        finance_key = manifest["sentences"]["a-finance"]["stages"][
            "candidates"
        ]["key"]
        legal_key = manifest["sentences"]["b-legal"]["stages"][
            "candidates"
        ]["key"]
        self.assertEqual(finance_key["domain"], "finance")
        self.assertEqual(legal_key["domain"], "legal")

    def test_targeted_dictionary_invalidation_reuses_unaffected_sentences(self) -> None:
        first = self._run(
            self.v1,
            name="baseline",
            state_name="invalidation",
            workers=1,
        )
        bundle = read_config(self.v1)
        data = copy.deepcopy(bundle.data)
        data["dictionary"] = [
            entry for entry in data["dictionary"] if entry["term"] != "财报"
        ]
        data["dictionary_version"] = "lex-2026-11-27.2"
        data["generation"] = 2
        data["logical_clock_ms"] += 1000
        changed_path = self.root / "v1-changed.json"
        write_v1(
            ConfigBundle(data, 1, "test-invalidation", bundle.provenance),
            changed_path,
        )
        second = self._run(
            changed_path,
            name="changed",
            state_name="invalidation",
            workers=1,
        )
        plan = second.report["cache_plan"]
        affected = plan["affected_sentences"]
        reused = plan["reuse_sentences"]
        self.assertGreater(len(affected), 0)
        self.assertGreater(len(reused), 0)
        self.assertEqual(len(affected) + len(reused), 16)
        self.assertEqual(plan["changed_terms"], ["财报"])
        affected_set = set(affected)
        for output in first.outputs:
            if "财报" in output["raw_text"]:
                self.assertIn(output["sentence_id"], affected_set)
        for sentence_id in affected:
            self.assertEqual(
                plan["first_invalid_stage"][sentence_id], "candidates"
            )
            self.assertEqual(
                plan["invalidated_stages"][sentence_id],
                ["candidates", "disambiguation", "tagging"],
            )
        for sentence_id in reused:
            self.assertIsNone(plan["first_invalid_stage"][sentence_id])

        candidate_stage = next(
            stage
            for stage in second.report["stage_reports"]
            if stage["stage"] == "candidates"
        )
        self.assertEqual(candidate_stage["cache_hits"], len(reused))
        self.assertEqual(candidate_stage["computed"], len(affected))
        normalize_stage = next(
            stage
            for stage in second.report["stage_reports"]
            if stage["stage"] == "normalize"
        )
        self.assertEqual(normalize_stage["cache_hits"], 16)
        self.assertEqual(normalize_stage["computed"], 0)

    def test_generation_only_change_reuses_all_semantic_artifacts(self) -> None:
        self._run(
            self.v1,
            name="gen-base",
            state_name="generation",
            workers=1,
        )
        bundle = read_config(self.v1)
        data = copy.deepcopy(bundle.data)
        data["generation"] += 1
        data["logical_clock_ms"] += 500
        changed_path = self.root / "v1-generation.json"
        write_v1(
            ConfigBundle(data, 1, "test-generation", bundle.provenance),
            changed_path,
        )
        run = self._run(
            changed_path,
            name="gen-next",
            state_name="generation",
            workers=1,
        )
        plan = run.report["cache_plan"]
        self.assertEqual(plan["affected_sentences"], [])
        self.assertEqual(len(plan["reuse_sentences"]), 16)
        self.assertTrue(
            all(
                stage["cache_hits"] == 16 and stage["computed"] == 0
                for stage in run.report["stage_reports"]
            )
        )

    def test_pipeline_degrades_to_serial_under_token_memory_pressure(self) -> None:
        bundle = read_config(self.v1)
        data = copy.deepcopy(bundle.data)
        data["workers"] = 2
        data["limits"]["max_token_cache_bytes"] = 1
        data["limits"]["resource_policy"] = "degrade_serial"
        config_path = self.root / "resource-degrade.json"
        write_v1(
            ConfigBundle(data, 1, "test-resource", bundle.provenance),
            config_path,
        )
        run = self._run(
            config_path,
            name="resource",
            workers=2,
            batch_size=3,
        )
        self.assertEqual(run.report["resources"]["workers"], 1)
        self.assertEqual(
            run.report["degradations"][0]["category"],
            "serial_due_token_cache_memory",
        )
        self.assertEqual(
            run.report["parallel_verification"]["status"], "serial_path"
        )
        ledger = Ledger(self.root / "resource" / "state")
        events = ledger.events()
        self.assertTrue(
            any(event["kind"] == "resource_degradation" for event in events)
        )

    def test_crash_resume_is_reproducible_and_skips_committed_stages(self) -> None:
        clean = self._run(self.v1, name="clean", workers=2, batch_size=3)
        crash_root = self.root / "crash"
        with self.assertRaises(SimulatedCrash):
            run_pipeline(
                self.v1,
                self.tasks,
                cache_dir=crash_root / "cache",
                state_dir=crash_root / "state",
                options=PipelineOptions(
                    workers_override=2,
                    batch_size_override=3,
                    crash_stage="disambiguation",
                    crash_after_batches=1,
                ),
            )
        resumed = run_pipeline(
            self.v1,
            self.tasks,
            cache_dir=crash_root / "cache",
            state_dir=crash_root / "state",
            options=PipelineOptions(workers_override=2, batch_size_override=3),
        )
        reports = {stage["stage"]: stage for stage in resumed.report["stage_reports"]}
        self.assertTrue(reports["normalize"]["checkpoint_reused"])
        self.assertTrue(reports["candidates"]["checkpoint_reused"])
        self.assertFalse(reports["disambiguation"]["checkpoint_reused"])
        self.assertEqual(reports["disambiguation"]["computed"], 16)
        self.assertGreaterEqual(len(resumed.report["pending_discarded"]), 1)
        self.assertEqual(clean.report["output_sha256"], resumed.report["output_sha256"])
        versions = [
            checkpoint["version"]
            for checkpoint in resumed.report["checkpoints"].values()
        ]
        self.assertEqual(versions, sorted(versions))
        for checkpoint in resumed.report["checkpoints"].values():
            self.assertEqual(len(checkpoint["completed_sentence_ids"]), 16)
            self.assertEqual(
                checkpoint["content_checksum"], checkpoint["payload_hash"]
            )

    def test_parallel_mismatch_reports_first_differing_sentence(self) -> None:
        from dsn_workbench.normalization import normalize_sentence
        from dsn_workbench.segmentation import disambiguate, generate_candidates
        from dsn_workbench.tagging import tag_and_validate

        config = read_config(self.v1).data
        tasks = load_tasks(self.tasks)
        tagged = {}
        for task in tasks:
            normalized = normalize_sentence(
                task["sentence_id"], task["raw_text"], config
            )
            candidates = generate_candidates(normalized, config)
            tokens = disambiguate(normalized, candidates, config)
            tagged[task["sentence_id"]] = tag_and_validate(
                normalized, tokens, config
            )
        first = tasks[0]["sentence_id"]
        tagged[first] = copy.deepcopy(tagged[first])
        tagged[first]["tokens"][0]["pos_tag"] = "BROKEN"
        with self.assertRaises(WorkbenchError) as context:
            _verify_serial_parallel(tasks, config, tagged)
        self.assertEqual(context.exception.code, E_PARALLEL_MISMATCH)
        self.assertEqual(
            context.exception.details["first_difference"]["sentence_id"], first
        )

    def test_cache_key_contains_required_versions_and_upstream_hash(self) -> None:
        run = self._run(self.v1, name="keys", workers=1)
        manifest = self._read_json(self.root / "keys" / "state" / "manifest.json")
        sentence = manifest["sentences"][run.outputs[0]["sentence_id"]]
        normalize_key = sentence["stages"]["normalize"]["key"]
        candidates_key = sentence["stages"]["candidates"]["key"]
        self.assertEqual(normalize_key["upstream_hash"], "root")
        self.assertEqual(normalize_key["dictionary_version"], "lex-2026-11-27.1")
        self.assertEqual(normalize_key["rule_version"], "rules-2026-11-27.1")
        self.assertIn("config_parameter_version", normalize_key)
        self.assertEqual(
            candidates_key["upstream_hash"],
            sentence["stages"]["normalize"]["object_hash"],
        )

    def test_html_diagnostic_contains_core_sections(self) -> None:
        run = self._run(self.v1, name="html", workers=1)
        output = write_diagnostic_report(
            self.root / "html" / "state",
            self.root / "html" / "report.html",
            report=run.report,
            outputs=run.outputs,
        )
        rendered = output.read_text(encoding="utf-8")
        self.assertIn("PASS", rendered)
        self.assertIn("Stage checkpoints and cache activity", rendered)
        self.assertIn("Versioned ledger", rendered)
        self.assertIn(run.outputs[0]["sentence_id"], rendered)

    def _read_json(self, path: Path):
        import json

        return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

