import json
import os
import shutil
import tempfile
import unittest

from aggwin.errors import BusinessError
from aggwin.fixedpoint import mean_scaled, scaled_to_text, trunc_div
from aggwin.pipeline import StreamingEngine, report_to_dict
from aggwin.queries import load_queries, parse_sql
from aggwin.ring import RingBuffer, RingEntry
from aggwin.schema import load_tables
from aggwin.verifier import assert_identical_results, verify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLES = os.path.join(ROOT, "sample", "tables.json")
QUERIES = os.path.join(ROOT, "sample", "queries.json")


class FixedPointTests(unittest.TestCase):
    def test_truncation_toward_zero(self):
        self.assertEqual(trunc_div(7, 3), 2)
        self.assertEqual(trunc_div(-7, 3), -2)
        self.assertEqual(trunc_div(7, -3), -2)

    def test_mean_scaled_and_rendering(self):
        self.assertEqual(mean_scaled(19, 3), 63333)
        self.assertEqual(scaled_to_text(63333), "6.3333")
        self.assertEqual(scaled_to_text(-50), "-0.0050")

    def test_error_bound_strictly_below_unit_last_place(self):
        total, count, scale = 1, 3, 10000
        scaled = mean_scaled(total, count, scale)
        absolute_error = abs(total / count - scaled / scale)
        self.assertLess(absolute_error, 1 / scale)


class RingBufferTests(unittest.TestCase):
    def test_capacity_eviction_and_sum(self):
        ring = RingBuffer(3)
        for row_id in range(5):
            ring.push(RingEntry(row_id, row_id, row_id + 1))
        self.assertEqual(len(ring), 3)
        self.assertEqual(ring.value_sum, 12)
        self.assertEqual([entry.row_id for entry in ring.entries()], [2, 3, 4])

    def test_sliding_expiration_preserves_remaining_contributions(self):
        ring = RingBuffer(10)
        for row_id in range(6):
            ring.push(RingEntry(row_id, row_id, row_id))
        removed = ring.expire_before(3)
        self.assertEqual([entry.row_id for entry in removed], [0, 1, 2])
        self.assertEqual(ring.value_sum, sum(entry.value for entry in ring.entries()))
        self.assertEqual(ring.value_sum, 12)

    def test_state_roundtrip_normalizes_head(self):
        ring = RingBuffer(4)
        for row_id in range(7):
            ring.push(RingEntry(row_id, row_id, row_id))
        state = ring.to_state()
        self.assertEqual(state["head"], 0)
        restored = RingBuffer.from_state(state)
        self.assertEqual(restored.entries(), ring.entries())
        self.assertEqual(restored.value_sum, ring.value_sum)

    def test_rank_sorted_by_timestamp_then_row_id(self):
        ring = RingBuffer(5)
        ring.push(RingEntry(0, 5, 1))
        ring.push(RingEntry(1, 3, 1))
        ring.push(RingEntry(2, 3, 1))
        self.assertEqual(ring.rank_of(2), 2)
        self.assertEqual(ring.rank_of(0), 3)


class CanonicalizationTests(unittest.TestCase):
    def test_whitespace_case_and_key_order_collapse(self):
        tables = load_tables(TABLES)
        plan_a = load_queries(QUERIES, tables)
        document = json.load(open(QUERIES, encoding="utf-8"))
        for query in document["queries"]:
            if isinstance(query, dict) and query.get("id") == "q_sales_by_region":
                query["group_by"] = ["region"]
                query["func"] = "SUM"
                query["arg"] = "amount"
        reordered = dict(document)
        reordered["queries"] = list(reversed(document["queries"]))
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(reordered, tmp)
        tmp.close()
        plan_b = load_queries(tmp.name, tables)
        os.unlink(tmp.name)
        digest_a = {
            p.query_id: p.digest for p in plan_a.all_plans()
            if not p.query_id.startswith("sql_")
        }
        digest_b = {
            p.query_id: p.digest for p in plan_b.all_plans()
            if not p.query_id.startswith("sql_")
        }
        self.assertEqual(digest_a, digest_b)

    def test_sql_subquery_rejected(self):
        with self.assertRaises(BusinessError) as ctx:
            parse_sql("SELECT * FROM (SELECT amount FROM orders)", 0)
        self.assertEqual(ctx.exception.code, "E_EXPR")


