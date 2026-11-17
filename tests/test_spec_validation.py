from __future__ import annotations

from pathlib import Path

from replica_ledger.errors import E_SPEC, SpecError
from replica_ledger.spec import load_replica_group

from tests.common import SampleWorkspace


class SpecValidationTests(SampleWorkspace):
    def _mutate_and_load(self, mutator, expected_field: str) -> None:
        root = self.copy_scenario("grid")
        path = root / "spec.json"
        document = self.read_json(path)
        mutator(document)
        self.write_json(path, document)
        with self.assertRaises(SpecError) as context:
            load_replica_group(path)
        error = context.exception
        self.assertEqual(error.code, E_SPEC)
        self.assertIn(expected_field, error.field_path)
        self.assertTrue(error.details.get("errors"))

    def test_duplicate_node_id_rejected(self) -> None:
        def mutate(document):
            document["nodes"][1]["id"] = document["nodes"][0]["id"]

        self._mutate_and_load(mutate, "nodes[1].id")

    def test_invalid_shard_digest_rejected(self) -> None:
        def mutate(document):
            document["nodes"][0]["blocks"][0]["shards"][0]["digest"] = "g" * 64

        self._mutate_and_load(mutate, "nodes[0].blocks[0].shards[0].digest")

    def test_nonpositive_byte_size_rejected(self) -> None:
        def mutate(document):
            document["nodes"][0]["blocks"][0]["byte_size"] = 0

        self._mutate_and_load(mutate, "nodes[0].blocks[0].byte_size")

    def test_priority_must_be_positive_integer(self) -> None:
        def mutate(document):
            document["nodes"][2]["priority"] = 0

        self._mutate_and_load(mutate, "nodes[2].priority")

    def test_missing_check_reference_rejected(self) -> None:
        def mutate(document):
            document["checks"][0]["depends_on"] = ["DOES-NOT-EXIST"]

        self._mutate_and_load(mutate, "checks[0].depends_on[0]")

    def test_cycle_rejected(self) -> None:
        def mutate(document):
            by_id = {item["block_id"]: item for item in document["checks"]}
            by_id["B-GRID"]["depends_on"] = ["B-TIE"]
            by_id["B-TIE"]["depends_on"] = ["B-GRID"]

        self._mutate_and_load(mutate, "depends_on")

    def test_duplicate_json_key_rejected(self) -> None:
        root = self.copy_scenario("single")
        path = root / "spec.json"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace('"group_id":', '"group_id": "duplicate", "group_id":', 1), encoding="utf-8")
        with self.assertRaises(SpecError) as context:
            load_replica_group(path)
        self.assertEqual(context.exception.code, E_SPEC)
        self.assertEqual(context.exception.field_path, "$")

    def test_valid_samples_load(self) -> None:
        for scenario in ("single", "multi", "grid"):
            with self.subTest(scenario=scenario):
                spec = load_replica_group(self.workspace / "scenarios" / scenario / "spec.json")
                self.assertGreaterEqual(len(spec.nodes), 4)
                self.assertTrue(spec.block_ids)

    def test_group_without_blocks_rejected(self) -> None:
        def mutate(document):
            for node in document["nodes"]:
                node["blocks"] = []
            document["checks"] = []

        self._mutate_and_load(mutate, "nodes")
