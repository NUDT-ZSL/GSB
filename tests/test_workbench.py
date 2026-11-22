from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from pathlib import Path

from app import create_app
from eventgraph.cache import ArtifactCache
from eventgraph.config import AppConfig
from eventgraph.engine import IncrementalEngine
from eventgraph.errors import BusinessError
from eventgraph.generator import DocumentGenerator
from eventgraph.parser import DocumentParser
from eventgraph.service import WorkbenchService
from eventgraph.similarity import SimilarityScorer
from eventgraph.snapshots import SnapshotStore
from eventgraph.utils import (
    atomic_write_json,
    canonical_json,
    content_hash,
    read_json,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class WorkbenchTestCase(unittest.TestCase):
    def make_root(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        shutil.copytree(PROJECT_ROOT / "data", root / "data")
        shutil.copytree(PROJECT_ROOT / "config", root / "config")
        return root

    def make_documents(self, root: Path) -> tuple[list[dict], dict[str, dict]]:
        manifest = DocumentGenerator(root).generate_all()
        documents = {
            item["doc_id"]: read_json(item["path"]) for item in manifest
        }
        return manifest, documents

    def make_engine(self, root: Path) -> tuple[IncrementalEngine, dict[str, dict]]:
        config = AppConfig.from_files(root)
        parser = DocumentParser(root, config.parser_version)
        cache = ArtifactCache(root / "runtime" / "cache")
        engine = IncrementalEngine(root, config, parser, cache)
        _, documents = self.make_documents(root)
        return engine, documents

    def make_service(self, root: Path) -> WorkbenchService:
        return WorkbenchService(root, clear_runtime=True)


class DeterminismAndParsingTests(WorkbenchTestCase):
    def test_generation_and_parsing_are_byte_deterministic(self) -> None:
        root = self.make_root()
        first = DocumentGenerator(root).generate_all(root / "out-a")
        second = DocumentGenerator(root).generate_all(root / "out-b")
        first_bytes = {
            item["doc_id"]: (Path(item["path"]).read_bytes()) for item in first
        }
        second_bytes = {
            item["doc_id"]: (Path(item["path"]).read_bytes()) for item in second
        }
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(
            [item["file_sha256"] for item in first],
            [item["file_sha256"] for item in second],
        )

        config = AppConfig.from_files(root)
        parser = DocumentParser(root, config.parser_version)
        _, documents = self.make_documents(root)
        parsed_payloads = []
        for doc_id in sorted(documents):
            parsed = parser.parse(documents[doc_id])
            parsed_payloads.append(
                {
                    "document_id": parsed["document_id"],
                    "events": parsed["events"],
                    "mentions": parsed["mentions"],
                }
            )
            local_ids = [event["local_id"] for event in parsed["events"]]
            self.assertEqual(len(local_ids), len(set(local_ids)))
            for mention in parsed["mentions"]:
                self.assertEqual(
                    parsed["content"][mention["start"] : mention["end"]],
                    mention["value"],
                )
                self.assertEqual(mention["offset_unit"], "unicode_code_point")
            for event in parsed["events"]:
                start = event["trigger"]["start"]
                end = event["trigger"]["end"]
                self.assertEqual(
                    parsed["content"][start:end],
                    event["trigger"]["surface"],
                )
                for argument in event["arguments"]:
                    self.assertEqual(
                        parsed["content"][
                            argument["start"] : argument["end"]
                        ],
                        argument["value"],
                    )
        again = []
        for doc_id in sorted(documents):
            parsed = parser.parse(documents[doc_id])
            again.append(
                {
                    "document_id": parsed["document_id"],
                    "events": parsed["events"],
                    "mentions": parsed["mentions"],
                }
            )
        self.assertEqual(canonical_json(parsed_payloads), canonical_json(again))

    def test_parser_rejects_invalid_and_duplicate_trigger_spans(self) -> None:
        root = self.make_root()
        config = AppConfig.from_files(root)
        parser = DocumentParser(root, config.parser_version)
        _, documents = self.make_documents(root)

        invalid = copy.deepcopy(documents["D01"])
        invalid["sentences"][0]["event_specs"][0]["trigger_span_override"] = {
            "start": -1,
            "end": 2,
        }
        with self.assertRaises(BusinessError) as caught:
            parser.parse(invalid)
        self.assertEqual(caught.exception.code, "E_SPAN_INVALID")
        self.assertEqual(caught.exception.details["document_id"], "D01")
        self.assertEqual(caught.exception.details["event_id"], "D01:E01")

        parsed = parser.parse(documents["D01"])
        trigger = parsed["events"][0]["trigger"]
        duplicate = copy.deepcopy(documents["D01"])
        duplicate_spec = copy.deepcopy(
            duplicate["sentences"][0]["event_specs"][0]
        )
        duplicate_spec["local_id"] = "E02"
        duplicate_spec["trigger_span_override"] = {
            "start": trigger["start"],
            "end": trigger["end"],
        }
        duplicate["sentences"][0]["event_specs"].append(duplicate_spec)
        with self.assertRaises(BusinessError) as caught:
            parser.parse(duplicate)
        self.assertEqual(caught.exception.code, "E_SPAN_INVALID")
        self.assertEqual(
            caught.exception.details["reason"],
            "duplicate_trigger_occupancy",
        )
        self.assertEqual(caught.exception.details["event_id"], "D01:E02")


class ScoringAndConfigTests(WorkbenchTestCase):
    def test_config_rejects_invalid_floor_and_negative_weight(self) -> None:
        root = self.make_root()
        config = AppConfig.from_files(root)
        with self.assertRaises(BusinessError) as caught:
            config.clone_with(similarity_floor=1.5)
        self.assertEqual(caught.exception.code, "E_CONFIG_INVALID")
        self.assertEqual(
            caught.exception.details["field"],
            "similarity_floor",
        )
        weights = dict(config.weights)
        weights["shared_entity"] = -0.1
        with self.assertRaises(BusinessError) as caught:
            config.clone_with(weights=weights)
        self.assertEqual(caught.exception.code, "E_CONFIG_INVALID")
        self.assertEqual(
            caught.exception.details["field"],
            "shared_entity",
        )

    def test_similarity_contributions_recompute_and_tie_order(self) -> None:
        root = self.make_root()
        config = AppConfig.from_files(root)
        parser = DocumentParser(root, config.parser_version)
        cache = ArtifactCache(root / "runtime" / "cache")
        scorer = SimilarityScorer(cache, config)
        _, documents = self.make_documents(root)
        left = parser.parse(documents["D01"])["events"][0]
        right = parser.parse(documents["D02"])["events"][0]
        for event, doc in ((left, documents["D01"]), (right, documents["D02"])):
            event["source"] = doc["source"]
            event["document_digest"] = content_hash(doc["content"])
            event["event_hash"] = content_hash(event)
        score = scorer.score(
            left,
            right,
            left_hash=left["event_hash"],
            right_hash=right["event_hash"],
            left_doc_digest=left["document_digest"],
            right_doc_digest=right["document_digest"],
        )
        self.assertEqual(
            score["total"],
            scorer.recompute_total(score["components"]),
        )
        self.assertGreaterEqual(score["total"], config.similarity_floor)
        for component in score["components"].values():
            self.assertEqual(
                component["contribution"],
                round(component["weight"] * component["raw"], 6),
            )

        events = {
            "0001:E02": {
                "doc_index": 1,
                "local_id": "E02",
                "order_start": 20,
            },
            "0001:E01": {
                "doc_index": 1,
                "local_id": "E01",
                "order_start": 10,
            },
            "0002:E01": {
                "doc_index": 2,
                "local_id": "E01",
                "order_start": 10,
            },
        }
        candidates = [
            {
                "left_event": "0002:E01",
                "right_event": "0001:E02",
                "total": 0.7,
            },
            {
                "left_event": "0001:E01",
                "right_event": "0001:E02",
                "total": 0.7,
            },
            {
                "left_event": "0001:E01",
                "right_event": "0002:E01",
                "total": 0.8,
            },
        ]
        ordered = scorer.sort_candidates(candidates, events)
        self.assertEqual(ordered[0]["total"], 0.8)
        self.assertEqual(ordered[1]["left_event"], "0001:E01")
        self.assertEqual(ordered[2]["left_event"], "0002:E01")


class IncrementalChainTests(WorkbenchTestCase):
    def test_incremental_merge_only_touches_affected_chain(self) -> None:
        root = self.make_root()
        service = self.make_service(root)
        chain_b = "CH-D03-E01"
        chain_a = "CH-D01-E01"
        before = service.engine.chain_artifacts[chain_b]
        response = service.commit_next()
        report = response["report"]
        self.assertEqual(report["document_id"], "D04")
        self.assertIn(chain_a, report["recalculated_chain_ids"])
        self.assertIn(chain_b, report["reused_chain_ids"])
        self.assertEqual(service.engine.chain_artifacts[chain_b], before)

    def test_chain_conflict_arbitration_and_argument_losers(self) -> None:
        root = self.make_root()
        service = self.make_service(root)
        response = service.demo_conflict()
        record = response["conflict_records"][0]
        self.assertEqual(record["chosen_chain"], "CH-D01-E01")
        self.assertEqual(record["rejected"][0]["chain_id"], "CH-D03-E01")
        self.assertAlmostEqual(
            record["proposals"][0]["evidence_strength"],
            0.754,
        )
        self.assertAlmostEqual(
            record["proposals"][1]["evidence_strength"],
            0.61025,
        )
        self.assertIn("链证据强度较低", record["rejected"][0]["reason"])

        chain = service.engine.chains["CH-D01-E01"]
        time_resolution = chain["argument_resolutions"]["时间"]
        self.assertEqual(time_resolution["winner"]["value"], "2024年7月21日")
        loser_values = {item["value"] for item in time_resolution["losers"]}
        self.assertIn("2024年7月22日", loser_values)
        org_resolution = chain["argument_resolutions"]["机构"]
        self.assertEqual(org_resolution["winner"]["value"], "省应急管理厅")
        self.assertTrue(org_resolution["losers"])

    def test_unresolved_argument_evidence_raises(self) -> None:
        root = self.make_root()
        engine, documents = self.make_engine(root)
        engine.commit_document(documents["D01"])
        synthetic = copy.deepcopy(documents["D01"])
        synthetic["doc_id"] = "T02"
        synthetic["doc_index"] = 2
        synthetic["source"] = "provincial_emergency"
        old_org = "省应急管理厅"
        new_org = "临海市防汛指挥部"
        synthetic["content"] = synthetic["content"].replace(old_org, new_org, 1)
        synthetic["sentences"][0]["text"] = synthetic["content"]
        event_spec = synthetic["sentences"][0]["event_specs"][0]
        for argument in event_spec["arguments"]:
            if argument["role"] == "机构":
                argument["entity_id"] = "O-LH"
        with self.assertRaises(BusinessError) as caught:
            engine.commit_document(synthetic)
        self.assertEqual(caught.exception.code, "E_ARBITRATION_UNRESOLVED")
        self.assertEqual(caught.exception.details["role"], "机构")
        self.assertEqual(len(caught.exception.details["evidence"]), 2)

    def test_membership_validation_reports_duplicate_and_missing(self) -> None:
        root = self.make_root()
        engine, documents = self.make_engine(root)
        engine.commit_document(documents["D01"])
        event_uid = "D01:E01"
        chain_id = engine.membership[event_uid]

        duplicate = engine.clone()
        duplicate.chains[chain_id]["members"].append(event_uid)
        with self.assertRaises(BusinessError) as caught:
            duplicate.validate_membership()
        self.assertEqual(caught.exception.code, "E_CHAIN_DUP_MEMBER")
        self.assertEqual(caught.exception.details["event_id"], event_uid)

        missing = engine.clone()
        missing.chains[chain_id]["members"].remove(event_uid)
        del missing.membership[event_uid]
        with self.assertRaises(BusinessError) as caught:
            missing.validate_membership()
        self.assertEqual(caught.exception.code, "E_CHAIN_MISSING")
        self.assertEqual(caught.exception.details["document_id"], "D01")

    def test_chain_priority_tiebreak_uses_size_and_first_seen(self) -> None:
        root = self.make_root()
        engine, documents = self.make_engine(root)
        engine.commit_document(documents["D01"])
        event = engine.events["D01:E01"]
        proposals = [
            {
                "chain_id": "CH-SMALL",
                "evidence_strength": 0.8,
                "chain_size": 1,
                "first_seen": "D01:E01",
                "first_doc_index": 1,
                "first_local_id": "E01",
                "links": [],
            },
            {
                "chain_id": "CH-LARGE",
                "evidence_strength": 0.8,
                "chain_size": 3,
                "first_seen": "D01:E01",
                "first_doc_index": 1,
                "first_local_id": "E01",
                "links": [],
            },
        ]
        proposals.sort(
            key=lambda item: (
                -item["evidence_strength"],
                -item["chain_size"],
                item["first_doc_index"],
                item["first_local_id"],
                item["chain_id"],
            )
        )
        winner, rejected = engine._arbitrate_chain_conflict(
            event,
            proposals,
            1,
        )
        self.assertEqual(winner["chain_id"], "CH-LARGE")
        self.assertEqual(rejected[0]["reason"], "链规模较小")


class ConsistencyTests(WorkbenchTestCase):
    def test_full_rebuild_parity_and_drift_detection(self) -> None:
        root = self.make_root()
        service = self.make_service(root)
        service.demo_conflict()
        service.demo_degrade()
        self.assertTrue(service.engine.diagnose()["ok"])

        drifting = service.engine.clone()
        chain_id = next(iter(drifting.chains))
        victim = drifting.chains[chain_id]["members"].pop()
        with self.assertRaises(BusinessError) as caught:
            drifting.diagnose()
        self.assertEqual(caught.exception.code, "E_INCREMENTAL_DRIFT")
        self.assertIn("chain_id", caught.exception.details)
        self.assertIn("event_id", caught.exception.details)
        self.assertTrue(
            victim in caught.exception.details["incremental_members"]
            or victim in caught.exception.details["full_members"]
        )

    def test_candidate_scan_omission_is_detected(self) -> None:
        root = self.make_root()
        service = self.make_service(root)
        service.demo_conflict()
        drifting = service.engine.clone()
        target = next(
            event_uid
            for event_uid, manifest in drifting.scan_manifest.items()
            if manifest["peers"]
        )
        drifting.scan_manifest[target]["peers"].pop()
        with self.assertRaises(BusinessError) as caught:
            drifting.diagnose()
        self.assertEqual(caught.exception.code, "E_INCREMENTAL_DRIFT")
        self.assertEqual(
            caught.exception.details["reason"],
            "event_scan_omitted",
        )
        self.assertEqual(caught.exception.details["event_id"], target)


class CacheAndInvalidationTests(WorkbenchTestCase):
    def test_cache_key_material_and_threshold_invalidation(self) -> None:
        root = self.make_root()
        service = self.make_service(root)
        pointers = list((service.runtime / "cache" / "keys" / "local_events").glob("*/*.json"))
        self.assertTrue(pointers)
        pointer = read_json(pointers[0])
        material = pointer["key_material"]
        self.assertIn("document_content_digest", material)
        self.assertIn("vocab_version", material)
        self.assertIn("weights_version", material)
        self.assertIn("upstream_artifact_hash", material)
        chain_pointers = list(
            (service.runtime / "cache" / "keys" / "chain_state").glob("*/*.json")
        )
        self.assertTrue(chain_pointers)
        chain_material = read_json(chain_pointers[0])["key_material"]
        self.assertIn("document_digests", chain_material)
        self.assertIn("vocab_version", chain_material)
        self.assertIn("upstream_artifact_hash", chain_material)
        self.assertIn("weights_version", chain_material)

        response = service.invalidate(threshold=0.80)
        item = response["invalidation"]
        self.assertEqual(item["start_document"], "D02")
        pair_ids = {
            pair["pair_id"] for pair in item["affected_candidate_pairs"]
        }
        self.assertIn("D01:E01|D02:E01", pair_ids)
        self.assertTrue(item["recalculated_chain_ids"])
        self.assertTrue(item["self_check"]["ok"])

    def test_template_revision_reuses_unaffected_chains(self) -> None:
        root = self.make_root()
        service = self.make_service(root)
        service.demo_conflict()
        service.demo_degrade()
        response = service.invalidate(
            revise_document_id="D06",
            reason="test_template_revision",
        )
        item = response["invalidation"]
        self.assertEqual(item["start_document"], "D06")
        self.assertIn("CH-D06-E01", item["recalculated_chain_ids"])
        self.assertIn("CH-D01-E01", item["reused_chain_ids"])
        self.assertIn("CH-D03-E01", item["reused_chain_ids"])
        self.assertTrue(item["self_check"]["ok"])
        self.assertTrue(
            any(
                pair.get("reason") == "document_revised"
                for pair in item["affected_candidate_pairs"]
            )
        )

    def test_trust_update_invalidates_source_suffix(self) -> None:
        root = self.make_root()
        service = self.make_service(root)
        response = service.invalidate(
            trust_updates={"local_daily": 0.40},
            reason="test_trust_update",
        )
        item = response["invalidation"]
        self.assertEqual(item["start_document"], "D02")
        self.assertEqual(item["reused_document_ids"], ["D01"])
        self.assertNotEqual(
            item["trust_version_before"],
            item["trust_version_after"],
        )
        self.assertTrue(
            any(
                pair["pair_id"] == "D01:E01|D02:E01"
                for pair in item["affected_candidate_pairs"]
            )
        )
        self.assertTrue(item["self_check"]["ok"])
        self.assertEqual(
            service.state_view()["snapshots"]["trust_table"]["version"],
            service.snapshots.latest("trust_table")["version"],
        )


class SnapshotAndResourceTests(WorkbenchTestCase):
    def test_snapshot_whole_replace_rollback_and_corruption(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = SnapshotStore(Path(temporary.name) / "snapshots")
        first = store.write("chains", {"members": ["D01:E01"]})
        second = store.write("chains", {"members": ["D01:E01", "D02:E01"]})
        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        with self.assertRaises(BusinessError) as caught:
            store.write("chains", {"members": []}, version=1)
        self.assertEqual(caught.exception.code, "E_SNAPSHOT_ROLLBACK")
        self.assertEqual(caught.exception.details["version"], 1)
        self.assertEqual(caught.exception.details["current_version"], 2)

        with self.assertRaises(BusinessError) as caught:
            store.probe_corruption("chains")
        self.assertEqual(caught.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(caught.exception.details["category"], "chains")

        current_path = (
            Path(temporary.name) / "snapshots" / "chains" / "current.json"
        )
        corrupted = read_json(current_path)
        corrupted["payload"] = {"members": ["TAMPERED"]}
        atomic_write_json(current_path, corrupted)
        with self.assertRaises(BusinessError) as caught:
            store.latest("chains")
        self.assertEqual(caught.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(caught.exception.details["category"], "chains")
        self.assertEqual(caught.exception.details["version"], 2)

    def test_resource_limit_and_degraded_merge(self) -> None:
        root = self.make_root()
        service = self.make_service(root)
        limited_config = service.config.clone_with(
            max_candidate_comparisons=1,
            degrade_on_limit=False,
        )
        limited = service.engine.clone(config=limited_config)
        with self.assertRaises(BusinessError) as caught:
            limited.commit_document(service.documents["D04"])
        self.assertEqual(caught.exception.code, "E_RESOURCE_LIMIT")
        self.assertEqual(
            caught.exception.details["resource"],
            "max_candidate_comparisons",
        )

        response = service.demo_degrade()
        report = response["report"]
        self.assertTrue(report["degraded"])
        self.assertEqual(
            report["degradation"]["category"],
            "HIGHER_SIMILARITY_FLOOR",
        )
        self.assertLessEqual(report["comparisons_performed"], 3)
        self.assertGreater(
            report["comparisons_planned"],
            report["comparisons_performed"],
        )

    def test_stage_busy_returns_immediately_without_state_change(self) -> None:
        root = self.make_root()
        service = self.make_service(root)
        before = len(service.engine.documents)
        with self.assertRaises(BusinessError) as caught:
            service.stage_busy_probe()
        self.assertEqual(caught.exception.code, "E_STAGE_BUSY")
        self.assertEqual(len(service.engine.documents), before)


class ApiTests(WorkbenchTestCase):
    def test_flask_api_smoke_and_error_codes(self) -> None:
        root = self.make_root()
        service = self.make_service(root)
        app = create_app(service=service)
        client = app.test_client()

        index = client.get("/")
        self.assertEqual(index.status_code, 200)
        self.assertIn("共指链拓扑", index.get_data(as_text=True))
        state = client.get("/api/state")
        self.assertEqual(state.status_code, 200)
        payload = state.get_json()
        self.assertEqual(payload["counter"]["documents_processed"], 3)
        self.assertEqual(payload["counter"]["chains"], 2)

        conflict = client.post("/api/demo/conflict")
        self.assertEqual(conflict.status_code, 200)
        self.assertEqual(
            conflict.get_json()["conflict_records"][0]["chosen_chain"],
            "CH-D01-E01",
        )
        degrade = client.post("/api/demo/degrade")
        self.assertEqual(degrade.status_code, 200)
        self.assertTrue(degrade.get_json()["report"]["degraded"])
        invalidation = client.post("/api/demo/invalidate", json={})
        self.assertEqual(invalidation.status_code, 200)
        self.assertTrue(
            invalidation.get_json()["invalidation"]["reused_chain_ids"]
        )
        for endpoint, code in (
            ("/api/probes/rollback", "E_SNAPSHOT_ROLLBACK"),
            ("/api/probes/corruption", "E_SNAPSHOT_CORRUPT"),
            ("/api/probes/stage-busy", "E_STAGE_BUSY"),
        ):
            response = client.post(endpoint)
            self.assertEqual(response.get_json()["code"], code)
            self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
