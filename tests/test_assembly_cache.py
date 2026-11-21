import tempfile
import unittest
from pathlib import Path

from diagwb.cache import (fragment_cache_key, parse_cache_key,
                          ContentAddressedCache)
from diagwb.errors import DiagError
from diagwb.graph import recompose_answer
from diagwb.lineage import replay_assembly, replay_fragment
from diagwb.models import ManifestEntry, ToolCall
from diagwb.samples import generate_workspace
from diagwb.workbench import DiagnosticWorkbench


class AssemblyCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.info = generate_workspace(base / "data")
        self.wb = DiagnosticWorkbench(base / "runtime")
        self.manifests = self.wb.load_manifest(self.info["manifest"])
        for entry in self.manifests:
            for call in entry.tool_calls:
                path = self.wb.resolve_result_file(
                    self.info["results"], call.tool_id,
                    call.result_format_version)
                self.wb.load_result(call.result_ref, path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_assembly_fully_uses_cache(self):
        first = self.wb.assemble_request(self.manifests[0])
        second = self.wb.assemble_request(self.manifests[0])
        self.assertEqual(len(second.recomputed), 0)
        self.assertEqual(len(second.reused), len(first.fragments))
        parse_hits = sum(1 for node in second.dag
                         if node.kind == "parse" and node.status == "hit")
        self.assertEqual(parse_hits, len(first.fragments))
        self.assertEqual(first.answer, second.answer)
        self.assertEqual(first.answer_hash, second.answer_hash)

    def test_answer_recomputes_identically(self):
        assembly = self.wb.assemble_request(self.manifests[0])
        answer, answer_hash = recompose_answer(assembly.fragments)
        self.assertEqual(answer, assembly.answer)
        self.assertEqual(answer_hash, assembly.answer_hash)

    def test_invalidation_only_affects_changed_result(self):
        self.wb.assemble_request(self.manifests[0])
        target = self.manifests[0].tool_calls[0]
        new_version = 3 if target.result_format_version != 3 else 2
        self.wb.load_result(target.result_ref, self.wb.resolve_result_file(
            self.info["results"], target.tool_id, new_version))
        report = self.wb.invalidate([target.result_ref])
        self.assertEqual(report.affected_results, [target.result_ref])
        self.assertNotIn(target.result_ref, report.unaffected_results)
        self.assertEqual(len(report.affected_fragments), 1)
        self.assertEqual(len(report.reused),
                         len(self.manifests[0].tool_calls) - 1)
        disjoint = set(report.recomputed).isdisjoint(report.reused)
        self.assertTrue(disjoint)

    def test_unchanged_reload_is_not_invalidated(self):
        target = self.manifests[0].tool_calls[0]
        path = self.wb.resolve_result_file(
            self.info["results"], target.tool_id,
            target.result_format_version)
        # Remove the entry loaded during setUp so the first reload here is a
        # genuine insertion.
        del self.wb.store.entries[target.result_ref]
        first = self.wb.load_result(target.result_ref, path)
        second = self.wb.load_result(target.result_ref, path)
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])

    def test_stale_fragment_rejected_on_replay(self):
        self.wb.assemble_request(self.manifests[0])
        target = self.manifests[0].tool_calls[0]
        new_version = 3 if target.result_format_version != 3 else 2
        self.wb.load_result(target.result_ref, self.wb.resolve_result_file(
            self.info["results"], target.tool_id, new_version))
        assembly = self.wb.assemblies[self.manifests[0].request_id]
        fragment = next(item for item in assembly.fragments
                        if item.result_ref == target.result_ref)
        with self.assertRaises(DiagError) as caught:
            replay_fragment(fragment, self.wb.cache, self.wb.store,
                            fragment.position)
        self.assertEqual(caught.exception.code, "E_RESULT_STALE")
        self.assertEqual(caught.exception.context["result_ref"],
                         target.result_ref)

    def test_lineage_broken_when_cache_entry_missing(self):
        assembly = self.wb.assemble_request(self.manifests[0])
        fragment = assembly.fragments[0]
        self.wb.cache.delete(fragment.cache_key)
        with self.assertRaises(DiagError) as caught:
            replay_fragment(fragment, self.wb.cache, self.wb.store,
                            fragment.position)
        self.assertEqual(caught.exception.code, "E_LINEAGE_BROKEN")

    def test_full_replay_succeeds_for_fresh_assembly(self):
        assembly = self.wb.assemble_request(self.manifests[0])
        verdicts = replay_assembly(assembly, self.wb.cache, self.wb.store)
        self.assertEqual(len(verdicts), len(assembly.fragments))
        self.assertTrue(all(item.matches for item in verdicts))

    def test_merge_conflict_on_duplicate_interval(self):
        call = self.manifests[0].tool_calls[0]
        duplicate = ManifestEntry(
            request_id="dup", turn_no=1, query_text="dup",
            tool_calls=[call, ToolCall(
                tool_id=call.tool_id, arg_digest=call.arg_digest,
                result_ref=call.result_ref,
                result_format_version=call.result_format_version)])
        with self.assertRaises(DiagError) as caught:
            self.wb.assemble_request(duplicate)
        self.assertEqual(caught.exception.code, "E_MERGE_CONFLICT")

    def test_cache_keys_fold_all_required_inputs(self):
        self.wb.assemble_request(self.manifests[0])
        target = self.manifests[0].tool_calls[0]
        node = self.wb.store.node(target.result_ref)
        key = parse_cache_key(node, target.arg_digest)
        frag_key = fragment_cache_key(node, target.arg_digest, key)
        self.assertNotEqual(key, frag_key)
        self.assertTrue(self.wb.cache.has(key))


if __name__ == "__main__":
    unittest.main()
