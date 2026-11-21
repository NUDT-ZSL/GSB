from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from dep_arc_interop import (
    BusinessError,
    ErrorCode,
    FormatVersion,
    ResourceLimits,
    SnapshotStore,
    judge_equivalence,
    load_annotated_samples,
    load_format,
    migrate,
    run_self_check,
    verify_roundtrip,
)
from dep_arc_interop.config import section_checksum
from dep_arc_interop.demo import run_demo
from dep_arc_interop.streaming import stream_migrate_samples

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SAMPLES = DATA / "samples"
INJECTION = DATA / "injection"
CONFIG = DATA / "config"


class InteropTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.store = SnapshotStore()
        cls.store.load(CONFIG / "controlled_snapshot_v1.json")
        cls.snapshot = cls.store.current
        cls.v1 = load_format(SAMPLES / "arcs_v1.txt", cls.snapshot)
        cls.v2 = load_format(SAMPLES / "arcs_v2.txt", cls.snapshot)
        cls.v3 = load_format(SAMPLES / "arcs_v3.json", cls.snapshot)
        cls.rich_v3 = load_format(INJECTION / "semantic_drift_v3.json", cls.snapshot)

    def assert_code(self, code: ErrorCode, fn, **details) -> BusinessError:
        with self.assertRaises(BusinessError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)
        for key, value in details.items():
            self.assertEqual(caught.exception.details.get(key), value)
        return caught.exception


class ReaderTests(InteropTestCase):
    def test_reads_all_three_versions(self) -> None:
        self.assertEqual(self.v1.version, FormatVersion.V1)
        self.assertEqual(self.v2.version, FormatVersion.V2)
        self.assertEqual(self.v3.version, FormatVersion.V3)
        expected = ["S001", "S002", "S003", "S004", "S005"]
        self.assertEqual(self.v1.graph.sent_ids(), expected)
        self.assertEqual(self.v2.graph.sent_ids(), expected)
        self.assertEqual(self.v3.graph.sent_ids(), expected)
        self.assertEqual(self.v1.graph.node_count(), 42)

    def test_field_set_dispatch_without_format_header(self) -> None:
        v1 = load_format("# sent_id=X1\n1|0|root\n2|1|obj\n", self.snapshot)
        v2 = load_format(
            (
                "# sent_id=X2\n# root=1\n"
                "token_id=1|head=0|deprel=root|confidence_low=0.0|"
                "confidence_high=1.0\n"
            ),
            self.snapshot,
        )
        self.assertEqual(v1.version, FormatVersion.V1)
        self.assertEqual(v2.version, FormatVersion.V2)
        alternate_header = load_format(
            "# version=v1\n# sent_id=X3\n1|0|root\n",
            self.snapshot,
        )
        self.assertEqual(alternate_header.version, FormatVersion.V1)

    def test_headerless_bad_field_count_is_format_corrupt(self) -> None:
        self.assert_code(
            ErrorCode.FORMAT_CORRUPT,
            lambda: load_format("# sent_id=X4\n1|0|root|extra\n", self.snapshot),
            line=2,
            field="fields",
            sent_id="X4",
            actual=4,
        )

    def test_unknown_version_has_line_and_field(self) -> None:
        self.assert_code(
            ErrorCode.VERSION_UNSUPPORTED,
            lambda: load_format(
                INJECTION / "format_unknown_version.txt",
                self.snapshot,
            ),
            line=1,
            field="version",
        )

    def test_truncated_header_is_format_corrupt(self) -> None:
        self.assert_code(
            ErrorCode.FORMAT_CORRUPT,
            lambda: load_format(
                INJECTION / "format_truncated_header.txt",
                self.snapshot,
            ),
            line=1,
            field="format",
        )
        self.assert_code(
            ErrorCode.FORMAT_CORRUPT,
            lambda: load_format("# format=v\n", self.snapshot),
            line=1,
            field="format",
        )

    def test_v1_field_count_and_numeric_errors(self) -> None:
        self.assert_code(
            ErrorCode.FORMAT_CORRUPT,
            lambda: load_format(
                INJECTION / "format_v1_bad_field_count.txt",
                self.snapshot,
            ),
            line=3,
            field="fields",
            sent_id="B001",
            actual=4,
        )
        self.assert_code(
            ErrorCode.FORMAT_CORRUPT,
            lambda: load_format(
                INJECTION / "format_numeric_unparseable.txt",
                self.snapshot,
            ),
            line=3,
            field="token_id",
            sent_id="B003",
        )

    def test_v2_missing_required_field(self) -> None:
        self.assert_code(
            ErrorCode.FORMAT_CORRUPT,
            lambda: load_format(
                INJECTION / "format_v2_missing_field.txt",
                self.snapshot,
            ),
            line=4,
            field="confidence_high",
            sent_id="B002",
        )

    def test_v3_unknown_version_and_missing_required_fields(self) -> None:
        unsupported = json.dumps({"format": "deparc", "version": "v9", "sentences": []})
        self.assert_code(
            ErrorCode.VERSION_UNSUPPORTED,
            lambda: load_format(unsupported, self.snapshot),
            field="version",
        )
        corrupt = json.dumps(
            {
                "format": "deparc",
                "version": "v3",
                "sentences": [
                    {
                        "sent_id": "VX",
                        "root": 1,
                        "tokens": [
                            {
                                "token_id": 1,
                                "head": 0,
                                "deprel": "root",
                                "confidence": {"low": 0.0, "high": 1.0},
                            }
                        ],
                        "bidirectional_arc_chains": [],
                        "cross_sentence_refs": [],
                    }
                ],
            }
        )
        self.assert_code(
            ErrorCode.FORMAT_CORRUPT,
            lambda: load_format(corrupt, self.snapshot),
            field="sentences[0].tokens[0].dependency_checks",
            sent_id="VX",
        )

    def test_v3_cross_sentence_reference_must_resolve(self) -> None:
        raw = json.loads(
            (INJECTION / "semantic_drift_v3.json").read_text(encoding="utf-8")
        )
        raw["sentences"][0]["cross_sentence_refs"][0]["target_token_id"] = 999
        self.assert_code(
            ErrorCode.FORMAT_CORRUPT,
            lambda: load_format(
                json.dumps(raw, ensure_ascii=False),
                self.snapshot,
            ),
            field="cross_sentence_refs.target_token_id",
            sent_id="S001",
            target_token_id=999,
        )


