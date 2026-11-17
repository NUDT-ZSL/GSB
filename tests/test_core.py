import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rollback_ops.app import OpsApp, VersionStore
from rollback_ops.errors import (E_DEPEND, E_FORMAT, E_INCR, E_LEDGER,
                                 E_REPLAY, E_VERSION, OpsError)
from rollback_ops.graph import (dependents_closure, find_cycle, remove_step,
                                topo_order, validate_plan)
from rollback_ops.incremental import (ContributionCache, diff_tickets,
                                      incremental_recompute)
from rollback_ops.ledger import (DeterministicClock, RollbackExecutor,
                                 fetch_entries, open_ledger, recover,
                                 verify_ledger)
from rollback_ops.loader import load_plan, load_ticket, normalize_ticket
from rollback_ops.models import RollbackPlan, RollbackStep
from rollback_ops.risk import (SCALE, compare_scores, compute_risk,
                               item_contribution, item_error_bound,
                               ItemContribution)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = lambda name: os.path.join(ROOT, "samples", name)


class TestLoader(unittest.TestCase):
    def test_v1_defaults_and_version_marker(self):
        t = load_ticket(S("ticket_v1.json"))
        self.assertEqual(t.source_format_version, 1)
        for it in t.items:
            self.assertEqual(it.source_format_version, 1)
            self.assertEqual(it.category, "application")
            self.assertEqual(it.impacted_services, ())
            self.assertEqual(it.source_system, "legacy-v1")
            self.assertEqual(it.risk_weight, 1)

    def test_v2_v3_fields(self):
        t2 = load_ticket(S("ticket_v2.json"))
        self.assertEqual(t2.item_map()["db.max_connections"].category, "database")
        t3 = load_ticket(S("ticket_v3.json"))
        item = t3.item_map()["net.tls_min_version"]
        self.assertEqual(item.source_system, "netops")
        self.assertEqual(item.risk_weight, 3)
        self.assertEqual(item.source_format_version, 3)

    def test_normalized_result_stable_and_comparable(self):
        a = load_ticket(S("ticket_v3.json"))
        b = load_ticket(S("ticket_v3.json"))
        self.assertEqual(a, b)
        self.assertEqual(a.canonical_json(), b.canonical_json())
        self.assertEqual(a.fingerprint(), b.fingerprint())
        keys = [it.key for it in a.items]
        self.assertEqual(keys, sorted(keys))

    def test_unknown_version_rejected(self):
        with self.assertRaises(OpsError) as ctx:
            normalize_ticket({"format_version": 9, "ticket_id": "x", "changes": []})
        self.assertEqual(ctx.exception.code, E_FORMAT)
        self.assertEqual(ctx.exception.details["path"], "/format_version")

    def test_missing_field_path(self):
        raw = {"format_version": 2, "ticket_id": "x", "changes": [
            {"key": "k", "old_value": "1", "new_value": "2"}]}
        with self.assertRaises(OpsError) as ctx:
            normalize_ticket(raw)
        self.assertEqual(ctx.exception.details["path"], "/changes/0/category")

    def test_bad_category_path(self):
        with self.assertRaises(OpsError) as ctx:
            load_ticket(S("broken/ticket_bad_category.json"))
        self.assertEqual(ctx.exception.code, E_FORMAT)
        self.assertEqual(ctx.exception.details["path"], "/changes/0/category")

    def test_corrupt_json_rejected(self):
        with self.assertRaises(OpsError) as ctx:
            load_ticket(S("broken/ticket_corrupt.json"))
        self.assertEqual(ctx.exception.code, E_FORMAT)

    def test_duplicate_key_rejected(self):
        raw = {"format_version": 1, "ticket_id": "x", "changes": [
            {"key": "k", "old_value": 1, "new_value": 2},
            {"key": "k", "old_value": 3, "new_value": 4}]}
        with self.assertRaises(OpsError) as ctx:
            normalize_ticket(raw)
        self.assertEqual(ctx.exception.code, E_FORMAT)


