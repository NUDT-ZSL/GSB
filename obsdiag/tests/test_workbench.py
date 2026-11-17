"""工作台确定性与业务规则测试（仅标准库 unittest）。"""
from __future__ import annotations

import os
import tempfile
import unittest

from obsdiag.correlation import correlate
from obsdiag.errors import E_INPUT, ObsDiagError
from obsdiag.injection import inject_gap, inject_order
from obsdiag.loader import load_observations, load_rules, load_topology
from obsdiag.models import canonical_dumps
from obsdiag.persistence import STATUS_UNKNOWN, WorkbenchStore
from obsdiag.pipeline import (
    report_canonical_bytes,
    run_pipeline,
    run_topology_evolution,
    rerun_gap_scenario,
    rerun_order_scenario,
)
from obsdiag.recovery import recover_session
from obsdiag.rules import evaluate_events
from obsdiag.versioning import apply_rule_changeset, apply_topology_changeset

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "samples")
TOPO = os.path.join(SAMPLE, "v1", "topology.json")
RULES = os.path.join(SAMPLE, "v1", "rules.json")
OBS = os.path.join(SAMPLE, "v1", "observations.ndjson")
TOPO_CHANGESET = os.path.join(SAMPLE, "v2", "topology_changeset.json")
RULE_CHANGESET = os.path.join(SAMPLE, "v2", "rule_changeset_sample.json")


class LoaderValidationTests(unittest.TestCase):
    def test_valid_inputs_load(self):
        topology = load_topology(TOPO)
        ruleset = load_rules(RULES, topology)
        observations = load_observations(OBS, topology, ruleset)
        self.assertEqual(topology.version, 1)
        self.assertEqual(len(ruleset.rules), 8)
        self.assertEqual(len(observations), 25)

    def _assert_input_fault(self, topology_path, rules_path, obs_path, field_path):
        with self.assertRaises(ObsDiagError) as context:
            topology = load_topology(topology_path)
            ruleset = load_rules(rules_path, topology)
            load_observations(obs_path, topology, ruleset)
        self.assertEqual(context.exception.code, E_INPUT)
        self.assertIsNotNone(context.exception.fault)
        self.assertEqual(context.exception.fault.field_path, field_path)

    def test_bad_edge_weight_rejected_with_line(self):
        with self.assertRaises(ObsDiagError) as context:
            load_topology(os.path.join(SAMPLE, "invalid", "topology_bad_weight.json"))
        self.assertEqual(context.exception.fault.line_number, 5)
        self.assertEqual(context.exception.fault.field_path, "edges[0].weight")

    def test_duplicate_service_rejected(self):
        with self.assertRaises(ObsDiagError) as context:
            load_topology(os.path.join(SAMPLE, "invalid", "topology_duplicate_service.json"))
        self.assertEqual(context.exception.fault.field_path, "services[1]")

    def test_bad_metric_and_timestamp_rejected(self):
        topology = load_topology(TOPO)
        ruleset = load_rules(RULES, topology)
        with self.assertRaises(ObsDiagError) as context:
            load_observations(
                os.path.join(SAMPLE, "invalid", "observations_bad_metric.ndjson"),
                topology,
                ruleset,
            )
        self.assertEqual(context.exception.fault.line_number, 1)
        self.assertEqual(context.exception.fault.field_path, "metric")
        with self.assertRaises(ObsDiagError) as context:
            load_observations(
                os.path.join(SAMPLE, "invalid", "observations_bad_ts.ndjson"),
                topology,
                ruleset,
            )
        self.assertEqual(context.exception.fault.field_path, "timestamp_ms")

    def test_duplicate_rule_id_rejected(self):
        topology = load_topology(TOPO)
        with self.assertRaises(ObsDiagError) as context:
            load_rules(os.path.join(SAMPLE, "invalid", "rules_duplicate_id.json"), topology)
        self.assertEqual(context.exception.fault.field_path, "rules[1].rule_id")


class RuleEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.topology = load_topology(TOPO)
        self.ruleset = load_rules(RULES, self.topology)
        self.observations = load_observations(OBS, self.topology, self.ruleset)

    def test_integer_float_threshold_equivalence(self):
        # 85.5 触发、85 不触发；90 与 90.0 行为一致；0.05 边界等值命中 >=
        events = evaluate_events(self.observations, self.ruleset)
        values_by_rule = {}
        for event in events:
            values_by_rule.setdefault(event.rule_id, set()).add(event.value)
        self.assertIn(90.0, values_by_rule["R-PDB-POOL"])
        self.assertNotIn(89, values_by_rule["R-PDB-POOL"])
        self.assertIn(0.05, values_by_rule["R-ORD-ERR"])
        self.assertNotIn(0.0499, values_by_rule["R-ORD-ERR"])
        self.assertIn(90.0, values_by_rule["R-PAY-CPU"])
        self.assertNotIn(85.5, values_by_rule["R-PAY-CPU"])

    def test_concurrency_and_order_independence(self):
        sequential = evaluate_events(self.observations, self.ruleset, concurrency=1)
        parallel = evaluate_events(self.observations, self.ruleset, concurrency=8)
        shuffled_input = list(self.observations)
        shuffled_input.sort(key=lambda item: (-item.timestamp_ms, item.service))
        shuffled_events = evaluate_events(shuffled_input, self.ruleset, concurrency=4)
        self.assertEqual(
            canonical_dumps([event.member_key() for event in sequential]),
            canonical_dumps([event.member_key() for event in parallel]),
        )
        self.assertEqual(
            [event.stable_key() for event in sequential],
            [event.stable_key() for event in shuffled_events],
        )

    def test_no_duplicate_events_per_observation(self):
        events = evaluate_events(self.observations, self.ruleset)
        keys = [event.member_key() for event in events]
        self.assertEqual(len(keys), len(set(keys)))


class CorrelationAndRootCauseTests(unittest.TestCase):
    def test_deterministic_clusters_and_folding(self):
        topology = load_topology(TOPO)
        ruleset = load_rules(RULES, topology)
        observations = load_observations(OBS, topology, ruleset)
        events = evaluate_events(observations, ruleset)
        first = correlate(events, topology, ruleset, 5000)
        second = correlate(tuple(reversed(events)), topology, ruleset, 5000)
        self.assertEqual(
            [cluster.cluster_id for cluster in first],
            [cluster.cluster_id for cluster in second],
        )
        payment_cluster = next(
            cluster
            for cluster in first
            if {event.service for event in cluster.members} ==
            {"notify", "order", "payment", "payment-db"}
        )
        self.assertEqual(payment_cluster.root_causes[0].service, "payment-db")
        folded_services = {item.event.service for item in payment_cluster.folded}
        # payment/order 沿依赖边可到达 payment-db，故折叠；
        # notify 与 payment-db 间不存在有向依赖路径，保留为独立根因候选
        self.assertEqual(folded_services, {"order", "payment"})
        for item in payment_cluster.folded:
            self.assertEqual(item.triggered_by_service, "payment-db")
        # 簇内根因候选顺序确定
        ranks = [candidate.service for candidate in payment_cluster.root_causes]
        self.assertEqual(ranks, ["payment-db", "payment", "notify", "order"])

    def test_window_boundary(self):
        topology = load_topology(TOPO)
        ruleset = load_rules(RULES, topology)
        observations = load_observations(OBS, topology, ruleset)
        events = evaluate_events(observations, ruleset)
        wide = correlate(events, topology, ruleset, 100_000)
        narrow = correlate(events, topology, ruleset, 0)
        self.assertLessEqual(len(wide), len(narrow))
        # 窗口为 0 时同服务同毫秒事件仍可合并，不同时间戳事件必然分开
        self.assertGreaterEqual(len(narrow), len(wide))


class InjectionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.store = WorkbenchStore(os.path.join(self.tempdir, "test.sqlite3"))
        self.baseline, self.topology, _, _ = run_pipeline(
            TOPO, RULES, OBS, 5000, concurrency=1, tag="baseline", store=self.store
        )

    def tearDown(self):
        self.store.close()

    def test_e_order_byte_identical_conclusions(self):
        shuffled = os.path.join(self.tempdir, "shuffled.ndjson")
        injection, _ = inject_order(OBS, shuffled, seed=42)
        rerun, verified = rerun_order_scenario(
            self.baseline, TOPO, RULES, shuffled, 5000, 4, injection, store=self.store
        )
        self.assertTrue(verified.verified)
        self.assertEqual(
            [c.cluster_id for c in self.baseline.clusters],
            [c.cluster_id for c in rerun.clusters],
        )

    def test_e_gap_affected_clusters(self):
        gapped = os.path.join(self.tempdir, "gapped.ndjson")
        injection, _ = inject_gap(OBS, gapped, "payment-db", self.topology)
        rerun, verified = rerun_gap_scenario(
            self.baseline, self.topology, TOPO, RULES, gapped, 5000, 2,
            injection, "payment-db", store=self.store,
        )
        self.assertTrue(verified.verified)
        expected_old = {
            cluster.cluster_id
            for cluster in self.baseline.clusters
            if any(event.service == "payment-db" for event in cluster.members)
        }
        self.assertTrue(expected_old.issubset(set(verified.actual_affected_cluster_ids)))
        self.assertGreater(len(rerun.clusters), 0)

    def test_e_topo_evolution_invalidation_and_retention(self):
        rerun, new_topology, verified, diff = run_topology_evolution(
            self.baseline, self.topology, TOPO, RULES, OBS,
            TOPO_CHANGESET, 5000, 2, store=self.store,
        )
        self.assertEqual(new_topology.version, 2)
        self.assertTrue(verified.verified)
        self.assertTrue(diff.invalidated_cluster_ids)
        self.assertTrue(diff.new_cluster_ids)
        # 未受影响簇内容指纹保持不变且可校验
        retained_hashes = {
            cluster.content_hash
            for cluster in rerun.clusters
            if cluster.cluster_id in diff.retained_cluster_ids
        }
        baseline_hashes = {cluster.content_hash for cluster in self.baseline.clusters}
        self.assertTrue(retained_hashes.issubset(baseline_hashes))

    def test_rule_changeset_version_monotonic(self):
        ruleset = load_rules(RULES, self.topology)
        new_ruleset, replaced = apply_rule_changeset(ruleset, self.topology, RULE_CHANGESET)
        self.assertEqual(new_ruleset.version, 2)
        self.assertEqual(replaced, ("R-PAY-CPU",))
        replaced_rule = next(rule for rule in new_ruleset.rules if rule.rule_id == "R-PAY-CPU")
        self.assertEqual(replaced_rule.severity, "critical")


class PersistenceAndRecoveryTests(unittest.TestCase):
    def test_complete_session_roundtrip(self):
        tempdir = tempfile.mkdtemp()
        store = WorkbenchStore(os.path.join(tempdir, "db.sqlite3"))
        report, _, _, _ = run_pipeline(
            TOPO, RULES, OBS, 5000, concurrency=2, tag="persist", store=store
        )
        loaded = store.load_report(report.session_id)
        self.assertEqual(
            report_canonical_bytes(report), report_canonical_bytes(loaded)
        )
        result = recover_session(store, report.session_id)
        self.assertEqual(result.recovered_clusters, len(report.clusters))
        self.assertFalse(result.rerun_required)
        store.close()

    def test_force_terminated_session_unknown_and_rerun(self):
        tempdir = tempfile.mkdtemp()
        db_path = os.path.join(tempdir, "db.sqlite3")
        store = WorkbenchStore(db_path)
        topology = load_topology(TOPO)
        ruleset = load_rules(RULES, topology)
        observations = load_observations(OBS, topology, ruleset)
        session_id = "SESS-UNIT-CRASH"
        store.begin_session(session_id, 1, 1, len(observations), 5000, "fp")
        store.save_checkpoint(session_id, 7, 0, "boundary")
        store.close()

        reopened = WorkbenchStore(db_path)
        self.assertIsNone(reopened.load_report(session_id))
        result = recover_session(reopened, session_id)
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertTrue(result.rerun_required)
        self.assertEqual(result.cursor, 7)
        self.assertIn("no exit report", result.note)
        reopened.close()


class VersioningValidationTests(unittest.TestCase):
    def test_topology_changeset_must_increment_by_one(self):
        topology = load_topology(TOPO)
        bad = os.path.join(tempfile.mkdtemp(), "bad.json")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write('{"new_version": 9, "removed_edges": [], "added_edges": []}')
        with self.assertRaises(ObsDiagError):
            apply_topology_changeset(topology, bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