class ValidationTests(InteropTestCase):
    def test_semantic_validation_codes(self) -> None:
        cases = (
            ("semantic_bad_deprel.txt", ErrorCode.DEPREL_INVALID, "B010", 1),
            ("semantic_head_range.txt", ErrorCode.HEAD_RANGE, "B011", 2),
            ("semantic_two_roots.txt", ErrorCode.ROOT_VIOLATION, "B012", 1),
            ("semantic_cycle.txt", ErrorCode.CYCLE_DETECTED, "B013", 2),
            ("semantic_token_sequence.txt", ErrorCode.TOKEN_SEQ, "B014", 3),
            ("semantic_duplicate_sent.txt", ErrorCode.DUP_SENT, "B015", None),
        )
        for filename, code, sent_id, token_id in cases:
            with self.subTest(filename=filename):
                details = {"sent_id": sent_id}
                if token_id is not None:
                    details["token_id"] = token_id
                self.assert_code(
                    code,
                    lambda filename=filename: load_format(
                        INJECTION / filename,
                        self.snapshot,
                    ),
                    **details,
                )

    def test_v2_explicit_root_must_match_root_arc(self) -> None:
        text = (
            "# format=v2\n# sent_id=ROOTX\n# root=2\n"
            "token_id=1|head=0|deprel=root|confidence_low=0|confidence_high=1\n"
            "token_id=2|head=1|deprel=obj|confidence_low=0|confidence_high=1\n"
        )
        self.assert_code(
            ErrorCode.ROOT_VIOLATION,
            lambda: load_format(text, self.snapshot),
            sent_id="ROOTX",
            token_id=1,
            field="root",
        )

    def test_raw_input_codes(self) -> None:
        self.assert_code(
            ErrorCode.DUP_SENT,
            lambda: load_annotated_samples(
                INJECTION / "raw_duplicate_sent.jsonl",
                self.snapshot,
            ),
            sent_id="S001",
            field="sent_id",
        )
        self.assert_code(
            ErrorCode.TOKEN_SEQ,
            lambda: load_annotated_samples(
                INJECTION / "raw_token_sequence.jsonl",
                self.snapshot,
            ),
            sent_id="RAW_SEQ",
            token_id=3,
            field="id",
        )
        self.assert_code(
            ErrorCode.POS_INVALID,
            lambda: load_annotated_samples(
                INJECTION / "raw_pos_invalid.jsonl",
                self.snapshot,
            ),
            sent_id="RAW_POS",
            token_id=1,
            field="pos",
        )


