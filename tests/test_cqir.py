from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from cqir.arbitration import arbitrate
from cqir.config import PipelineConfig
from cqir.demo import run_demo
from cqir.errors import AppError
from cqir.formats import parse_answer, serialize, serialize_v1, serialize_v3
from cqir.generation import build_samples, write_samples, load_documents, load_queries
from cqir.migration import migrate_answer
from cqir.models import AnswerPackage, Citation, Document, QueryTurn
from cqir.parallel import run_batch
from cqir.pipeline import IncrementalEngine, Pipeline
from cqir.retrieval import merge_recall
from cqir.selfcheck import consistency_check
from cqir.snippets import recompute_detail_score, validate_score_detail
from cqir.snapshot import VersionedSnapshotStore
from cqir.text import normalize_query
from cqir.util import canonical_json


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="cqir-test-")
        self.workspace = Path(self.temp.name)
        self.queries, self.documents, self.bumped, self.change_set = build_samples()
        self.query = next(
            item
            for item in self.queries
            if item.conv_id == "conv-002" and item.turn_no == 1
        )
        self.config = PipelineConfig()
        self.pipeline = Pipeline(
            config=self.config,
            cache_root=self.workspace / "cache",
        )
        self.baseline = self.pipeline.run(
            self.query,
            self.queries,
            self.documents,
            force_recompute=True,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assert_error(self, code: str, action, **detail_checks) -> AppError:
        with self.assertRaises(AppError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        for key, value in detail_checks.items():
            self.assertEqual(caught.exception.details.get(key), value)
        return caught.exception


class DeterminismTests(BaseCase):
    def test_same_query_and_config_are_byte_reproducible(self) -> None:
        second = Pipeline(
            config=self.config,
            cache_root=self.workspace / "cache-two",
        )
        result = second.run(self.query, self.queries, self.documents)
        self.assertEqual(self.baseline.canonical_answer, result.canonical_answer)
        self.assertEqual(serialize(self.baseline.package, 3), serialize(result.package, 3))
        self.assertEqual(self.baseline.byte_hash, result.byte_hash)
        self.assertEqual(
            canonical_json(self.baseline.recall),
            canonical_json(result.recall),
        )

    def test_recall_merge_tie_breaks_by_doc_id(self) -> None:
        normalized = normalize_query(self.query, self.queries, self.config)
        route_scores = {
            "inverted": {
                "doc-007": 1.0,
                "doc-001": 1.0,
                "doc-003": 0.2,
                "doc-004": 0.2,
            },
            "category_popular": {},
            "vector_approx": {},
        }
        merged = merge_recall(
            route_scores,
            self.documents,
            normalized,
            self.config,
        )
        self.assertEqual(merged["top_doc_ids"], ["doc-001", "doc-007", "doc-003", "doc-004"])
        self.assertEqual(merged["candidates"][0]["routes"][0]["route"], "inverted")

    def test_snippet_score_detail_recomputes(self) -> None:
        self.assertGreater(len(self.baseline.selected), 0)
        for item in self.baseline.selected:
            validate_score_detail(item["score_detail"])
            self.assertEqual(
                recompute_detail_score(item["score_detail"]),
                item["score_detail"]["final_score"],
            )

    def test_parallel_process_pool_matches_sequential(self) -> None:
        selected = [item for item in self.queries if item.conv_id == "conv-001"][:2]
        parallel = run_batch(
            selected,
            self.queries,
            self.documents,
            self.config,
            self.workspace / "parallel-a",
            workers=2,
            force_recompute=True,
        )
        sequential = run_batch(
            selected,
            self.queries,
            self.documents,
            self.config,
            self.workspace / "parallel-b",
            workers=1,
            force_recompute=True,
        )
        self.assertEqual(parallel, sequential)


class SpanAndCitationTests(BaseCase):
    def test_span_out_of_range(self) -> None:
        citation = self.baseline.package.citations[0]
        document = self.documents[citation.doc_id]
        broken = replace(
            self.baseline.package,
            citations=(replace(citation, end=len(document.text) + 1),),
            snippets=(self.baseline.package.snippets[0],),
        )
        self.assert_error(
            "E_SPAN_OUT_OF_RANGE",
            lambda: broken.validate(self.documents),
            doc_id=citation.doc_id,
        )

    def test_duplicate_citation(self) -> None:
        citation = self.baseline.package.citations[0]
        snippet = self.baseline.package.snippets[0]
        broken = replace(
            self.baseline.package,
            citations=(citation, citation),
            snippets=(snippet, snippet),
        )
        self.assert_error("E_CITE_DUP", lambda: broken.validate(self.documents))

    def test_all_citations_are_within_document_codepoints(self) -> None:
        for citation in self.baseline.package.citations:
            document = self.documents[citation.doc_id]
            self.assertLessEqual(0, citation.start)
            self.assertLess(citation.start, citation.end)
            self.assertLessEqual(citation.end, len(document.text))
            self.assertEqual(
                document.text[citation.start : citation.end],
                self.baseline.package.snippets[
                    self.baseline.package.citations.index(citation)
                ].text,
            )


class FormatTests(BaseCase):
    def test_parse_all_three_formats(self) -> None:
        for version in (1, 2, 3):
            text = serialize(self.baseline.package, version)
            parsed = parse_answer(text, self.documents, expected_format=version)
            self.assertEqual(parsed.format_version, version)
            self.assertEqual(
                [(item.doc_id, item.start, item.end) for item in parsed.package.citations],
                [(item.doc_id, item.start, item.end) for item in self.baseline.package.citations],
            )

    def test_field_set_dispatch_without_headers(self) -> None:
        v1_lines = serialize(self.baseline.package, 1).splitlines()[1:]
        parsed_v1 = parse_answer("\n".join(v1_lines) + "\n", self.documents)
        self.assertEqual(parsed_v1.format_version, 1)
        self.assertEqual(parsed_v1.dispatch, "field_set")

        v2_lines = serialize(self.baseline.package, 2).splitlines()[1:]
        parsed_v2 = parse_answer("\n".join(v2_lines) + "\n", self.documents)
        self.assertEqual(parsed_v2.format_version, 2)
        self.assertEqual(parsed_v2.dispatch, "field_set")

        v3_value = json.loads(serialize(self.baseline.package, 3))
        del v3_value["format"]
        parsed_v3 = parse_answer(canonical_json(v3_value), self.documents)
        self.assertEqual(parsed_v3.format_version, 3)
        self.assertEqual(parsed_v3.dispatch, "field_set")

    def test_unknown_format_version(self) -> None:
        self.assert_error(
            "E_FORMAT_VERSION_UNSUPPORTED",
            lambda: parse_answer("CQIR-ANSWER/7\n", self.documents),
            format_version=7,
            line=1,
        )

    def test_truncated_header(self) -> None:
        self.assert_error(
            "E_FORMAT_CORRUPT",
            lambda: parse_answer("CQIR-AN\n", self.documents),
            field="header",
            line=1,
        )

    def test_pipe_field_count_mismatch_reports_line(self) -> None:
        text = "CQIR-ANSWER/1\ninvalid|0|1\n"
        error = self.assert_error(
            "E_FORMAT_CORRUPT",
            lambda: parse_answer(text, self.documents),
            field="citation",
            line=2,
        )
        self.assertEqual(error.details["actual_fields"], 3)
        self.assertEqual(error.details["expected_fields"], 4)

    def test_missing_required_field(self) -> None:
        error = self.assert_error(
            "E_FORMAT_CORRUPT",
            lambda: parse_answer("CQIR-ANSWER/1\n", self.documents),
            field="conv_id",
        )
        self.assertEqual(error.details["line"], 1)

    def test_numeric_parse_error_reports_field_and_line(self) -> None:
        text = serialize(self.baseline.package, 1)
        lines = text.splitlines()
        citation_index = next(
            index for index, line in enumerate(lines) if "|" in line and "=" not in line
        )
        parts = lines[citation_index].split("|")
        parts[3] = "not-a-number"
        lines[citation_index] = "|".join(parts)
        error = self.assert_error(
            "E_FORMAT_CORRUPT",
            lambda: parse_answer("\n".join(lines) + "\n", self.documents),
            field="score",
            line=citation_index + 1,
        )
        self.assertEqual(error.details["value"], "not-a-number")

    def test_answer_checksum_corruption(self) -> None:
        lines = serialize(self.baseline.package, 1).splitlines()
        lines[4] = "answer_sha256=" + "0" * 64
        self.assert_error(
            "E_FORMAT_CORRUPT",
            lambda: parse_answer("\n".join(lines) + "\n", self.documents),
            field="answer_sha256",
        )


class MigrationAndArbitrationTests(BaseCase):
    def test_all_directed_migrations_preserve_spans(self) -> None:
        for source in (1, 2, 3):
            source_text = serialize(self.baseline.package, source)
            for target in (1, 2, 3):
                if source == target:
                    continue
                result = migrate_answer(
                    source_text,
                    target,
                    self.documents,
                    self.queries,
                    self.config,
                )
                self.assertTrue(result["span_check"]["ok"])
                self.assertEqual(
                    result["span_check"]["source_keys"],
                    result["span_check"]["target_keys"],
                )

    def test_v3_to_v1_records_context_and_revision_drops(self) -> None:
        result = migrate_answer(
            serialize(self.baseline.package, 3),
            1,
            self.documents,
            self.queries,
            self.config,
        )
        fields = {item["field"] for item in result["drops"]}
        self.assertIn("revision", fields)
        self.assertIn("context_refs", fields)
        self.assertIn("context_chain", fields)
        self.assertGreater(len(result["drops"]), 0)

    def test_v1_to_v3_records_each_fill_source(self) -> None:
        result = migrate_answer(
            serialize(self.baseline.package, 1),
            3,
            self.documents,
            self.queries,
            self.config,
        )
        fields = {item["field"] for item in result["fills"]}
        self.assertTrue(
            {"confidence_low", "confidence_high", "paragraph_role", "order_weight", "context_refs", "revision"}
            <= fields
        )
        for item in result["fills"]:
            self.assertTrue(item["source"])

    def test_cross_format_arbitration_success(self) -> None:
        texts = {
            f"v{version}": serialize(self.baseline.package, version)
            for version in (1, 2, 3)
        }
        report = arbitrate(texts, self.documents, self.queries, self.config)
        self.assertTrue(report["ok"])
        self.assertEqual(report["formats"], ["v1", "v2", "v3"])

    def test_v1_semantic_incompleteness_is_not_silent(self) -> None:
        rich = replace(
            self.baseline.package,
            citations=tuple(
                replace(citation, revision=4)
                for citation in self.baseline.package.citations
            ),
        )
        with self.assertRaises(AppError) as caught:
            arbitrate(
                {"v1": serialize(rich, 1), "v3": serialize(rich, 3)},
                self.documents,
                self.queries,
                self.config,
            )
        self.assertEqual(caught.exception.code, "E_SEMANTIC_INCOMPLETE")
        self.assertIn("unrecoverable", caught.exception.details)
        self.assertTrue(
            any(
                item.get("field") == "revision"
                for item in caught.exception.details["unrecoverable"]
            )
        )


class IncrementalTests(BaseCase):
    def test_update_and_delete_match_fresh_recompute(self) -> None:
        engine = IncrementalEngine(self.pipeline)
        incremental = engine.run(
            self.query,
            self.queries,
            self.bumped,
            previous_result=self.baseline,
            changed_doc_ids=["doc-002"],
            deleted_doc_ids=["doc-005"],
        )
        fresh = Pipeline(
            config=self.config,
            cache_root=self.workspace / "fresh",
        ).run(
            self.query,
            self.queries,
            self.bumped,
            force_recompute=True,
        )
        self.assertEqual(
            incremental["result"].canonical_answer,
            fresh.canonical_answer,
        )
        self.assertEqual(
            serialize(incremental["result"].package, 3),
            serialize(fresh.package, 3),
        )
        report = incremental["report"]
        self.assertIn("doc-002", report["changed_docs"])
        self.assertIn("doc-005", report["removed_docs"])
        self.assertTrue(report["reuse_set"]["stages"])
        self.assertTrue(report["recompute_set"]["stages"])
        self.assertTrue(
            any("doc-002" in stage for stage in report["recompute_set"]["stages"])
        )
        self.assertFalse(
            set(map(tuple, report["reuse_set"]["citation_keys"]))
            & set(map(tuple, report["recompute_set"]["citation_keys"]))
        )

    def test_incremental_selfcheck(self) -> None:
        incremental = IncrementalEngine(self.pipeline).run(
            self.query,
            self.queries,
            self.bumped,
            previous_result=self.baseline,
            changed_doc_ids=["doc-002"],
            deleted_doc_ids=["doc-005"],
        )
        report = incremental["report"]
        check = consistency_check(
            incremental["result"].package,
            incremental["result"].stage_sets,
            retain_set=report["reuse_set"]["citation_keys"],
            evict_set=report["recompute_set"]["citation_keys"],
        )
        self.assertTrue(check["ok"])
        self.assertFalse(check["retain_evict_disjoint"] is False)


class SnapshotTests(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = VersionedSnapshotStore(self.workspace / "snapshots")

    def test_whole_snapshot_replace_and_load(self) -> None:
        first = self.store.replace("answer", "k", {"text": "one"}, 1)
        second = self.store.replace(
            "answer",
            "k",
            {"text": "two", "extra": True},
            2,
            expected_version=1,
            expected_checksum=first["checksum"],
        )
        loaded = self.store.load("answer", "k")
        self.assertEqual(loaded["version"], 2)
        self.assertNotEqual(first["checksum"], second["checksum"])
        self.assertEqual(loaded["payload"], {"text": "two", "extra": True})
        self.assertTrue(second["replaced_whole_snapshot"])

    def test_rollback_is_rejected(self) -> None:
        self.store.replace("answer", "k", {"text": "two"}, 2)
        self.assert_error(
            "E_SNAPSHOT_ROLLBACK",
            lambda: self.store.replace("answer", "k", {"text": "one"}, 1),
            current_version=2,
            requested_version=1,
        )

    def test_stale_checksum_is_rejected(self) -> None:
        self.store.replace("answer", "k", {"text": "one"}, 1)
        self.assert_error(
            "E_SNAPSHOT_ROLLBACK",
            lambda: self.store.replace(
                "answer",
                "k",
                {"text": "two"},
                2,
                expected_checksum="stale",
            ),
            expected_checksum="stale",
        )

    def test_corrupt_snapshot(self) -> None:
        self.store.replace("answer", "k", {"text": "one"}, 1)
        path = self.store._snapshot_path("answer", "k")
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["payload"]["text"] = "tampered"
        path.write_text(canonical_json(envelope) + "\n", encoding="utf-8")
        error = self.assert_error(
            "E_SNAPSHOT_CORRUPT",
            lambda: self.store.load("answer", "k"),
        )
        self.assertIn("actual_checksum", error.details)

    def test_concurrent_writer_gets_immediate_stage_busy(self) -> None:
        with self.store.guard("answer", "k"):
            started = time.monotonic()
            self.assert_error(
                "E_STAGE_BUSY",
                lambda: self.store.replace("answer", "k", {"text": "x"}, 1),
            )
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5)
        self.assertIsNone(self.store.load("answer", "k"))


class SelfCheckTests(BaseCase):
    def test_conv_id_inconsistency(self) -> None:
        stage_sets = {
            name: dict(value)
            for name, value in self.baseline.stage_sets.items()
        }
        stage_sets["normalized"]["conv_id"] = "conv-other"
        with self.assertRaises(AppError) as caught:
            consistency_check(self.baseline.package, stage_sets)
        self.assertEqual(caught.exception.code, "E_INCONSISTENT")
        self.assertIn(
            "conv_id_mismatch",
            caught.exception.details["categories"],
        )

    def test_retain_and_evict_overlap(self) -> None:
        key = list(self.baseline.package.citations[0].key)
        with self.assertRaises(AppError) as caught:
            consistency_check(
                self.baseline.package,
                self.baseline.stage_sets,
                retain_set=[key],
                evict_set=[key],
            )
        self.assertEqual(caught.exception.code, "E_INCONSISTENT")
        self.assertIn(
            "retain_evict_overlap",
            caught.exception.details["categories"],
        )


class DemoRepeatabilityTests(unittest.TestCase):
    def test_demo_is_repeatable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cqir-demo-") as temporary:
            workspace = Path(temporary)
            first = run_demo(workspace)
            second = run_demo(workspace)
            self.assertTrue(first["all_passed"])
            self.assertTrue(second["all_passed"])
            self.assertEqual(first["baseline"]["byte_hash"], second["baseline"]["byte_hash"])
            self.assertEqual(
                first["incremental"]["byte_hash"],
                second["incremental"]["byte_hash"],
            )
            self.assertEqual(
                first["snapshots"]["versions"],
                second["snapshots"]["versions"],
            )


if __name__ == "__main__":
    unittest.main()
