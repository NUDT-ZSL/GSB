from __future__ import annotations

import json
import hashlib
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from kgpipe.adaptive import decide_path
from kgpipe.config import effective_config, load_config
from kgpipe.corpus import load_corpus
from kgpipe.dictionary import EntityDictionary
from kgpipe.errors import KGError
from kgpipe.generate import generate_sample_data
from kgpipe.models import Document, Sentence
from kgpipe.normalization import recompute_graph_stats
from kgpipe.paths import compare_candidate_sets
from kgpipe.pipeline import run_pipeline
from kgpipe.recognition import recognize_document
from kgpipe.relation import RelationSchema, extract_relations
from kgpipe.snapshot import SnapshotStore
from kgpipe.util import (
    atomic_write_json,
    attach_checksum,
    canonical_json,
    read_json,
)
from kgpipe.verifier import verify_execution_report


class PipelineTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.data = cls.root / "data"
        cls.metadata = generate_sample_data(cls.data)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def out(self, name: str) -> Path:
        return self.root / name

    def read_report(self, path: Path) -> dict:
        return json.loads((path / "report.json").read_text(encoding="utf-8"))


class GenerationAndIngestTests(PipelineTestCase):
    def test_generation_is_byte_deterministic(self) -> None:
        second = self.root / "data-second"
        generate_sample_data(second, seed=20261121)
        first_files = sorted(
            path.relative_to(self.data)
            for path in self.data.rglob("*")
            if path.is_file()
        )
        second_files = sorted(
            path.relative_to(second) for path in second.rglob("*") if path.is_file()
        )
        self.assertEqual(first_files, second_files)
        for relative in first_files:
            self.assertEqual(
                (self.data / relative).read_bytes(),
                (second / relative).read_bytes(),
                msg=str(relative),
            )

    def test_ingest_rejects_invalid_char_len(self) -> None:
        broken = self.root / "broken-data"
        shutil.copytree(self.data, broken)
        shard = sorted((broken / "shards").glob("*.jsonl"))[0]
        rows = [
            json.loads(line)
            for line in shard.read_text(encoding="utf-8").splitlines()
        ]
        rows[0]["char_len"] += 1
        shard.write_text(
            "".join(canonical_json(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        report = run_pipeline(
            data_dir=broken,
            out_dir=self.out("broken-ingest"),
        )
        self.assertEqual(report["status"], "error")
        self.assertIn("E_INGEST", [item["code"] for item in report["errors"]])

    def test_pipeline_span_sets_are_byte_stable_across_runs(self) -> None:
        first = run_pipeline(
            data_dir=self.data,
            out_dir=self.out("span-stable-first"),
            overrides={"pair_budget": 12000},
        )
        second = run_pipeline(
            data_dir=self.data,
            out_dir=self.out("span-stable-second"),
            overrides={"pair_budget": 12000},
        )
        self.assertEqual(
            canonical_json(
                first["stages"]["entity_recognition"]["documents"]
            ),
            canonical_json(
                second["stages"]["entity_recognition"]["documents"]
            ),
        )


class DictionaryTests(PipelineTestCase):
    def _minimal_dictionary(
        self,
        *,
        aliases: list[str],
        normalization_map: dict[str, str],
    ) -> dict:
        return attach_checksum(
            {
                "dict_version": 1,
                "entries": [
                    {
                        "entity_id": "E1",
                        "canonical_name": "实体",
                        "type": "PERSON",
                        "type_id": 10,
                        "aliases": aliases,
                    }
                ],
                "normalization_map": normalization_map,
            }
        )

    def test_alias_conflict_is_reported_with_alias(self) -> None:
        raw = read_json(self.data / "dictionary.json")
        raw.pop("checksum")
        raw["entries"][1]["aliases"].append("张三")
        with self.assertRaises(KGError) as raised:
            EntityDictionary(attach_checksum(raw))
        self.assertEqual(raised.exception.code, "E_ALIAS_CONFLICT")
        self.assertEqual(raised.exception.alias, "张三")

    def test_normalization_cycle_and_self_reference(self) -> None:
        with self.assertRaises(KGError) as cycle_error:
            EntityDictionary(
                self._minimal_dictionary(
                    aliases=["甲", "乙"],
                    normalization_map={"甲": "乙", "乙": "甲"},
                )
            )
        self.assertEqual(cycle_error.exception.code, "E_NORMALIZE_CYCLE")
        with self.assertRaises(KGError) as self_error:
            EntityDictionary(
                self._minimal_dictionary(
                    aliases=["甲"],
                    normalization_map={"甲": "甲"},
                )
            )
        self.assertEqual(self_error.exception.code, "E_NORMALIZE_CYCLE")
        self.assertEqual(self_error.exception.alias, "甲")

    def test_entity_matching_offsets_longest_match_and_determinism(self) -> None:
        dictionary = EntityDictionary.load(self.data / "dictionary.json")
        document = Document(
            doc_id="doc-test",
            shard_ids=["shard-test"],
            sentences=[
                Sentence(
                    shard_id="shard-test",
                    doc_id="doc-test",
                    sent_no=0,
                    text="老张任职于国防科大，随后前往长沙市。",
                    char_len=21,
                )
            ],
        )
        first = recognize_document(document, dictionary)
        second = recognize_document(document, dictionary)
        self.assertEqual(
            canonical_json([span.to_dict() for span in first.spans]),
            canonical_json([span.to_dict() for span in second.spans]),
        )
        surfaces = [span.text for span in first.spans]
        self.assertIn("老张", surfaces)
        self.assertIn("国防科大", surfaces)
        self.assertIn("长沙市", surfaces)
        self.assertNotIn("长沙", surfaces)
        for span in first.spans:
            self.assertEqual(
                document.sentences[0].text[span.start : span.end], span.text
            )
            self.assertEqual(span.dictionary_version, dictionary.version)
        ranges = sorted((span.start, span.end) for span in first.spans)
        for left, right in zip(ranges, ranges[1:]):
            self.assertLessEqual(left[1], right[0])


class RelationAndAdaptiveTests(PipelineTestCase):
    def _loaded(self):
        corpus = load_corpus(self.data)
        dictionary = EntityDictionary.load(self.data / "dictionary.json")
        schema = RelationSchema.load(self.data / "relation_schema.json")
        entities = {
            document.doc_id: recognize_document(document, dictionary)
            for document in corpus.documents
        }
        return corpus, dictionary, schema, entities

    def test_relation_confidence_recomputes_from_components(self) -> None:
        corpus, _, schema, entities = self._loaded()
        document = next(item for item in corpus.documents if item.doc_id == "doc-0001")
        triples = extract_relations(
            document=document,
            entities=entities[document.doc_id],
            schema=schema,
            mode="exact",
            neighbor_limit=4,
        )
        self.assertGreater(len(triples), 0)
        for triple in triples:
            self.assertEqual(
                triple.confidence_bp,
                sum(component["basis_points"] for component in triple.score_components),
            )
            self.assertEqual(triple.confidence, round(triple.confidence_bp / 10000, 6))
            sentence = document.sentences[triple.sent_no]
            self.assertEqual(
                sentence.text[
                    triple.trigger_offset : triple.trigger_offset
                    + len(triple.trigger)
                ],
                triple.trigger,
            )
            self.assertLessEqual(triple.head_end, triple.trigger_offset)
            self.assertLessEqual(
                triple.trigger_offset + len(triple.trigger), triple.tail_start
            )
        expected_order = sorted(
            triples,
            key=lambda item: (
                -item.confidence_bp,
                item.head_start,
                item.tail_start,
                item.relation_type_id,
                item.sent_no,
                item.head_end,
                item.tail_end,
                item.pattern_line,
                item.trigger_offset,
            ),
        )
        self.assertEqual(
            [item.triple_id for item in triples],
            [item.triple_id for item in expected_order],
        )

    def test_adaptive_decision_is_byte_stable(self) -> None:
        corpus, _, schema, entities = self._loaded()
        config = effective_config(
            load_config(self.data / "config.json"),
            {"pair_budget": 300, "approx_neighbors": 3, "budget_policy": "degrade"},
        )
        first = decide_path(
            corpus=corpus,
            entities_by_doc=entities,
            schema=schema,
            config=config,
            requested_mode="auto",
        )
        second = decide_path(
            corpus=corpus,
            entities_by_doc=entities,
            schema=schema,
            config=config,
            requested_mode="auto",
        )
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(first["decision_sha256"], second["decision_sha256"])
        self.assertEqual(first["chosen_path"], "approx")

    def test_budget_degrade_and_error_modes(self) -> None:
        degraded = run_pipeline(
            data_dir=self.data,
            out_dir=self.out("budget-degrade"),
            requested_mode="auto",
            overrides={
                "pair_budget": 300,
                "approx_neighbors": 3,
                "budget_policy": "degrade",
            },
        )
        self.assertEqual(degraded["status"], "degraded")
        self.assertEqual(degraded["selection"]["chosen_path"], "approx")
        self.assertEqual(
            degraded["selection"]["degradation"]["error_code"], "E_PAIR_BUDGET"
        )
        self.assertIn("doc-0002", degraded["selection"]["degradation"]["affected_docs"])
        self.assertTrue(degraded["self_check"]["valid"])

        strict = run_pipeline(
            data_dir=self.data,
            out_dir=self.out("budget-error"),
            requested_mode="auto",
            overrides={"pair_budget": 300, "budget_policy": "error"},
        )
        self.assertEqual(strict["status"], "error")
        self.assertEqual(strict["errors"][0]["code"], "E_PAIR_BUDGET")

    def test_neighbor_shrink_reduces_batch_size(self) -> None:
        report = run_pipeline(
            data_dir=self.data,
            out_dir=self.out("neighbor-shrink"),
            requested_mode="approx",
            overrides={
                "pair_budget": 230,
                "approx_neighbors": 3,
                "budget_policy": "degrade",
            },
        )
        self.assertEqual(report["status"], "degraded")
        self.assertEqual(report["selection"]["neighbor_limit"], 1)
        self.assertIn(
            "approx_neighbors_reduced_to_1", report["selection"]["reasons"]
        )

    def test_memory_limit_returns_resource_error(self) -> None:
        report = run_pipeline(
            data_dir=self.data,
            out_dir=self.out("memory-error"),
            requested_mode="auto",
            overrides={
                "pair_budget": 1000000,
                "memory_limit_mb": 1,
                "reserved_memory_mb": 0,
                "bytes_per_pair": 300000,
                "budget_policy": "error",
            },
        )
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["errors"][0]["code"], "E_RESOURCE_LIMIT")

    def test_cross_path_difference_and_confidence_deviation(self) -> None:
        corpus, _, schema, entities = self._loaded()
        document = next(item for item in corpus.documents if item.doc_id == "doc-0002")
        exact = extract_relations(
            document=document,
            entities=entities[document.doc_id],
            schema=schema,
            mode="exact",
            neighbor_limit=2,
        )
        approx = extract_relations(
            document=document,
            entities=entities[document.doc_id],
            schema=schema,
            mode="approx",
            neighbor_limit=2,
        )
        comparison = compare_candidate_sets(
            doc_id=document.doc_id,
            exact=exact,
            approximate=approx,
            mandatory_pattern_lines=[1],
        )
        self.assertFalse(comparison["statistics_comparable"])
        self.assertGreater(len(comparison["missing_in_approx"]), 0)
        self.assertLess(comparison["recall_estimate"], 1.0)
        self.assertIn(
            "APPROXIMATE_ENUMERATION_OMISSION",
            comparison["difference_causes"],
        )

        common_key = {
            (
                item.sent_no,
                item.head_span_id,
                item.tail_span_id,
                item.relation_type,
            )
            for item in exact
        } & {
            (
                item.sent_no,
                item.head_span_id,
                item.tail_span_id,
                item.relation_type,
            )
            for item in approx
        }
        self.assertTrue(common_key)
        target = next(
            item
            for item in approx
            if (
                item.sent_no,
                item.head_span_id,
                item.tail_span_id,
                item.relation_type,
            )
            in common_key
        )
        deviated = [
            replace(item, confidence_bp=item.confidence_bp - 1)
            if item.triple_id == target.triple_id
            else item
            for item in approx
        ]
        mismatch = compare_candidate_sets(
            doc_id=document.doc_id,
            exact=exact,
            approximate=deviated,
            mandatory_pattern_lines=[1],
        )
        self.assertGreater(len(mismatch["confidence_deviations"]), 0)
        self.assertIn("PATTERN_SEMANTICS_MISMATCH", mismatch["difference_causes"])

    def test_both_mode_returns_path_divergence(self) -> None:
        report = run_pipeline(
            data_dir=self.data,
            out_dir=self.out("path-divergence"),
            requested_mode="both",
            overrides={
                "pair_budget": 12000,
                "approx_neighbors": 2,
                "budget_policy": "error",
            },
        )
        self.assertEqual(report["status"], "error")
        self.assertIn(
            "E_PATH_DIVERGENCE", [item["code"] for item in report["errors"]]
        )
        self.assertEqual(
            report["cross_path"]["first_divergence_doc_id"], "doc-0002"
        )
        self.assertIsNotNone(report["cross_path"]["first_divergence_triple"])


class NormalizationSnapshotAndVerificationTests(PipelineTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.baseline_dir = cls.root / "baseline"
        cls.baseline = run_pipeline(
            data_dir=cls.data,
            out_dir=cls.baseline_dir,
            requested_mode="auto",
            overrides={"pair_budget": 12000},
        )

    def test_baseline_truth_f1_and_normalization_aliases(self) -> None:
        self.assertEqual(self.baseline["status"], "ok")
        self.assertEqual(self.baseline["evaluation"]["f1"], 1.0)
        self.assertTrue(self.baseline["self_check"]["valid"])
        normalized = self.baseline["stages"]["normalization"]["documents"]["doc-0003"]
        alias_map = normalized["alias_to_canonical"]
        self.assertEqual(alias_map["老张"], "PER_ZHANG")
        self.assertEqual(alias_map["张先生"], "PER_ZHANG")
        for entity in normalized["canonical_entities"]:
            span_surfaces = sorted(
                {
                    span["text"]
                    for span in entity["spans"]
                }
            )
            self.assertEqual(entity["aliases"], span_surfaces)
        keys = [
            (
                triple["head_entity_id"],
                triple["tail_entity_id"],
                triple["relation_type"],
            )
            for triple in normalized["triples"]
        ]
        self.assertEqual(len(keys), len(set(keys)))

    def test_graph_statistics_recompute_independently(self) -> None:
        corpus = load_corpus(self.data)
        normalized_docs = self.baseline["stages"]["normalization"]["documents"]
        recomputed = recompute_graph_stats(corpus, normalized_docs)
        self.assertEqual(
            canonical_json(recomputed),
            canonical_json(self.baseline["stages"]["graph"]["statistics"]),
        )

    def test_snapshot_whole_replace_and_corruption(self) -> None:
        store = SnapshotStore(self.root / "snapshot-store")
        first = store.install(
            "graph_stats", 1, {"graph_version": 1, "triple_count": 3}
        )
        second = store.install(
            "graph_stats", 2, {"graph_version": 2, "triple_count": 4}
        )
        self.assertEqual(first["mode"], "create")
        self.assertEqual(second["mode"], "whole_replace")
        self.assertFalse((store.root / "graph_stats-v000001.json").exists())
        self.assertEqual(store.load("graph_stats")["graph_version"], 2)

        with self.assertRaises(KGError) as version_error:
            store.install("graph_stats", 1, {"graph_version": 1})
        self.assertEqual(version_error.exception.code, "E_SNAPSHOT_VERSION")

        with self.assertRaises(KGError) as same_version_error:
            store.install(
                "graph_stats", 2, {"graph_version": 2, "triple_count": 999}
            )
        self.assertEqual(same_version_error.exception.code, "E_SNAPSHOT_CORRUPT")

        entry = json.loads(
            (store.root / "manifest.json").read_text(encoding="utf-8")
        )["entries"]["graph_stats"]
        path = store.root / entry["file"]
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        wrapper["payload"]["triple_count"] = 999
        path.write_text(
            json.dumps(wrapper, ensure_ascii=False),
            encoding="utf-8",
        )
        with self.assertRaises(KGError) as corrupt_error:
            store.load("graph_stats")
        self.assertEqual(corrupt_error.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(
            corrupt_error.exception.details["snapshot_category"], "graph_stats"
        )
        self.assertEqual(corrupt_error.exception.details["version"], 2)

        payload_store = SnapshotStore(self.root / "payload-checksum-store")
        payload_store.install(
            "graph_stats", 1, {"graph_version": 1, "triple_count": 1}
        )
        manifest_path = payload_store.root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload_path = payload_store.root / manifest["entries"]["graph_stats"]["file"]
        payload_wrapper = json.loads(payload_path.read_text(encoding="utf-8"))
        payload_wrapper["payload"]["triple_count"] = 2
        payload_path.write_text(
            json.dumps(payload_wrapper, ensure_ascii=False),
            encoding="utf-8",
        )
        manifest["entries"]["graph_stats"]["file_sha256"] = hashlib.sha256(
            payload_path.read_bytes()
        ).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        with self.assertRaises(KGError) as payload_error:
            payload_store.load("graph_stats")
        self.assertEqual(payload_error.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertIn(
            "expected_checksum", payload_error.exception.details
        )

    def test_verifier_detects_report_tampering(self) -> None:
        report = self.read_report(self.baseline_dir)
        tampered = json.loads(json.dumps(report, ensure_ascii=False))
        normalized = tampered["stages"]["normalization"]["documents"]["doc-0001"]
        normalized["triples"][0]["confidence_bp"] += 1
        verification = verify_execution_report(tampered, self.data)
        self.assertFalse(verification["valid"])
        categories = {
            issue.get("category")
            for issue in verification["issues"]
        }
        self.assertIn("NORMALIZATION_MISMATCH", categories)
        self.assertTrue(
            any(issue.get("doc_id") == "doc-0001" for issue in verification["issues"])
        )
        for issue in verification["issues"]:
            self.assertEqual(issue["code"], "E_INCONSISTENT")

        stats_tampered = json.loads(json.dumps(report, ensure_ascii=False))
        stats_tampered["stages"]["graph"]["statistics"]["triple_count"] = 999
        stats_verification = verify_execution_report(stats_tampered, self.data)
        self.assertFalse(stats_verification["valid"])
        self.assertIn(
            "GRAPH_STATS",
            {
                issue.get("category")
                for issue in stats_verification["issues"]
            },
        )

    def test_approximate_path_preserves_mandatory_hits(self) -> None:
        report = run_pipeline(
            data_dir=self.data,
            out_dir=self.out("mandatory-degrade"),
            requested_mode="auto",
            overrides={
                "pair_budget": 300,
                "approx_neighbors": 2,
                "budget_policy": "degrade",
            },
        )
        self.assertEqual(report["status"], "degraded")
        self.assertTrue(report["self_check"]["valid"])
        self.assertTrue(
            report["stages"]["relation_extraction"]["mandatory_preservation"][
                "preserved"
            ]
        )


if __name__ == "__main__":
    unittest.main()