class TestRisk(unittest.TestCase):
    def test_fixed_point_contributions(self):
        t = load_ticket(S("ticket_v3.json"))
        report = compute_risk(t)
        m = report.contribution_map()
        # db: 4 * (2+1) * 1500 = 18000
        self.assertEqual(m["db.max_connections"].contribution, 18000)
        # cache: 2 * (1+1) * 1000 = 4000
        self.assertEqual(m["cache.ttl_seconds"].contribution, 4000)
        # net: 3 * (2+1) * 1200 = 10800
        self.assertEqual(m["net.tls_min_version"].contribution, 10800)
        self.assertEqual(report.total_score, 34800)
        self.assertEqual(report.total_error_bound,
                         sum(c.error_bound for c in report.contributions))

    def test_error_bound_is_integer_ceiling(self):
        t = load_ticket(S("ticket_v3.json"))
        db = t.item_map()["db.max_connections"]
        # w*(n+1)/2 = 4*3/2 = 6
        self.assertEqual(item_error_bound(db), 6)

    def test_tie_break_dominated_by_error_bound(self):
        t = load_ticket(S("ticket_v3.json"))
        report = compute_risk(t)
        other = compute_risk(load_ticket(S("ticket_v3.json")))
        cmp = compare_scores(report, other)
        self.assertTrue(cmp.dominated_by_error_bound)
        self.assertIn("tie-break", cmp.reason)

    def test_decisive_when_difference_exceeds_bound(self):
        a = compute_risk(load_ticket(S("ticket_v3.json")))
        b = compute_risk(load_ticket(S("ticket_v3_rev4.json")))
        cmp = compare_scores(a, b)
        self.assertFalse(cmp.dominated_by_error_bound)
        self.assertEqual(cmp.ordered[0][1], 4)  # rev4 分数更高


class TestGraph(unittest.TestCase):
    def setUp(self):
        self.plan = load_plan(S("rollback_plan.json"))

    def test_topo_order_deterministic(self):
        self.assertEqual(topo_order(self.plan), ["rb-01", "rb-02", "rb-03", "rb-04"])

    def test_topo_order_independent_of_input_order(self):
        shuffled = RollbackPlan(plan_version=1,
                                steps=tuple(reversed(self.plan.steps)))
        self.assertEqual(topo_order(shuffled), topo_order(self.plan))

    def test_cycle_reports_sequence(self):
        plan = load_plan(S("broken/rollback_plan_cycle.json"))
        with self.assertRaises(OpsError) as ctx:
            validate_plan(plan)
        self.assertEqual(ctx.exception.code, E_DEPEND)
        cycle = ctx.exception.details["cycle"]
        self.assertEqual(cycle[0], cycle[-1])
        self.assertEqual(cycle, ["rb-02", "rb-03", "rb-02"])

    def test_duplicate_step_id(self):
        plan = RollbackPlan(plan_version=1, steps=(
            RollbackStep("rb-01", "a", "restore", ()),
            RollbackStep("rb-01", "b", "restore", ()),
        ))
        with self.assertRaises(OpsError) as ctx:
            validate_plan(plan)
        self.assertEqual(ctx.exception.details["duplicates"], ["rb-01"])

    def test_missing_dependency_reference(self):
        plan = RollbackPlan(plan_version=1, steps=(
            RollbackStep("rb-01", "a", "restore", ("rb-99",)),
        ))
        with self.assertRaises(OpsError) as ctx:
            validate_plan(plan)
        self.assertEqual(ctx.exception.code, E_DEPEND)
        self.assertEqual(ctx.exception.details["missing_references"][0]["missing_dependency"], "rb-99")

    def test_remove_depended_step_rejected(self):
        with self.assertRaises(OpsError) as ctx:
            remove_step(self.plan, "rb-01")
        self.assertEqual(ctx.exception.details["dependents"], ["rb-02"])

    def test_remove_leaf_step_ok(self):
        new_plan = remove_step(self.plan, "rb-03")
        self.assertNotIn("rb-03", [s.step_id for s in new_plan.steps])

    def test_dependents_closure(self):
        self.assertEqual(dependents_closure(self.plan, ["rb-01"]),
                         ["rb-01", "rb-02", "rb-03"])
        self.assertEqual(dependents_closure(self.plan, ["rb-04"]), ["rb-04"])


