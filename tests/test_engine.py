from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from index_diag.engine import IndexEngine, InjectedFault
from index_diag.errors import (
    DiagnosticError,
    E_BATCH_ROLLBACK,
    E_CONFIG_ROLLBACK,
    E_DUP_DOC,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_CORRUPT,
)
from index_diag.samples import generate_samples
from index_diag.samples import VOCABULARY

from .helpers import document, make_config, write_json, write_jsonl


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.samples = cls.root / "samples"
        cls.manifest = generate_samples(cls.samples, seed=20261127)
        cls.config = cls.samples / cls.manifest["config_v1"]
        cls.config_gen2 = cls.samples / cls.manifest["config_v1_generation2"]
        cls.config_v2 = cls.samples / cls.manifest["config_v2"]
        cls.batches = [
            cls.samples / relative for relative in cls.manifest["batches"]
        ]
        cls.replacement = cls.samples / cls.manifest["replacement"]
        cls.fault = (
            cls.samples / "faults" / "fail_before_compress_checkpoint.json"
        )
        cls.fault_after_postings = (
            cls.samples / "faults" / "fail_after_postings_checkpoint.json"
        )
        cls.recovery = cls.samples / "recovery" / "resume.json"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_full_build_query_noop_and_selfcheck(self) -> None:
        engine = IndexEngine(self.root / "full", config_path=self.config)
        result = engine.run(self.batches)
        self.assertTrue(result["ok"])
        self.assertEqual(result["checkpoint"]["phase"], "ledger")
        self.assertEqual(result["checkpoint"]["completed_by_phase"]["ledger"], [1, 2, 3])
        self.assertGreater(result["postings"]["total_postings"], 0)
        check = engine.self_check()
        self.assertEqual(check["status"], "ok")
        query = engine.query_term("search")
        self.assertGreater(query["doc_frequency"], 0)
        self.assertTrue(query["blocks"])
        decoded = engine.query_block("search", 0)
        self.assertTrue(decoded["values"])
        self.assertEqual(engine.last_business_code()["code"], "OK")

        second = engine.run(self.batches)
        self.assertEqual(second["work"]["analyzed_documents"], 0)
        self.assertEqual(second["work"]["postings_added"], 0)
        self.assertEqual(second["work"]["compression_blocks_recomputed"], 0)

    def test_fault_recovery_skips_committed_stages(self) -> None:
        work = self.root / "recovery"
        engine = IndexEngine(work, config_path=self.config)
        with self.assertRaises(InjectedFault):
            engine.run(self.batches, fault_path=self.fault)
        interrupted = IndexEngine(work)
        self.assertEqual(interrupted.checkpoint.phase, "postings")
        self.assertEqual(
            interrupted.status()["checkpoint"]["completed_by_phase"]["compress"], []
        )
        recovered = IndexEngine(work)
        result = recovered.run(recovery_path=self.recovery)
        self.assertEqual(result["work"]["analyzed_documents"], 0)
        self.assertEqual(result["work"]["postings_added"], 0)
        self.assertGreater(result["work"]["compression_blocks_recomputed"], 0)
        self.assertEqual(result["checkpoint"]["phase"], "ledger")
        self.assertEqual(recovered.self_check()["status"], "ok")

    def test_fault_after_checkpoint_advances_on_recovery(self) -> None:
        work = self.root / "recovery-after-checkpoint"
        engine = IndexEngine(work, config_path=self.config)
        with self.assertRaises(InjectedFault):
            engine.run(self.batches, fault_path=self.fault_after_postings)
        interrupted = IndexEngine(work)
        self.assertEqual(interrupted.checkpoint.phase, "postings")
        recovered = IndexEngine(work)
        result = recovered.run(recovery_path=self.recovery)
        self.assertEqual(result["work"]["analyzed_documents"], 0)
        self.assertEqual(result["work"]["postings_added"], 0)
        self.assertEqual(result["checkpoint"]["phase"], "ledger")
        self.assertEqual(recovered.self_check()["status"], "ok")

    def test_separate_batches_append_incrementally(self) -> None:
        work = self.root / "incremental"
        engine = IndexEngine(work, config_path=self.config)
        first = engine.run([self.batches[0]])
        self.assertEqual(first["postings"]["documents"], 4)
        second = engine.run([self.batches[1]])
        self.assertEqual(second["postings"]["documents"], 8)
        self.assertEqual(second["work"]["analyzed_documents"], 4)
        self.assertGreater(second["work"]["postings_added"], 0)
        self.assertEqual(second["invalidation"]["reason"], "incremental_batch")
        self.assertGreater(second["work"]["compression_blocks_reused"], 0)
        self.assertEqual(
            second["checkpoint"]["completed_by_phase"]["ledger"], [1, 2]
        )
        self.assertEqual(engine.self_check()["status"], "ok")

    def test_config_generation_upgrade_and_rollback(self) -> None:
        work = self.root / "config-upgrade"
        engine = IndexEngine(work, config_path=self.config)
        engine.run(self.batches)
        same_params_generation_two = self.root / "generation-two.json"
        write_json(
            same_params_generation_two,
            make_config(generation=2, vocabulary=VOCABULARY),
        )
        upgraded = IndexEngine(work, config_path=same_params_generation_two)
        result = upgraded.run(self.batches)
        self.assertEqual(result["config"]["generation"], 2)
        self.assertEqual(result["work"]["analyzed_documents"], 0)
        self.assertEqual(result["work"]["compression_blocks_recomputed"], 0)
        self.assertGreater(len(result["invalidation"]["rekeyed_terms"]), 0)
        self.assertEqual(upgraded.self_check()["status"], "ok")
        with self.assertRaises(DiagnosticError) as caught:
            upgraded.run(self.batches, config_path=self.config)
        self.assertEqual(caught.exception.code, E_CONFIG_ROLLBACK)

    def test_batch_replacement_reuses_suffix(self) -> None:
        work = self.root / "replacement"
        engine = IndexEngine(work, config_path=self.config)
        engine.run(self.batches)
        result = engine.replace_batch(2, self.replacement)
        self.assertTrue(result["ok"])
        invalidation = result["invalidation"]
        self.assertEqual(invalidation["reason"], "batch_replacement")
        self.assertGreater(invalidation["blocks_reused"], 0)
        self.assertGreater(invalidation["blocks_recomputed"], 0)
        self.assertTrue(invalidation["first_recomputed_block"])
        self.assertEqual(engine.self_check()["status"], "ok")
        self.assertEqual(engine.status()["postings"]["documents"], 12)

    def test_batch_rollback_and_duplicate_doc_errors(self) -> None:
        work = self.root / "rollback"
        engine = IndexEngine(work, config_path=self.config)
        engine.run([self.batches[1]])
        with self.assertRaises(DiagnosticError) as caught:
            engine.run([self.batches[0]])
        self.assertEqual(caught.exception.code, E_BATCH_ROLLBACK)

        duplicate = write_jsonl(
            self.root / "duplicate.jsonl",
            [document(3, 3005, ["search"]), document(3, 3005, ["index"])],
        )
        with self.assertRaises(DiagnosticError) as caught:
            engine.run([duplicate])
        self.assertEqual(caught.exception.code, E_DUP_DOC)

    def test_resource_limits_and_explicit_truncation(self) -> None:
        strict = self.root / "strict-config.json"
        write_json(
            strict, make_config(max_batch_docs=2, vocabulary=VOCABULARY)
        )
        engine = IndexEngine(self.root / "strict", config_path=strict)
        with self.assertRaises(DiagnosticError) as caught:
            engine.run([self.batches[0]])
        self.assertEqual(caught.exception.code, E_RESOURCE_LIMIT)

        degraded = self.root / "degraded-config.json"
        write_json(
            degraded,
            make_config(
                max_batch_docs=2,
                degrade_policy="truncate_batch",
                vocabulary=VOCABULARY,
            ),
        )
        engine2 = IndexEngine(self.root / "degraded", config_path=degraded)
        result = engine2.run([self.batches[0]])
        self.assertTrue(result["ok"])
        self.assertEqual(result["postings"]["documents"], 2)
        self.assertTrue(result["resource_actions"])
        self.assertEqual(engine2.self_check()["status"], "ok")

    def test_corrupt_ledger_is_rejected(self) -> None:
        work = self.root / "corrupt-ledger"
        engine = IndexEngine(work, config_path=self.config)
        engine.run([self.batches[0]])
        ledger = work / "state" / "ledger.json"
        original = ledger.read_text(encoding="utf-8")
        corrupted = original.replace('"batch_no":1', '"batch_no":2', 1)
        self.assertNotEqual(original, corrupted)
        ledger.write_text(corrupted, encoding="utf-8")
        with self.assertRaises(DiagnosticError) as caught:
            IndexEngine(work).self_check()
        self.assertEqual(caught.exception.code, E_SNAPSHOT_CORRUPT)


if __name__ == "__main__":
    unittest.main()
