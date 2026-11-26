from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from flashdiag.core import (
    assert_arrays_equal,
    compute_full_blockwise,
    compute_full_reference,
    compute_score_block,
    merge_score_blocks,
    normalize_block,
)
from flashdiag.engine import FlashAttentionDiagnostics
from flashdiag.errors import (
    DiagnosticError,
    E_CACHE_CORRUPT,
    E_INPUT_INVALID,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    E_TOLERANCE_EXCEEDED,
)
from flashdiag.models import ResourcePolicy, TaskSpec
from flashdiag.planner import load_manifest, prepare_task
from flashdiag.samples import SampleCase, generate_samples, tree_content_sha256


TINY = SampleCase(
    case_id="tiny-causal",
    seq_len=16,
    head_dim=4,
    num_heads=2,
    q_block_len=4,
    kv_block_len=4,
    dtype="float64",
    causal=True,
    seed=77,
    changed_head=1,
    changed_from=9,
)


class DiagnosticsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.runtime = self.root / "runtime"
        generate_samples(self.data, cases=(TINY,))
        self.base_manifest = self.data / "tasks.jsonl"
        self.revision_manifest = self.data / "tasks_rev.jsonl"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def engine(
        self,
        *,
        policy: ResourcePolicy | None = None,
        name: str = "runtime",
    ) -> FlashAttentionDiagnostics:
        runtime = self.root / name
        return FlashAttentionDiagnostics(
            runtime / "ledger.db",
            runtime / "store",
            policy=policy or ResourcePolicy(max_parallel_q_blocks=8),
            export_root=runtime / "exports",
        )

    def test_generator_is_deterministic_and_revision_is_head_local(self) -> None:
        second = self.root / "data-second"
        generate_samples(second, cases=(TINY,))
        self.assertEqual(tree_content_sha256(self.data), tree_content_sha256(second))

        base_task = load_manifest(self.base_manifest)[0]
        revision_task = load_manifest(self.revision_manifest)[0]
        base_q = np.load(base_task.q_path)
        base_k = np.load(base_task.k_path)
        base_v = np.load(base_task.v_path)
        revision_q = np.load(revision_task.q_path)
        revision_k = np.load(revision_task.k_path)
        revision_v = np.load(revision_task.v_path)
        np.testing.assert_array_equal(base_q, revision_q)
        for head in range(TINY.num_heads):
            if head == TINY.changed_head:
                self.assertFalse(np.array_equal(base_k[head], revision_k[head]))
                self.assertFalse(np.array_equal(base_v[head], revision_v[head]))
            else:
                np.testing.assert_array_equal(base_k[head], revision_k[head])
                np.testing.assert_array_equal(base_v[head], revision_v[head])

    def test_validation_rejects_duplicate_and_bad_shape(self) -> None:
        lines = self.base_manifest.read_text(encoding="utf-8").splitlines()
        duplicate = self.data / "duplicate.jsonl"
        duplicate.write_text(lines[0] + "\n" + lines[0] + "\n", encoding="utf-8")
        with self.assertRaises(DiagnosticError) as raised:
            load_manifest(duplicate)
        self.assertEqual(raised.exception.code, E_INPUT_INVALID)

        record = json.loads(lines[0])
        bad_q = self.data / "bad-q.npy"
        np.save(
            bad_q,
            np.zeros(
                (TINY.num_heads, TINY.seq_len, TINY.head_dim + 1),
                dtype=np.float64,
            ),
            allow_pickle=False,
        )
        record["q_path"] = str(bad_q)
        task = TaskSpec.from_record(record, self.data)
        with self.assertRaises(DiagnosticError) as raised:
            prepare_task(task)
        self.assertEqual(raised.exception.code, E_INPUT_INVALID)
        self.assertIn("shape", raised.exception.message)

    def test_validation_rejects_block_length_and_dtype(self) -> None:
        record = json.loads(self.base_manifest.read_text(encoding="utf-8").splitlines()[0])
        invalid = dict(record)
        invalid["q_block_len"] = TINY.seq_len + 1
        with self.assertRaises(DiagnosticError) as raised:
            TaskSpec.from_record(invalid, self.data)
        self.assertEqual(raised.exception.code, E_INPUT_INVALID)

    def test_manifest_can_use_conventional_array_layout(self) -> None:
        record = json.loads(self.base_manifest.read_text(encoding="utf-8").splitlines()[0])
        for key in ("q_path", "k_path", "v_path"):
            record.pop(key)
        task = TaskSpec.from_record(record, self.data)
        self.assertEqual(task.q_path, self.data / "arrays" / TINY.case_id / "rev1" / "q.npy")
        prepared = prepare_task(task)
        self.assertEqual(prepared.task.case_id, TINY.case_id)

        invalid = dict(record)
        invalid["dtype"] = "int32"
        with self.assertRaises(DiagnosticError) as raised:
            TaskSpec.from_record(invalid, self.data)
        self.assertEqual(raised.exception.code, E_INPUT_INVALID)

    def test_core_blockwise_matches_whole_sequence_reference(self) -> None:
        prepared = prepare_task(load_manifest(self.base_manifest)[0])
        block_output, block_max, block_denom = compute_full_blockwise(
            prepared.arrays,
            prepared.plans,
            prepared.task.causal,
        )
        reference_output, reference_max, reference_denom = compute_full_reference(
            prepared.arrays,
            prepared.task.causal,
        )
        np.testing.assert_allclose(block_output, reference_output, atol=1e-12, rtol=1e-12)
        np.testing.assert_allclose(block_max, reference_max, atol=1e-12, rtol=1e-12)
        np.testing.assert_allclose(block_denom, reference_denom, atol=1e-12, rtol=1e-12)

        repeat_output, _, _ = compute_full_blockwise(
            prepared.arrays,
            prepared.plans,
            prepared.task.causal,
        )
        np.testing.assert_array_equal(block_output, repeat_output)

        first_plan = prepared.plans[0]
        first_score = compute_score_block(
            np.asarray(
                prepared.arrays.q[
                    first_plan.head,
                    first_plan.q_start : first_plan.q_start + first_plan.q_len,
                ],
                dtype=np.float64,
            ),
            np.asarray(
                prepared.arrays.k[
                    first_plan.head,
                    first_plan.kv_blocks[0].start : first_plan.kv_blocks[0].stop,
                ],
                dtype=np.float64,
            ),
            np.asarray(
                prepared.arrays.v[
                    first_plan.head,
                    first_plan.kv_blocks[0].start : first_plan.kv_blocks[0].stop,
                ],
                dtype=np.float64,
            ),
            head=first_plan.head,
            q_block_index=first_plan.q_block,
            kv_block_index=first_plan.kv_blocks[0].index,
            q_start=first_plan.q_start,
            kv_start=first_plan.kv_blocks[0].start,
            causal=True,
        )
        self.assertEqual(first_score.scores.dtype, np.float64)
        self.assertEqual(first_score.partial.dtype, np.float64)
        merged = merge_score_blocks([first_score])
        self.assertEqual(normalize_block(merged).dtype, np.float64)

    def test_incremental_revision_is_surgical_and_matches_full_recompute(self) -> None:
        engine = self.engine()
        base = engine.run_manifest(self.base_manifest)[0]
        revision = engine.run_manifest(self.revision_manifest)[0]
        self.assertEqual(base.snapshot_version, 1)
        self.assertEqual(revision.snapshot_version, 2)
        self.assertEqual(revision.changed_heads, [TINY.changed_head])
        self.assertEqual(
            revision.affected_q_blocks,
            ["h1:q2", "h1:q3"],
        )
        expected_recomputed = {
            "score:h1:q2:k2",
            "score:h1:q3:k2",
            "score:h1:q3:k3",
            "norm:h1:q2",
            "norm:h1:q3",
            "writeback:h1:q2",
            "writeback:h1:q3",
        }
        self.assertEqual(set(revision.recomputed), expected_recomputed)
        self.assertTrue(revision.reused)
        self.assertIn("score:h0:q0:k0", revision.reused)
        self.assertNotIn("score:h0:q0:k0", revision.recomputed)

        revision_task = load_manifest(self.revision_manifest)[0]
        prepared = prepare_task(revision_task)
        full_output, _, _ = compute_full_blockwise(
            prepared.arrays,
            prepared.plans,
            prepared.task.causal,
        )
        incremental_output = np.load(revision.output_path)
        np.testing.assert_array_equal(incremental_output, full_output)
        reference_output, _, _ = compute_full_reference(
            prepared.arrays,
            prepared.task.causal,
        )
        np.testing.assert_allclose(
            incremental_output,
            reference_output,
            atol=1e-9,
            rtol=1e-9,
        )

        base_output = np.load(base.output_path)
        np.testing.assert_array_equal(
            incremental_output[TINY.changed_head - 1],
            base_output[TINY.changed_head - 1],
        )
        np.testing.assert_array_equal(
            incremental_output[TINY.changed_head, :8],
            base_output[TINY.changed_head, :8],
        )
        verification = engine.verify_case(TINY.case_id)
        self.assertTrue(verification.ok)
        self.assertEqual(
            revision.verification["ledger_replay"]["ok"],
            True,
        )

    def test_run_is_bitwise_reproducible_across_fresh_ledgers(self) -> None:
        first_engine = self.engine(name="fresh-one")
        second_engine = self.engine(name="fresh-two")
        first = first_engine.run_manifest(self.base_manifest)[0]
        second = second_engine.run_manifest(self.base_manifest)[0]
        self.assertEqual(first.output_sha256, second.output_sha256)
        np.testing.assert_array_equal(
            np.load(first.output_path),
            np.load(second.output_path),
        )

    def test_cache_hit_refuses_score_recompute(self) -> None:
        engine = self.engine()
        engine.run_manifest(self.base_manifest)
        calls = 0
        original = compute_score_block

        def counting(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        with mock.patch("flashdiag.engine.compute_score_block", side_effect=counting):
            revision = engine.run_manifest(self.revision_manifest)[0]
        self.assertEqual(calls, 3)
        self.assertEqual(
            set(revision.recomputed),
            {
                "score:h1:q2:k2",
                "score:h1:q3:k2",
                "score:h1:q3:k3",
                "norm:h1:q2",
                "norm:h1:q3",
                "writeback:h1:q2",
                "writeback:h1:q3",
            },
        )

    def test_ledger_versions_replay_and_rollback(self) -> None:
        engine = self.engine()
        engine.run_manifest(self.base_manifest)
        revision = engine.run_manifest(self.revision_manifest)[0]
        history = engine.history(TINY.case_id)
        self.assertEqual([entry["version"] for entry in history], [1, 2])
        self.assertEqual([entry["is_active"] for entry in history], [False, True])
        self.assertEqual(revision.snapshot_version, 2)
        snapshot = engine.query_case(TINY.case_id)
        self.assertEqual(len(snapshot["plans"]), TINY.num_heads * 4)
        self.assertTrue(all(node["version"] == 2 for node in snapshot["nodes"]))
        self.assertNotEqual(
            history[0]["snapshot_sha256"],
            history[1]["snapshot_sha256"],
        )
        old_replay = engine.verify_case(TINY.case_id, version=1)
        self.assertTrue(old_replay.ok)
        self.assertEqual(old_replay.snapshot_version, 1)
        current_replay = engine.verify_case(TINY.case_id, version=2)
        self.assertTrue(current_replay.ok)

        with self.assertRaises(DiagnosticError) as raised:
            engine.run_manifest(self.base_manifest)
        self.assertEqual(raised.exception.code, E_SNAPSHOT_ROLLBACK)

        idempotent = engine.run_manifest(self.revision_manifest)[0]
        self.assertTrue(idempotent.idempotent_reuse)
        self.assertEqual(idempotent.snapshot_version, 2)
        self.assertEqual(len(engine.history(TINY.case_id)), 2)

    def test_duplicate_revision_with_changed_content_is_invalid(self) -> None:
        engine = self.engine()
        engine.run_manifest(self.base_manifest)
        task = load_manifest(self.base_manifest)[0]
        k = np.load(task.k_path)
        k[0, 0, 0] += 1.0
        np.save(task.k_path, k, allow_pickle=False)
        with self.assertRaises(DiagnosticError) as raised:
            engine.run_manifest(self.base_manifest)
        self.assertEqual(raised.exception.code, E_INPUT_INVALID)

    def test_duplicate_revision_with_changed_configuration_is_invalid(self) -> None:
        engine = self.engine()
        engine.run_manifest(self.base_manifest)
        record = json.loads(self.base_manifest.read_text(encoding="utf-8").splitlines()[0])
        record["q_block_len"] = 8
        changed_manifest = self.data / "changed-config.jsonl"
        changed_manifest.write_text(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(DiagnosticError) as raised:
            engine.run_manifest(changed_manifest)
        self.assertEqual(raised.exception.code, E_INPUT_INVALID)

    def test_stage_busy_when_commit_conflicts(self) -> None:
        engine = self.engine()
        engine.run_manifest(self.base_manifest)
        lock = sqlite3.connect(
            engine.ledger.path,
            timeout=0.0,
            isolation_level=None,
        )
        try:
            lock.execute("BEGIN IMMEDIATE")
            with self.assertRaises(DiagnosticError) as raised:
                engine.run_manifest(self.revision_manifest)
            self.assertEqual(raised.exception.code, E_STAGE_BUSY)
        finally:
            lock.execute("ROLLBACK")
            lock.close()

    def test_resource_limits_and_serial_degrade(self) -> None:
        degrading = self.engine(
            policy=ResourcePolicy(max_parallel_q_blocks=1),
            name="degraded",
        )
        result = degrading.run_manifest(self.base_manifest)[0]
        self.assertTrue(result.resource.degraded)
        self.assertEqual(result.resource.parallel_workers_used, 1)
        self.assertEqual(
            result.resource.degraded_reason,
            "parallel_q_block_limit",
        )

        rejecting = self.engine(
            policy=ResourcePolicy(
                max_parallel_q_blocks=1,
                allow_serial_degrade=False,
            ),
            name="rejecting",
        )
        with self.assertRaises(DiagnosticError) as raised:
            rejecting.run_manifest(self.base_manifest)
        self.assertEqual(raised.exception.code, E_RESOURCE_LIMIT)

        memory_limited = self.engine(
            policy=ResourcePolicy(max_resident_bytes=1),
            name="memory-limited",
        )
        with self.assertRaises(DiagnosticError) as raised:
            memory_limited.run_manifest(self.base_manifest)
        self.assertEqual(raised.exception.code, E_RESOURCE_LIMIT)

    def test_tolerance_error_reports_first_difference(self) -> None:
        expected = np.zeros((2, 3, 4), dtype=np.float64)
        actual = expected.copy()
        actual[1, 2, 3] = 1e-3
        with self.assertRaises(DiagnosticError) as raised:
            assert_arrays_equal("demo", actual, expected, atol=1e-6, rtol=0.0)
        self.assertEqual(raised.exception.code, E_TOLERANCE_EXCEEDED)
        first = raised.exception.details["first_difference"]
        self.assertEqual(first["index"], [1, 2, 3])
        self.assertEqual(first["path"], "demo")

    def test_ledger_tamper_is_detected(self) -> None:
        engine = self.engine()
        engine.run_manifest(self.base_manifest)
        snapshot = engine.query_case(TINY.case_id)
        score_node = next(node for node in snapshot["nodes"] if node["node_type"] == "score")
        artifact_dir = Path(score_node["artifact_path"]).parent
        partial_path = artifact_dir / "data" / "partial.npy"
        partial = np.load(partial_path, allow_pickle=False)
        partial[0, 0] += 1.0
        np.save(partial_path, partial, allow_pickle=False)
        with self.assertRaises(DiagnosticError) as raised:
            engine.verify_case(TINY.case_id)
        self.assertEqual(raised.exception.code, E_CACHE_CORRUPT)

    def test_replay_uses_immutable_validation_inputs(self) -> None:
        engine = self.engine()
        result = engine.run_manifest(self.base_manifest)[0]
        task = load_manifest(self.base_manifest)[0]
        k = np.load(task.k_path)
        k[0, 0, 0] += 10.0
        np.save(task.k_path, k, allow_pickle=False)
        replay = engine.verify_case(TINY.case_id, version=result.snapshot_version)
        self.assertTrue(replay.ok)
        self.assertEqual(replay.output_sha256, result.output_sha256)

    def test_cli_demo_returns_structured_business_codes(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "flashdiag",
                "demo",
                "--data-dir",
                str(self.root / "cli-data"),
                "--runtime-dir",
                str(self.root / "cli-runtime"),
                "--max-parallel-q-blocks",
                "8",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["business_code"], "OK")
        self.assertEqual(payload["acceptance"]["ledger_replay"], "ok")
        self.assertTrue(payload["cases"])


if __name__ == "__main__":
    unittest.main()

