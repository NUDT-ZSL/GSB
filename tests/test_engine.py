from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from generate_samples import SESSIONS, generate_all
from vad_engine import EngineConfig
from vad_engine.cache import block_source_hash, envelope_cache_key
from vad_engine.drift import EventReplayer, compare_serial_whole
from vad_engine.envelope import (
    block_bounds,
    compute_envelope_serial,
    compute_envelope_whole,
    load_pcm,
)
from vad_engine.errors import (
    D_ENERGY_DRIFT,
    E_FORMAT_CORRUPT,
    E_PARSE_ERROR,
    E_RESOURCE_LIMIT,
    E_STAGE_BUSY,
    E_STATE_INCONSISTENT,
    E_TIMELINE_OVERLAP,
    E_VERSION_UNSUPPORTED,
    EngineError,
)
from vad_engine.pipeline import Pipeline
from vad_engine.rules import (
    migrate_v1_to_v2,
    migrate_v2_to_v1,
    read_rules,
)
from vad_engine.state import CommitGate
from vad_engine.util import atomic_write_json, canonical_json_bytes, load_json_file
from vad_engine.verification import run_recovery_drill


class EngineTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = Path(tempfile.mkdtemp(prefix="vad-tests-"))
        cls.data = cls.temp / "data"
        generate_all(cls.data, 20261126)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.temp, ignore_errors=True)


class TestDeterministicGenerationAndEnvelope(EngineTestBase):
    def test_sample_generation_is_byte_reproducible(self) -> None:
        second = self.temp / "data_second"
        generate_all(second, 20261126)
        for relative in (
            "manifest.json",
            "pcm/session_001.pcm",
            "truth/session_001.jsonl",
            "meta/session_001.json",
            "rules/v1/session_001.json",
        ):
            self.assertEqual(
                (self.data / relative).read_bytes(),
                (second / relative).read_bytes(),
                relative,
            )
        for session in SESSIONS:
            left = read_rules(self.data / "rules" / "v2" / session.session_id)
            right = read_rules(second / "rules" / "v2" / session.session_id)
            self.assertEqual(left.revision_hash, right.revision_hash)
        for relative in (
            "rules/v2/session_001/manifest.json",
            "rules/v2/session_001/shards/shard_000.json",
            "rules/v2/session_001/metadata.sqlite3",
        ):
            self.assertEqual(
                (self.data / relative).read_bytes(),
                (second / relative).read_bytes(),
                relative,
            )

    def test_envelope_boundaries_and_bytes_are_deterministic(self) -> None:
        config = EngineConfig()
        samples, pcm_hash, _ = load_pcm(self.data / "pcm" / "session_001.pcm")
        first = compute_envelope_serial(samples, config, pcm_sha256=pcm_hash)
        second = compute_envelope_serial(samples, config, pcm_sha256=pcm_hash)
        whole = compute_envelope_whole(samples, config, pcm_sha256=pcm_hash)
        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))
        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(whole))
        bounds = block_bounds(len(samples), config)
        self.assertEqual(bounds[0], (0, 0, 320))
        self.assertEqual(bounds[1], (1, 160, 480))
        for block, (index, start, end) in zip(first, bounds):
            self.assertEqual(block["block_index"], index)
            self.assertEqual(block["sample_start"], start)
            self.assertEqual(block["sample_end"], end)

    def test_cache_key_contains_all_required_components(self) -> None:
        config = EngineConfig()
        samples, pcm_hash, _ = load_pcm(self.data / "pcm" / "session_001.pcm")
        upstream = block_source_hash(samples, 0, 320)
        base = envelope_cache_key(
            pcm_sha256=pcm_hash,
            rule_version="v1@a|v2@b",
            config=config,
            upstream_envelope_hash=upstream,
        )
        variants = (
            envelope_cache_key(
                pcm_sha256="f" * 64,
                rule_version="v1@a|v2@b",
                config=config,
                upstream_envelope_hash=upstream,
            ),
            envelope_cache_key(
                pcm_sha256=pcm_hash,
                rule_version="v1@x|v2@b",
                config=config,
                upstream_envelope_hash=upstream,
            ),
            envelope_cache_key(
                pcm_sha256=pcm_hash,
                rule_version="v1@a|v2@b",
                config=EngineConfig(frame_shift_ms=20),
                upstream_envelope_hash=upstream,
            ),
            envelope_cache_key(
                pcm_sha256=pcm_hash,
                rule_version="v1@a|v2@b",
                config=config,
                upstream_envelope_hash="e" * 64,
            ),
        )
        self.assertEqual(len({base, *variants}), 5)

    def test_envelope_snapshot_checksum_is_reproducible(self) -> None:
        checksums: list[str] = []
        codes: list[str] = []
        for name in ("det_a", "det_b"):
            result = Pipeline(
                data_dir=self.data,
                state_dir=self.temp / f"{name}_state",
                report_dir=self.temp / f"{name}_reports",
                config=EngineConfig(),
            ).run(stop_after="envelope")
            checksums.append(result.stages["envelope"].record["checksum"])
            codes.append(result.stages["envelope"].payload["business_codes"])
        self.assertEqual(checksums[0], checksums[1])
        self.assertEqual(codes[0], codes[1])


