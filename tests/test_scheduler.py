"""验收测试：归一、关键路径、资源超限、增量重排与边界工况。"""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scheduler import generator
from scheduler.cpm import (backward, build_edges, critical_paths, forward)
from scheduler.errors import (BusinessError, E_CYCLE, E_SCHEMA_UNKNOWN,
                              E_UNIT_MISSING)
from scheduler.feedback import apply_event, apply_events, consistency_report
from scheduler.normalize import normalize
from scheduler.resources import daily_curve

HOUR = 3_600_000


def make_doc(segments, caps, planned_start=0, duration_unit="h"):
    return {"meta": {"schema": "v1", "time_unit": "s",
                     "duration_unit": duration_unit},
            "project": {"id": "t", "name": "t",
                        "planned_start": planned_start},
            "segments": segments, "resource_caps": caps}


def seg(z, b, s, acts):
    return {"zone": z, "building": b, "segment": s, "activities": acts}


def act(aid, order, duration, qty=1, preds=None):
    return {"id": aid, "name": aid, "order": order, "duration": duration,
            "resources": {"土方工": qty}, "predecessors": preds or []}


class NormalizeTest(unittest.TestCase):
    def test_three_versions_normalize_identical(self):
        acts = generator.canonical_activities()
        projects = [normalize(d(acts))
                    for d in (generator.build_v1, generator.build_v2,
                              generator.build_v3)]
        base = projects[0]
        for other in projects[1:]:
            self.assertEqual(base.planned_start_ms, other.planned_start_ms)
            self.assertEqual(base.caps, other.caps)
            self.assertEqual(set(base.activities), set(other.activities))
            for aid in base.activities:
                x, y = base.activities[aid], other.activities[aid]
                self.assertEqual(
                    (x.segment_id, x.order, x.duration_hours, x.resources,
                     sorted(x.predecessors)),
                    (y.segment_id, y.order, y.duration_hours, y.resources,
                     sorted(y.predecessors)), aid)

    def test_provenance_recorded(self):
        project = normalize(generator.build_v2(
            generator.canonical_activities()))
        a = project.activities["Z1-B1-S1-FND"]
        for field in ("id", "name", "order", "duration", "resources",
                      "predecessors"):
            prov = a.provenance[field]
            self.assertEqual(prov["schema"], "v2")
            self.assertTrue(prov["path"].startswith("$.zones[0]"), prov)
        self.assertEqual(project.provenance["planned_start"]["schema"], "v2")

    def test_unknown_schema_rejected(self):
        with self.assertRaises(BusinessError) as ctx:
            normalize(generator.build_boundary_unknown_schema())
        self.assertEqual(ctx.exception.code, E_SCHEMA_UNKNOWN)

    def test_missing_unit_rejected(self):
        with self.assertRaises(BusinessError) as ctx:
            normalize(generator.build_boundary_missing_unit())
        self.assertEqual(ctx.exception.code, E_UNIT_MISSING)

    def test_units_converted(self):
        doc = make_doc([seg("Z1", "B1", "S1", [act("A", 0, 2)])],
                       {"土方工": 5}, planned_start=10,
                       duration_unit="d")
        project = normalize(doc)
        self.assertEqual(project.planned_start_ms, 10_000)
        self.assertEqual(project.activities["A"].duration_hours, 48.0)


