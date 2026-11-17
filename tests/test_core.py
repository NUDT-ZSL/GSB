from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from domain_editor.bundle import load_bundle, load_commands
from domain_editor.config import load_rule_config
from domain_editor.diagnostics import build_diagnostic_report, report_bytes
from domain_editor.documents import load_documents
from domain_editor.edits import apply_edit
from domain_editor.engine import Engine
from domain_editor.errors import EditInvalidError
from domain_editor.jsonutil import loads_decimal
from domain_editor.samplegen import generate_samples
from domain_editor.v1 import V1_OFFSETS, parse_v1
from domain_editor.v2 import parse_v2


ROOT = Path(__file__).resolve().parent.parent


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="domain-editor-tests-")
        cls.samples = Path(cls.temp.name) / "samples"
        generate_samples(cls.samples, seed=424242, force=True)
        cls.bundle = load_bundle(cls.samples / "manifest.json")
        cls.config = load_rule_config(cls.bundle.rules_path)
        cls.documents, cls.document_failures = load_documents(cls.bundle.documents_path, 4)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_parse_both_versions(self):
        self.assertFalse(self.document_failures)
        v1_docs, v1_failures = parse_v1(
            self.bundle.v1_path, self.documents, self.config, 4
        )
        v2_docs, role_names, _metadata, v2_failures = parse_v2(
            self.bundle.v2_path, self.documents, self.config, 4
        )
        self.assertFalse(v1_failures)
        self.assertFalse(v2_failures)
        self.assertEqual(8, sum(len(doc.relations) for doc in v1_docs.values()))
        self.assertEqual(5, sum(len(doc.events) for doc in v1_docs.values()))
        self.assertEqual(8, sum(len(doc.relations) for doc in v2_docs.values()))
        self.assertEqual(5, sum(len(doc.events) for doc in v2_docs.values()))
        self.assertEqual("employee", role_names["R_EMPLOYEE"])

    def _v1_variant(self, name: str, mutate) -> Path:
        lines = self.bundle.v1_path.read_bytes().splitlines()
        mutated = mutate(list(lines))
        path = Path(self.temp.name) / name
        path.write_bytes(b"\n".join(mutated) + b"\n")
        return path

    def test_v1_format_codes(self):
        overlong = self._v1_variant("overlong.txt", lambda lines: [lines[0] + b"X", *lines[1:]])
        _docs, failures = parse_v1(overlong, self.documents, self.config, 2)
        self.assertIn("E_FORMAT_OVERLONG", {failure.business_code for failure in failures})

        truncated = self._v1_variant("truncated.txt", lambda lines: [lines[0][:-1], *lines[1:]])
        _docs, failures = parse_v1(truncated, self.documents, self.config, 2)
        self.assertIn("E_FORMAT_TRUNCATED", {failure.business_code for failure in failures})

        def bad_confidence(lines):
            offset, width = V1_OFFSETS["confidence_permille"]
            lines[0] = lines[0][:offset] + b"1200" + lines[0][offset + width :]
            return lines

        bad = self._v1_variant("confidence.txt", bad_confidence)
        _docs, failures = parse_v1(bad, self.documents, self.config, 2)
        self.assertIn("E_CONF_RANGE", {failure.business_code for failure in failures})

    def test_v2_json_and_span_codes(self):
        value = loads_decimal(self.bundle.v2_path.read_text())
        del value["role_names"]
        missing = Path(self.temp.name) / "missing.json"
        from domain_editor.jsonutil import dumps_canonical

        missing.write_text(dumps_canonical(value, newline=True))
        _docs, _roles, _meta, failures = parse_v2(missing, self.documents, self.config, 2)
        self.assertIn("E_JSON_INVALID", {failure.business_code for failure in failures})

        value = loads_decimal(self.bundle.v2_path.read_text())
        value["documents"][0]["relations"][0]["head"]["start"] = value["documents"][0]["relations"][0]["head"]["end"]
        value["documents"][0]["relations"][0]["head"]["end"] = loads_decimal("0")
        disorder = Path(self.temp.name) / "span.json"
        disorder.write_text(dumps_canonical(value, newline=True))
        _docs, _roles, _meta, failures = parse_v2(disorder, self.documents, self.config, 2)
        self.assertIn("E_SPAN_ORDER", {failure.business_code for failure in failures})

    def test_merge_split_roundtrip(self):
        engine = Engine(self.bundle, Path(self.temp.name) / "merge-workspace")
        engine.prepare("v1", resume=False)
        assert engine.state is not None
        before = engine.state.documents["doc-002"].to_dict()
        merged, _affected, _detail = apply_edit(
            engine.state,
            {"type": "merge_relations", "doc_id": "doc-002", "entry_ids": ["rel-003", "rel-004"]},
            self.config,
            2,
        )
        split, _affected, _detail = apply_edit(
            merged,
            {
                "type": "split_relation",
                "doc_id": "doc-002",
                "entry_id": "rel-003",
                "boundary_token_index": 4,
            },
            self.config,
            2,
        )
        after = split.documents["doc-002"].to_dict()
        self.assertEqual(before["relations"], after["relations"])
        self.assertEqual(
            [arg["source_relation"] for event in before["events"] for arg in event["arguments"]],
            [arg["source_relation"] for event in after["events"] for arg in event["arguments"]],
        )

    def test_invalid_edit_is_transactional(self):
        engine = Engine(self.bundle, Path(self.temp.name) / "transaction-workspace")
        engine.prepare("v1", resume=False)
        assert engine.state is not None
        before = engine.state.ir_fingerprint
        with self.assertRaises(EditInvalidError):
            apply_edit(
                engine.state,
                {
                    "type": "shift_offsets",
                    "doc_id": "doc-003",
                    "amount_seconds": "-100",
                },
                self.config,
                2,
            )
        self.assertEqual(before, engine.state.ir_fingerprint)

    def test_concurrency_and_diagnostics_are_deterministic(self):
        commands = load_commands(self.bundle.commands_path)
        results = []
        for workers in (1, 4):
            workspace = Path(self.temp.name) / f"workers-{workers}"
            engine = Engine(self.bundle, workspace, max_workers=workers)
            engine.reset_workspace()
            result = engine.execute(commands)
            results.append((result, workspace))
            self.assertEqual(0, result.exit_code)
        first, first_workspace = results[0]
        second, second_workspace = results[1]
        self.assertEqual(first.state.ir_fingerprint, second.state.ir_fingerprint)
        self.assertEqual(first.state.loss_hash, second.state.loss_hash)
        self.assertEqual(
            [failure.to_dict() for failure in first.state.failures],
            [failure.to_dict() for failure in second.state.failures],
        )
        self.assertEqual(
            [log.to_dict() for log in first.state.logs],
            [log.to_dict() for log in second.state.logs],
        )
        self.assertEqual(
            (first_workspace / "project.v2.json").read_bytes(),
            (second_workspace / "project.v2.json").read_bytes(),
        )
        report_one = build_diagnostic_report(first.state, self.config, 4)
        report_two = build_diagnostic_report(second.state, self.config, 4)
        self.assertEqual(report_bytes(report_one), report_bytes(report_two))
        self.assertTrue(report_one["roundtrips"]["v2"]["entry_equivalent"])

    def test_snapshot_hit_skips_parse(self):
        commands = load_commands(self.bundle.commands_path)
        workspace = Path(self.temp.name) / "snapshot-workspace"
        first = Engine(self.bundle, workspace)
        first.reset_workspace()
        result = first.execute(commands)
        self.assertEqual(0, result.exit_code)
        second = Engine(self.bundle, workspace)
        second.prepare("v1", resume=True)
        self.assertTrue(second.snapshot_hit)
        self.assertIsNotNone(second.state)
        self.assertEqual(result.state.ir_fingerprint, second.state.ir_fingerprint)


if __name__ == "__main__":
    unittest.main()