class TestIncremental(unittest.TestCase):
    def test_only_changed_items_recomputed(self):
        old = load_ticket(S("ticket_v3.json"))
        new = load_ticket(S("ticket_v3_rev4.json"))
        plan = load_plan(S("rollback_plan.json"))
        cache = ContributionCache()
        for it in old.items:
            cache.put(it)
        result = incremental_recompute(old, new, plan, cache)
        self.assertEqual(result.recomputed_keys, ("net.tls_min_version",))
        self.assertEqual(set(result.reused_keys),
                         {"cache.ttl_seconds", "db.max_connections", "feature.dark_mode"})
        self.assertEqual(result.affected_steps, ("rb-04",))
        # 未变化项贡献逐位保持
        full = compute_risk(new).contribution_map()
        for key in result.reused_keys:
            self.assertEqual(result.report.contribution_map()[key].contribution,
                             full[key].contribution)

    def test_upgrade_v1_to_v2_recomputes_all(self):
        old = load_ticket(S("ticket_v1.json"))
        new = load_ticket(S("ticket_v2.json"))
        plan = load_plan(S("rollback_plan.json"))
        cache = ContributionCache()
        for it in old.items:
            cache.put(it)
        result = incremental_recompute(old, new, plan, cache)
        self.assertEqual(len(result.recomputed_keys), 4)
        self.assertEqual(result.affected_steps,
                         ("rb-01", "rb-02", "rb-03", "rb-04"))

    def test_e_incr_on_divergence(self):
        old = load_ticket(S("ticket_v3.json"))
        new = load_ticket(S("ticket_v3_rev4.json"))
        plan = load_plan(S("rollback_plan.json"))
        cache = ContributionCache()
        for it in old.items:
            cache.put(it)
        # 篡改缓存：指纹正确但贡献错误，且键不在变化集内
        victim = old.item_map()["cache.ttl_seconds"]
        cache._entries[victim.key] = ItemContribution(
            key=victim.key, contribution=999999, error_bound=0,
            fingerprint=victim.fingerprint())
        with self.assertRaises(OpsError) as ctx:
            incremental_recompute(old, new, plan, cache)
        self.assertEqual(ctx.exception.code, E_INCR)
        self.assertEqual(ctx.exception.details["first_diff_key"], "cache.ttl_seconds")


class LedgerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = open_ledger(os.path.join(self.tmp.name, "ledger.db"))
        self.ticket = load_ticket(S("ticket_v3.json"))
        self.plan = load_plan(S("rollback_plan.json"))
        self.report = compute_risk(self.ticket)

    def tearDown(self):
        self.tmp.cleanup()


class TestLedger(LedgerTestBase):
    def test_execute_and_idempotent_replay(self):
        ex = RollbackExecutor(self.conn, clock=DeterministicClock())
        entries = ex.execute_plan(self.plan, self.ticket, self.report)
        self.assertEqual(len(entries), 4)
        self.assertEqual([e.step_id for e in entries],
                         ["rb-01", "rb-02", "rb-03", "rb-04"])
        count_before = self.conn.execute("SELECT COUNT(*) c FROM ledger").fetchone()["c"]
        again = ex.execute_plan(self.plan, self.ticket, self.report)
        count_after = self.conn.execute("SELECT COUNT(*) c FROM ledger").fetchone()["c"]
        self.assertEqual(count_before, count_after)
        self.assertTrue(all(e.idempotent_replay for e in again))
        self.assertEqual([e.result for e in entries], [e.result for e in again])

    def test_ledger_records_version_and_risk_snapshot(self):
        ex = RollbackExecutor(self.conn, clock=DeterministicClock())
        ex.execute_plan(self.plan, self.ticket, self.report)
        for e in fetch_entries(self.conn):
            self.assertEqual(e.plan_version, 1)
            self.assertEqual(e.ticket_revision, 3)
            self.assertEqual(e.risk_score, self.report.total_score)
            self.assertTrue(e.executed_at)

    def test_verify_ledger_missing_and_extra(self):
        ex = RollbackExecutor(self.conn, clock=DeterministicClock(),
                              crash_hook=lambda i: (_ for _ in ()).throw(RuntimeError()) if i == 2 else None)
        try:
            ex.execute_plan(self.plan, self.ticket, self.report)
        except RuntimeError:
            pass
        with self.assertRaises(OpsError) as ctx:
            verify_ledger(self.conn, self.plan, 3)
        self.assertEqual(ctx.exception.code, E_LEDGER)
        self.assertEqual(ctx.exception.details["missing_steps"], ["rb-03", "rb-04"])
        # 多余步骤
        self.conn.execute(
            "INSERT INTO ledger (plan_version, ticket_revision, step_id, change_key,"
            " action, result, risk_score, risk_error, executed_at)"
            " VALUES (1, 3, 'rb-99', 'x', 'a', 'r', 0, 0, 't')")
        self.conn.commit()
        with self.assertRaises(OpsError) as ctx:
            verify_ledger(self.conn, self.plan, 3)
        self.assertEqual(ctx.exception.details["extra_steps"], ["rb-99"])

    def test_version_conflict_on_stale_version(self):
        ex = RollbackExecutor(self.conn, clock=DeterministicClock())
        ex.execute_plan(self.plan, self.ticket, self.report)
        stale = load_ticket(S("ticket_v2.json"))  # revision 2 < 3
        with self.assertRaises(OpsError) as ctx:
            ex.execute_plan(self.plan, stale, compute_risk(stale))
        self.assertEqual(ctx.exception.code, E_VERSION)