class MigrationTests(InteropTestCase):
    def test_upgrade_fills_are_audited_and_replayable(self) -> None:
        result = migrate(self.v1, "v3", self.snapshot)
        fields = {record.field for record in result.fills}
        self.assertTrue(
            {
                "root_marker",
                "confidence_low",
                "confidence_high",
                "forward_check",
                "reverse_check",
            }.issubset(fields)
        )
        for record in result.fills:
            self.assertEqual(record.policy_version, self.snapshot.policy.version)
            self.assertEqual(record.policy_checksum, self.snapshot.policy.checksum)
            self.assertTrue(record.source)
        self.assertEqual(result.replay_differences(), [])

    def test_v2_to_v3_fills_checks_only(self) -> None:
        result = migrate(self.v2, "v3", self.snapshot)
        fields = {record.field for record in result.fills}
        self.assertEqual(
            fields,
            {
                "forward_check",
                "reverse_check",
                "bidirectional_arc_chains",
                "cross_sentence_refs",
            },
        )
        self.assertEqual(result.drops, [])
        self.assertEqual(result.replay_differences(), [])

    def test_v3_downgrade_drops_extended_semantics(self) -> None:
        result = migrate(self.rich_v3, "v1", self.snapshot)
        fields = {record.field for record in result.drops}
        self.assertTrue(
            {
                "root_marker",
                "confidence_low",
                "confidence_high",
                "forward_check",
                "reverse_check",
                "bidirectional_arc_chains",
                "cross_sentence_refs",
            }.issubset(fields)
        )
        self.assertEqual(result.graph.sentences["S001"].bidirectional_chains, [])
        self.assertEqual(result.graph.sentences["S001"].cross_sentence_refs, [])
        self.assertEqual(
            result.graph.core_signature(),
            result.source_graph.core_signature(),
        )
        self.assertEqual(result.replay_differences(), [])

    def test_downgrade_does_not_mutate_source(self) -> None:
        before = self.rich_v3.graph.core_signature()
        chain_count = len(self.rich_v3.graph.sentences["S001"].bidirectional_chains)
        result = migrate(self.rich_v3, "v1", self.snapshot)
        self.assertEqual(self.rich_v3.graph.core_signature(), before)
        self.assertEqual(
            len(self.rich_v3.graph.sentences["S001"].bidirectional_chains),
            chain_count,
        )
        self.assertEqual(result.source_graph.core_signature(), before)

    def test_discard_rule_must_be_declared_by_policy(self) -> None:
        raw = json.loads(
            (CONFIG / "controlled_snapshot_v1.json").read_text(encoding="utf-8")
        )
        raw["migration_policy"]["discard_rules"]["downgrade_to_v1"].remove(
            "confidence_low"
        )
        raw["migration_policy"]["checksum"] = section_checksum(raw["migration_policy"])
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "snapshot.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            store = SnapshotStore()
            store.load(path)
            self.assert_code(
                ErrorCode.INCONSISTENT,
                lambda: migrate(self.rich_v3, "v1", store.current),
                category="DISCARD_RULE_MISSING",
                field="confidence_low",
                sent_id="S001",
                token_id=1,
            )

    def test_all_nine_migration_pairs_replay_cleanly(self) -> None:
        documents = {
            FormatVersion.V1: self.v1,
            FormatVersion.V2: self.v2,
            FormatVersion.V3: self.v3,
        }
        for source_version, document in documents.items():
            for target_version in FormatVersion:
                with self.subTest(source=source_version, target=target_version):
                    result = migrate(
                        document,
                        target_version,
                        self.snapshot,
                    )
                    self.assertEqual(result.replay_differences(), [])
                    self.assertEqual(
                        result.graph.core_signature(),
                        document.graph.core_signature(),
                    )