class TestRuleReaders(EngineTestBase):
    def test_v1_and_v2_dispatch(self) -> None:
        v1 = read_rules(self.data / "rules" / "v1" / "session_001.json")
        v2 = read_rules(self.data / "rules" / "v2" / "session_001")
        self.assertEqual(v1.format_version, "v1")
        self.assertEqual(v2.format_version, "v2")
        self.assertGreater(len(v1.rules), 0)
        self.assertGreater(len(v2.rules), len(v1.rules))

    def test_unknown_version(self) -> None:
        copied = self.temp / "bad_version"
        shutil.copytree(self.data / "rules" / "v2" / "session_001", copied)
        manifest_path = copied / "manifest.json"
        manifest = load_json_file(manifest_path, context="manifest")
        manifest["format"] = "vad-rules/v9"
        atomic_write_json(manifest_path, manifest)
        with self.assertRaises(EngineError) as context:
            read_rules(copied)
        self.assertEqual(context.exception.code, E_VERSION_UNSUPPORTED)

    def test_missing_field(self) -> None:
        payload = load_json_file(
            self.data / "rules" / "v1" / "session_001.json", context="v1"
        )
        del payload["rules"][0]["energy_threshold"]
        path = self.temp / "missing.json"
        atomic_write_json(path, payload)
        with self.assertRaises(EngineError) as context:
            read_rules(path)
        self.assertEqual(context.exception.code, E_FORMAT_CORRUPT)

    def test_unparseable_numeric(self) -> None:
        payload = load_json_file(
            self.data / "rules" / "v1" / "session_001.json", context="v1"
        )
        payload["rules"][0]["energy_threshold"] = "not-a-number"
        path = self.temp / "bad_number.json"
        atomic_write_json(path, payload)
        with self.assertRaises(EngineError) as context:
            read_rules(path)
        self.assertEqual(context.exception.code, E_PARSE_ERROR)

    def test_timeline_overlap(self) -> None:
        payload = load_json_file(
            self.data / "rules" / "v1" / "session_001.json", context="v1"
        )
        payload["rules"][1]["speaker_id"] = payload["rules"][0]["speaker_id"]
        payload["rules"][1]["start_sec"] = payload["rules"][0]["start_sec"]
        path = self.temp / "overlap.json"
        atomic_write_json(path, payload)
        with self.assertRaises(EngineError) as context:
            read_rules(path)
        self.assertEqual(context.exception.code, E_TIMELINE_OVERLAP)


class TestMigration(EngineTestBase):
    def test_v1_to_v2_and_v2_to_v1(self) -> None:
        v1 = read_rules(self.data / "rules" / "v1" / "session_001.json")
        v2 = read_rules(self.data / "rules" / "v2" / "session_001")
        with tempfile.TemporaryDirectory(dir=self.temp) as directory:
            root = Path(directory)
            up = migrate_v1_to_v2(v1, root / "up")
            self.assertEqual(up.business_code, "M_V1_TO_V2_OK")
            self.assertGreater(up.semantic_checks, 0)
            self.assertTrue(
                all(
                    {"field", "value", "source", "rule_id"} <= set(item)
                    for item in up.provenance
                )
            )
            self.assertEqual(
                up.migrated.active_rule("spk_a", 500).energy_threshold,
                v1.active_rule("spk_a", 500).energy_threshold,
            )
            down = migrate_v2_to_v1(v2, root / "down.json")
            self.assertEqual(down.business_code, "M_V2_TO_V1_OK")
            self.assertGreater(down.semantic_checks, 0)
            self.assertGreater(
                down.dropped_counts.get("M_DROP_GENERATION_UNSUPPORTED", 0), 0
            )
            self.assertGreater(
                down.dropped_counts.get("M_DROP_SCOPE_TOMBSTONE_UNSUPPORTED", 0), 0
            )
            self.assertTrue(
                all(item.get("business_code") for item in down.discarded)
            )