class CpmTest(unittest.TestCase):
    def build(self):
        doc = make_doc([
            seg("Z1", "B1", "S1", [act("A", 0, 24),
                                   act("B", 1, 24, preds=["C"])]),
            seg("Z1", "B1", "S2", [act("C", 0, 48)]),
        ], {"土方工": 5})
        project = normalize(doc)
        edges = build_edges(project)
        forward(project)
        end = backward(project, edges)
        return project, edges, end

    def test_forward_cross_segment_not_overridden(self):
        project, _, _ = self.build()
        # B 的段内前驱 A 在 24h 完成，但跨段前驱 C 在 48h 才完成
        self.assertEqual(project.activities["B"].es_ms, 48 * HOUR)
        self.assertEqual(project.activities["B"].trace_predecessors, ["C"])
        self.assertEqual(project.activities["A"].es_ms, 0)

    def test_backward_and_critical_path(self):
        project, edges, end = self.build()
        self.assertEqual(end, 72 * HOUR)
        self.assertEqual(project.activities["A"].float_ms, 24 * HOUR)
        self.assertFalse(project.activities["A"].critical)
        self.assertTrue(project.activities["B"].critical)
        self.assertTrue(project.activities["C"].critical)
        self.assertEqual(critical_paths(project, edges), [["C", "B"]])

    def test_cycle_reports_members_without_fake_path(self):
        project = normalize(generator.build_boundary_cycle())
        build_edges(project)
        with self.assertRaises(BusinessError) as ctx:
            forward(project)
        self.assertEqual(ctx.exception.code, E_CYCLE)
        self.assertEqual(ctx.exception.details["cycle_members"],
                         ["CYC-A", "CYC-B", "CYC-C"])


class ResourceTest(unittest.TestCase):
    def curve_for(self, activities, caps):
        if isinstance(activities[0], dict):
            activities = [activities]
        doc = make_doc([seg("Z1", "B1", f"S{i + 1}", acts)
                        for i, acts in enumerate(activities)], caps)
        project = normalize(doc)
        edges = build_edges(project)
        forward(project)
        backward(project, edges)
        return daily_curve(project)

    def test_equal_to_cap_not_marked(self):
        curve = self.curve_for([[act("A", 0, 24, qty=5)]], {"土方工": 5})
        slot = curve[("1970-01-01", "土方工")]
        self.assertEqual(slot["demand"], 5)
        self.assertFalse(slot["over"])

    def test_over_cap_lists_activities(self):
        curve = self.curve_for(
            [[act("A", 0, 24, qty=4)], [act("B", 0, 24, qty=3)]],
            {"土方工": 5})
        slot = curve[("1970-01-01", "土方工")]
        self.assertTrue(slot["over"])
        self.assertEqual(slot["activities"], ["A", "B"])


class FeedbackTest(unittest.TestCase):
    def setUp(self):
        acts = generator.canonical_activities()
        self.base = normalize(generator.build_v1(acts))
        edges = build_edges(self.base)
        forward(self.base)
        backward(self.base, edges)
        self.events = generator.build_events()

    def test_affected_set_and_unaffected_unchanged(self):
        project = copy.deepcopy(self.base)
        result = apply_event(project, self.events[0])
        affected = set(result["affected"])
        self.assertIn(self.events[0]["activity"], affected)
        self.assertTrue(result["edges"])
        for aid, a in project.activities.items():
            if aid not in affected:
                self.assertEqual(a.es_ms, self.base.activities[aid].es_ms)
                self.assertEqual(a.ef_ms, self.base.activities[aid].ef_ms)

    def test_incremental_matches_full_per_activity(self):
        incremental = copy.deepcopy(self.base)
        results = apply_events(incremental, self.events)
        self.assertEqual(len(results), 3)
        report = consistency_report(incremental, daily_curve(incremental),
                                    self.base, self.events)
        mismatches = [l for l in report["activities"] if not l["match"]]
        self.assertEqual(mismatches, [])
        self.assertTrue(all(l["match"] for l in report["resources"]))
        self.assertTrue(report["critical_paths_match"])
        self.assertTrue(report["ok"])

    def test_cap_reduction_keeps_over_marks(self):
        project = copy.deepcopy(self.base)
        results = apply_events(project, self.events)
        last = results[-1]
        self.assertEqual(last["event"]["type"], "resource_change")
        over_days = [o for o in last["over_capacity"]
                     if o["trade"] == "钢筋工"]
        self.assertTrue(over_days)
        for o in over_days:
            self.assertGreater(o["demand"], o["cap"])
            self.assertTrue(o["activities"])

    def test_event_on_unknown_activity_rejected(self):
        project = copy.deepcopy(self.base)
        with self.assertRaises(BusinessError):
            apply_event(project, {"type": "delayed_finish",
                                  "activity": "NOPE",
                                  "actual_finish_ms": 0})


if __name__ == "__main__":
    unittest.main()