class EquivalenceAndRoundtripTests(InteropTestCase):
    def test_base_three_views_are_equivalent(self) -> None:
        result = judge_equivalence(
            {"v1": self.v1, "v2": self.v2, "v3": self.v3},
            self.snapshot,
        )
        self.assertEqual(result.arc_count, 42)
        self.assertEqual(result.sent_ids, ["S001", "S002", "S003", "S004", "S005"])
        self.assertTrue(result.reconstructed_fields)

    def test_semantic_drift_lists_fields_and_tokens(self) -> None:
        exc = self.assert_code(
            ErrorCode.SEMANTIC_DRIFT,
            lambda: judge_equivalence(
                {"v1": self.v1, "v2": self.v2, "v3": self.rich_v3},
                self.snapshot,
            ),
            sent_id="S001",
        )
        fields = set(exc.details["unrestorable_fields"])
        self.assertIn("confidence_low", fields)
        self.assertIn("bidirectional_arc_chains", fields)
        self.assertIn("cross_sentence_refs", fields)
        self.assertTrue(exc.details["tokens"])
        self.assertTrue(exc.details["drift"])

    def test_roundtrip_all_formats(self) -> None:
        for version in ("v1", "v2", "v3"):
            path = (
                SAMPLES / "arcs_v3.json"
                if version == "v3"
                else SAMPLES / f"arcs_{version}.txt"
            )
            with self.subTest(version=version):
                result = verify_roundtrip(path, self.snapshot)
                self.assertTrue(result.to_dict()["consistent"])

    def test_rich_v3_roundtrip_preserves_extras(self) -> None:
        result = verify_roundtrip(
            INJECTION / "semantic_drift_v3.json",
            self.snapshot,
        )
        self.assertEqual(result.version, FormatVersion.V3)

    def test_roundtrip_precision_mismatch(self) -> None:
        self.assert_code(
            ErrorCode.ROUNDTRIP_MISMATCH,
            lambda: verify_roundtrip(
                INJECTION / "roundtrip_precision_v2.txt",
                self.snapshot,
                confidence_tolerance=1e-12,
            ),
            format_version="v2",
            sent_id="R001",
            token_id=1,
            field="confidence_low",
        )


class SnapshotTests(InteropTestCase):
    def test_corrupt_checksum(self) -> None:
        self.assert_code(
            ErrorCode.SNAPSHOT_CORRUPT,
            lambda: SnapshotStore().load(CONFIG / "controlled_snapshot_corrupt.json"),
            field="deprels.checksum",
            category="CHECKSUM_MISMATCH",
        )

    def test_atomic_replacement_of_all_sections(self) -> None:
        store = SnapshotStore()
        store.load(CONFIG / "controlled_snapshot_v1.json")
        store.load(CONFIG / "controlled_snapshot_v2.json")
        self.assertEqual(len(store.history), 2)
        self.assertEqual(store.current.pos_tags.version, 2)
        self.assertEqual(store.current.deprels.version, 2)
        self.assertEqual(store.current.policy.version, 2)

    def test_version_rollback_is_rejected(self) -> None:
        store = SnapshotStore()
        store.load(CONFIG / "controlled_snapshot_v2.json")
        self.assert_code(
            ErrorCode.SNAPSHOT_CORRUPT,
            lambda: store.load(CONFIG / "controlled_snapshot_v1.json"),
            category="VERSION_ROLLBACK",
        )

    def test_same_version_changed_checksum_is_rejected(self) -> None:
        raw = json.loads(
            (CONFIG / "controlled_snapshot_v1.json").read_text(encoding="utf-8")
        )
        raw["migration_policy"]["replacement_note"] = "unversioned change"
        raw["migration_policy"]["checksum"] = section_checksum(raw["migration_policy"])
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "snapshot.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            store = SnapshotStore()
            store.load(CONFIG / "controlled_snapshot_v1.json")
            self.assert_code(
                ErrorCode.SNAPSHOT_CORRUPT,
                lambda: store.load(path),
                category="UNVERSIONED_REPLACEMENT",
            )

    def test_label_retirement_lists_every_affected_arc(self) -> None:
        store = SnapshotStore()
        store.load(CONFIG / "controlled_snapshot_v1.json")
        graphs = [load_format(SAMPLES / "arcs_v1.txt", store.current).graph]
        exc = self.assert_code(
            ErrorCode.LABEL_RETIRED,
            lambda: store.load(
                CONFIG / "controlled_snapshot_retired_v3.json",
                existing_graphs=graphs,
            ),
        )
        affected = exc.details["affected"]
        self.assertTrue(affected)
        self.assertTrue(
            all(
                {"sent_id", "token_id", "deprel", "format_version"} <= set(item)
                for item in affected
            )
        )
        self.assertEqual(store.current.deprels.version, 1)