class TestPipelineAndVerification(EngineTestBase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.state = cls.temp / "state"
        cls.reports = cls.temp / "reports"
        cls.config = EngineConfig()
        cls.pipeline = Pipeline(
            data_dir=cls.data,
            state_dir=cls.state,
            report_dir=cls.reports,
            config=cls.config,
        )
        cls.run_result = cls.pipeline.run()

    def test_pipeline_has_four_monotonic_snapshots(self) -> None:
        history = self.pipeline.store.snapshot_history()
        self.assertEqual([row["version"] for row in history], [1, 2, 3, 4])
        self.assertEqual([row["stage"] for row in history], ["envelope", "align", "drift", "report"])
        for row in history:
            self.assertEqual(
                set(row["completed_utterances"]),
                self.pipeline.expected_utterances,
            )

    def test_drift_events_are_replayable_and_dual_precision(self) -> None:
        events = self.run_result.report["drift_events"]
        self.assertGreater(len(events), 0)
        replayer = EventReplayer(self.data, self.config)
        for event in events[:10]:
            self.assertEqual(
                event["float64_business_code"],
                event["fixed_point_business_code"],
            )
            self.assertTrue(event["dual_precision_consistent"])
            replay = replayer.replay(event)
            self.assertEqual(replay["drift_category"], event["drift_category"])
            self.assertEqual(
                replay["sample_interval"],
                [
                    event["sample_interval"]["start"],
                    event["sample_interval"]["end"],
                ],
            )
            self.assertIn("block_sha256", event["envelope_block"])
            self.assertIn("rule_version", event)
            self.assertIn("precomputed_hash", event)

    def test_stage_reuse_does_not_create_new_versions(self) -> None:
        resumed = Pipeline(
            data_dir=self.data,
            state_dir=self.state,
            report_dir=self.reports,
            config=self.config,
        )
        second = resumed.run()
        self.assertTrue(all(stage.reused for stage in second.stages.values()))
        self.assertEqual(
            [row["version"] for row in resumed.store.snapshot_history()],
            [1, 2, 3, 4],
        )

    def test_serial_whole_first_deviation_is_reported(self) -> None:
        serial = copy.deepcopy(self.run_result.report["drift_events"][:1])
        whole = copy.deepcopy(serial)
        if not whole:
            self.skipTest("no drift events generated")
            return
        whole[0]["drift_category"] = D_ENERGY_DRIFT
        whole[0]["sample_interval"]["end"] += 1
        with self.assertRaises(EngineError) as context:
            compare_serial_whole(serial, whole, session_id="session_001")
        self.assertEqual(context.exception.code, E_STATE_INCONSISTENT)
        self.assertIn("first_deviation", context.exception.details)

    def test_memory_and_cache_budgets(self) -> None:
        memory_config = EngineConfig(max_envelope_memory_bytes=1024)
        with self.assertRaises(EngineError) as context:
            Pipeline(
                data_dir=self.data,
                state_dir=self.temp / "memory_state",
                report_dir=self.temp / "memory_reports",
                config=memory_config,
            ).run(stop_after="envelope")
        self.assertEqual(context.exception.code, E_RESOURCE_LIMIT)

        cache_config = EngineConfig(max_cache_entries=10)
        with self.assertRaises(EngineError) as context:
            Pipeline(
                data_dir=self.data,
                state_dir=self.temp / "cache_limit_state",
                report_dir=self.temp / "cache_limit_reports",
                config=cache_config,
            ).run(stop_after="envelope")
        self.assertEqual(context.exception.code, E_RESOURCE_LIMIT)

    def test_parallel_soft_limit_degrades_to_serial(self) -> None:
        config = EngineConfig(max_parallel_chunks=2, parallel_chunks=8)
        result = Pipeline(
            data_dir=self.data,
            state_dir=self.temp / "degrade_state",
            report_dir=self.temp / "degrade_reports",
            config=config,
        ).run(stop_after="envelope")
        degradation = result.stages["envelope"].payload["degradation"]
        self.assertEqual(degradation["business_code"], "R_DEGRADED_PARALLEL_TO_SERIAL")
        self.assertEqual(degradation["effective"], 1)

    def test_parallel_hard_limit(self) -> None:
        config = EngineConfig(
            max_parallel_chunks=2,
            parallel_chunks=8,
            strict_resources=True,
        )
        with self.assertRaises(EngineError) as context:
            Pipeline(
                data_dir=self.data,
                state_dir=self.temp / "strict_state",
                report_dir=self.temp / "strict_reports",
                config=config,
            ).run(stop_after="envelope")
        self.assertEqual(context.exception.code, E_RESOURCE_LIMIT)

    def test_commit_gate_returns_busy_immediately(self) -> None:
        gate1 = CommitGate(self.temp / "gate.lock")
        gate1.acquire(stage="align", owner="first")
        try:
            gate2 = CommitGate(self.temp / "gate.lock")
            with self.assertRaises(EngineError) as context:
                gate2.acquire(stage="drift", owner="second")
            self.assertEqual(context.exception.code, E_STAGE_BUSY)
        finally:
            gate1.release()

    def test_report_files_exist(self) -> None:
        for name in (
            "run_report.json",
            "drift_report.json",
            "drift_report.csv",
            "verification.json",
            "summary.md",
        ):
            self.assertTrue((self.reports / name).is_file(), name)


class TestRecoveryDrill(EngineTestBase):
    def test_forced_termination_and_cache_replacement(self) -> None:
        report = run_recovery_drill(data_dir=self.data, config=EngineConfig())
        self.assertTrue(report["passed"], json.dumps(report, ensure_ascii=False))
        self.assertTrue(report["uncommitted_crash"]["passed"])
        self.assertTrue(report["committed_resume"]["passed"])
        self.assertTrue(report["cache_reuse"]["passed"])
        self.assertTrue(report["cache_whole_replacement"]["passed"])


if __name__ == "__main__":
    unittest.main()