class SchemaTests(unittest.TestCase):
    def test_duplicate_table_rejected_with_location(self):
        document = json.load(open(TABLES, encoding="utf-8"))
        document["tables"].append(json.loads(json.dumps(document["tables"][0])))
        path = os.path.join(tempfile.gettempdir(), "bad_schema.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        with self.assertRaises(BusinessError) as ctx:
            load_tables(path)
        self.assertEqual(ctx.exception.code, "E_SCHEMA")
        self.assertIn("table", ctx.exception.context)

    def test_monotonic_violation_reports_row_and_field(self):
        document = json.load(open(TABLES, encoding="utf-8"))
        clicks = next(t for t in document["tables"] if t["name"] == "clicks")
        clicks["rows"][-1][0] = 0
        path = os.path.join(tempfile.gettempdir(), "monotonic_bad.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        with self.assertRaises(BusinessError) as ctx:
            load_tables(path)
        self.assertEqual(ctx.exception.context["row"], len(clicks["rows"]))
        self.assertEqual(ctx.exception.context["field"], "ts")


class EngineResumeTests(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="aggwin_test_")

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _paths(self, name):
        return (os.path.join(self.workdir, f"snap_{name}"),
                os.path.join(self.workdir, f"result_{name}"))

    def test_resume_is_byte_identical_to_uninterrupted(self):
        full_snap, full_result = self._paths("full")
        engine_full = StreamingEngine(
            load_tables(TABLES), load_queries(QUERIES, load_tables(TABLES)),
            full_snap)
        report_full = engine_full.run()
        from aggwin.pipeline import write_results
        write_results(full_result, report_full)

        partial_doc = json.load(open(TABLES, encoding="utf-8"))
        for table in partial_doc["tables"]:
            table["rows"] = table["rows"][: max(1, len(table["rows"]) // 2)]
        partial_path = os.path.join(self.workdir, "partial.json")
        with open(partial_path, "w", encoding="utf-8") as handle:
            json.dump(partial_doc, handle)

        resume_snap, partial_result = self._paths("resume")
        engine_partial = StreamingEngine(
            load_tables(partial_path),
            load_queries(QUERIES, load_tables(partial_path)),
            resume_snap, batch_sizes={"orders": 2, "clicks": 2})
        write_results(partial_result, engine_partial.run())

        tables = load_tables(TABLES)
        engine_resume = StreamingEngine(
            tables, load_queries(QUERIES, tables), resume_snap,
            batch_sizes={"orders": 2, "clicks": 2})
        resumed_result = os.path.join(self.workdir, "result_resumed")
        write_results(resumed_result, engine_resume.run())

        for name in ("aggregates.json", "windows.json", "trace.json"):
            outcome = assert_identical_results(
                os.path.join(full_result, name),
                os.path.join(resumed_result, name))
            self.assertEqual(outcome["status"], "PASS")
        outcome = assert_identical_results(
            os.path.join(full_result, "report.json"),
            os.path.join(resumed_result, "report.json"))
        self.assertEqual(outcome["status"], "PASS")

    def test_corrupt_snapshot_raises_e_snapshot(self):
        snap, _ = self._paths("corrupt")
        tables = load_tables(TABLES)
        engine = StreamingEngine(tables, load_queries(QUERIES, tables), snap)
        path = engine.checkpoint("seed.sqlite3")
        engine.store.corrupt(path)
        with self.assertRaises(BusinessError) as ctx:
            StreamingEngine(tables, load_queries(QUERIES, tables), snap).restore()
        self.assertEqual(ctx.exception.code, "E_SNAPSHOT")

    def test_out_of_order_limit_raises_e_order(self):
        tables = load_tables(TABLES)
        for table in tables.values():
            table.ooo_ratio_limit = 0.0
        engine = StreamingEngine(tables, load_queries(QUERIES, tables),
                                 os.path.join(self.workdir, "snap_order"))
        with self.assertRaises(BusinessError) as ctx:
            engine.run()
        self.assertEqual(ctx.exception.code, "E_ORDER")


class VerifierTests(unittest.TestCase):
    def test_verifier_passes_on_generated_outputs(self):
        workdir = tempfile.mkdtemp(prefix="aggwin_verify_")
        try:
            snap = os.path.join(workdir, "snap")
            result = os.path.join(workdir, "result")
            tables = load_tables(TABLES)
            engine = StreamingEngine(tables, load_queries(QUERIES, tables), snap)
            from aggwin.pipeline import write_results
            write_results(result, engine.run())
            outcome = verify(TABLES, QUERIES, result, snap)
            self.assertEqual(outcome["overall_status"], "PASS")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