class ResourceAndSelfCheckTests(InteropTestCase):
    def test_strict_resource_limit(self) -> None:
        self.assert_code(
            ErrorCode.RESOURCE_LIMIT,
            lambda: load_annotated_samples(
                SAMPLES / "dependency_samples.jsonl",
                self.snapshot,
                ResourceLimits(max_total_nodes=20, max_tokens_per_sentence=9),
            ),
            field="node_count",
            limit=20,
            degradation_available="stream",
        )

    def test_per_sentence_limit_is_not_streamable(self) -> None:
        self.assert_code(
            ErrorCode.RESOURCE_LIMIT,
            lambda: load_annotated_samples(
                SAMPLES / "dependency_samples.jsonl",
                self.snapshot,
                ResourceLimits(
                    max_total_nodes=1000,
                    max_tokens_per_sentence=5,
                    mode="stream",
                ),
            ),
            field="token_count",
            sent_id="S001",
        )

    def test_streaming_degradation_preserves_arcs(self) -> None:
        baseline = load_annotated_samples(
            SAMPLES / "dependency_samples.jsonl",
            self.snapshot,
            ResourceLimits(max_total_nodes=10_000, max_tokens_per_sentence=512),
        )
        streamed = stream_migrate_samples(
            SAMPLES / "dependency_samples.jsonl",
            "v2",
            self.snapshot,
            limits=ResourceLimits(
                mode="stream",
                max_total_nodes=20,
                max_tokens_per_sentence=9,
            ),
        )
        self.assertTrue(streamed.degraded)
        self.assertEqual(
            streamed.degradation.category,
            "TOTAL_NODE_LIMIT_STREAMING",
        )
        baseline_signatures = {
            graph.sent_ids()[0]: graph.sentences[graph.sent_ids()[0]].core_signature()
            for graph in baseline.graphs
        }
        for chunk in streamed.chunks:
            self.assertEqual(
                chunk.migration.graph.sentences[chunk.sent_id].core_signature(),
                baseline_signatures[chunk.sent_id],
            )

    def test_selfcheck_passes_on_consistent_views(self) -> None:
        result = run_self_check(
            {
                "v1": SAMPLES / "arcs_v1.txt",
                "v2": SAMPLES / "arcs_v2.txt",
                "v3": SAMPLES / "arcs_v3.json",
            },
            self.store,
        )
        self.assertEqual(
            {check["category"] for check in result.checks},
            {"SNAPSHOT_TRACE", "FORMAT_VIEW_ARCS", "AUDIT_REPLAY"},
        )
        self.assertTrue(all(check["status"] == "pass" for check in result.checks))
        self.assertEqual(result.replay_pairs, 9)

    def test_selfcheck_wraps_inconsistency(self) -> None:
        self.assert_code(
            ErrorCode.INCONSISTENT,
            lambda: run_self_check(
                {
                    "v1": SAMPLES / "arcs_v1.txt",
                    "v2": SAMPLES / "arcs_v2.txt",
                    "v3": INJECTION / "semantic_drift_v3.json",
                },
                self.store,
            ),
            category="FORMAT_VIEW_ARCS",
            sent_id="S001",
            format_version="v1/v2/v3",
        )


class DeterminismAndDemoTests(InteropTestCase):
    def _hash_generated_tree(self) -> str:
        digest = hashlib.sha256()
        for directory in (SAMPLES, CONFIG, INJECTION):
            for path in sorted(directory.glob("*")):
                if path.is_file():
                    digest.update(path.name.encode("utf-8"))
                    digest.update(path.read_bytes())
        return digest.hexdigest()

    def test_generator_is_deterministic(self) -> None:
        from scripts.generate_samples import generate

        generate()
        first = self._hash_generated_tree()
        generate()
        second = self._hash_generated_tree()
        self.assertEqual(first, second)

    def test_complete_demo_has_no_failures(self) -> None:
        report = run_demo(DATA)
        self.assertTrue(report["all_passed"])
        self.assertEqual(
            sum(1 for item in report["injection_results"] if item["passed"]),
            20,
        )
        self.assertTrue(report["resource_limit"]["stream_arcs_preserved"])
        self.assertEqual(len(report["migrations"]), 9)
        self.assertTrue(
            all(item["replay_differences"] == 0 for item in report["migrations"])
        )


if __name__ == "__main__":
    unittest.main()