class TestRecovery(LedgerTestBase):
    def _crash_after_two(self):
        def hook(index):
            if index == 2:
                raise KeyboardInterrupt()
        ex = RollbackExecutor(self.conn, clock=DeterministicClock(), crash_hook=hook)
        try:
            ex.execute_plan(self.plan, self.ticket, self.report)
        except KeyboardInterrupt:
            pass

    def test_recovery_marks_unknown_and_requires_rerun(self):
        self._crash_after_two()
        report = recover(self.conn, self.plan, self.ticket)
        states = {s.step_id: s for s in report.steps}
        self.assertEqual(states["rb-01"].status, "completed")
        self.assertEqual(states["rb-02"].status, "completed")
        self.assertEqual(states["rb-03"].status, "unknown")
        self.assertTrue(states["rb-03"].require_rerun)
        self.assertFalse(report.exit_report_generated)
        self.assertTrue(report.replay_verified)

    def test_rerun_after_recovery_completes(self):
        self._crash_after_two()
        recover(self.conn, self.plan, self.ticket)
        ex = RollbackExecutor(self.conn, clock=DeterministicClock())
        ex.execute_plan(self.plan, self.ticket, self.report)
        verify_ledger(self.conn, self.plan, 3)

    def test_replay_detects_tampered_snapshot(self):
        ex = RollbackExecutor(self.conn, clock=DeterministicClock())
        ex.execute_plan(self.plan, self.ticket, self.report)
        self.conn.execute("UPDATE ledger SET risk_score = 1 WHERE step_id = 'rb-02'")
        self.conn.commit()
        with self.assertRaises(OpsError) as ctx:
            recover(self.conn, self.plan, self.ticket)
        self.assertEqual(ctx.exception.code, E_REPLAY)
        self.assertEqual(ctx.exception.details["step_id"], "rb-02")


class TestVersionEvolution(LedgerTestBase):
    def test_monotonic_revision_and_full_replacement(self):
        store = VersionStore()
        store.load_initial(load_ticket(S("ticket_v1.json")), self.plan)
        for path in ("ticket_v2.json", "ticket_v3.json", "ticket_v3_rev4.json"):
            view, replacement, incr = store.replace_ticket(load_ticket(S(path)))
        self.assertEqual(view.ticket.revision, 4)
        self.assertEqual(len(store.history), 3)  # 旧版本只读历史
        self.assertEqual(replacement.changed_items, ("net.tls_min_version",))
        self.assertEqual(replacement.affected_steps, ("rb-04",))
        with self.assertRaises(OpsError) as ctx:
            store.replace_ticket(load_ticket(S("ticket_v2.json")))
        self.assertEqual(ctx.exception.code, E_VERSION)


class TestDemoDeterminism(unittest.TestCase):
    def test_demo_output_reproducible(self):
        outputs = []
        for _ in range(2):
            proc = subprocess.run(
                [sys.executable, os.path.join(ROOT, "demo.py"),
                 "--ledger", os.path.join(tempfile.mkdtemp(), "d.db")],
                capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            outputs.append(proc.stdout)
        self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
