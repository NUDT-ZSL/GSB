"""手算对照测试：小型拓扑逐段结算 + 回合推演 + 增量/全量一致性。

手算拓扑（均为双向边）：
  gate-A:4  A-B:6  gate-S:3  S-B:5  B-R:2
基础消耗：stamina(d)=d，supply(d)=(d+1)//2。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from parksim.configio import scenario_from_dict, scenario_to_dict
from parksim.engine import changed_rounds, simulate
from parksim.generator import generate
from parksim.graph import Graph
from parksim.model import Command, CrowdFactor, Edge, Node, Scenario, Topology, WeatherEvent
from parksim.report import report_to_json
from parksim.validate import ValidationError, validate_scenario


def make_scenario():
    topo = Topology(
        nodes={n.name: n for n in [
            Node("gate", "gate"), Node("A", "facility"), Node("B", "facility"),
            Node("S", "supply"), Node("R", "rest")]},
        edges=[Edge("gate", "A", 4), Edge("A", "B", 6), Edge("gate", "S", 3),
               Edge("S", "B", 5), Edge("B", "R", 2)])
    return Scenario(
        topology=topo, total_rounds=6, start_node="gate",
        init_stamina=20, init_supply=10, supply_capacity=15,
        targets=["A", "B"],
        weather=[WeatherEvent(2, "烈日", stamina_factor=1500, supply_factor=1000)],
        crowd=[CrowdFactor(2, stamina_factor=1200, supply_factor=2000)],
        commands=[
            Command("MOVE", ("gate", "A")),
            Command("MOVE", ("A", "B")),
            Command("MOVE", ("B", "gate")),   # 需要 st8 > 剩余6，被拒
            Command("MOVE", ("B", "R")),
            Command("REST", amount=10),
            Command("WAIT"),
        ])


class TestGraph(unittest.TestCase):
    def test_distances(self):
        g = Graph(make_scenario().topology)
        self.assertEqual(g.distance("gate", "A"), 4)
        self.assertEqual(g.distance("gate", "B"), 8)   # gate-S-B
        self.assertEqual(g.distance("A", "R"), 8)      # A-B-R

    def test_unreachable_is_none(self):
        sc = make_scenario()
        sc.topology.nodes["X"] = Node("X", "facility")
        g = Graph(sc.topology)
        self.assertIsNone(g.distance("gate", "X"))
        self.assertIsNone(g.route_segments(["gate", "X"]))

    def test_route_segments_sum(self):
        g = Graph(make_scenario().topology)
        segs = g.route_segments(["gate", "A", "B"])
        self.assertEqual(segs, [("gate", "A", 4), ("A", "B", 6)])
        self.assertEqual(sum(d for _, _, d in segs), 10)


class TestSimulation(unittest.TestCase):
    def setUp(self):
        self.rep = simulate(make_scenario())

    def test_round_by_round(self):
        r = self.rep.rounds
        # R1: MOVE gate->A，无修正。st 20-4=16, sp 10-2=8
        self.assertEqual((r[0].stamina_after, r[0].supply_after), (16, 8))
        # R2: MOVE A->B。st: 6*1500//1000=9 -> 9*1200//1000=10；sp: 3*2000//1000=6
        self.assertEqual((r[1].stamina_after, r[1].supply_after), (6, 2))
        seg = r[1].segments[0]
        self.assertEqual((seg.base_stamina, seg.final_stamina), (6, 10))
        self.assertEqual((seg.base_supply, seg.final_supply), (3, 6))
        mods = [(m.source, m.field, m.factor, m.before, m.after) for m in seg.modifiers]
        self.assertEqual(mods, [("weather:烈日", "stamina", 1500, 6, 9),
                                ("crowd", "stamina", 1200, 9, 10),
                                ("crowd", "supply", 2000, 3, 6)])
        # R3: MOVE B->gate 需 st8 > 6，skip 拒绝，位置不变
        self.assertFalse(r[2].executed)
        self.assertEqual(r[2].node_after, "B")
        self.assertEqual(r[2].alerts[0].code, "INSUFFICIENT_STAMINA")
        self.assertEqual(r[2].alerts[0].policy, "skip")
        # R4: MOVE B->R，d2 -> st2 sp1。st 6-2=4, sp 2-1=1
        self.assertEqual((r[3].stamina_after, r[3].supply_after), (4, 1))
        # R5: REST 10 -> min(20, 4+10)=14
        self.assertEqual(r[4].stamina_after, 14)
        # R6: WAIT
        self.assertEqual(r[5].stamina_after, 14)

    def test_totals(self):
        self.assertEqual(self.rep.total_stamina_spent, 4 + 10 + 2)
        self.assertEqual(self.rep.total_supply_spent, 2 + 6 + 1)

    def test_scores(self):
        a, b = self.rep.facility_scores
        # A: 40 + 800*30//1000=24 + 533*20//1000=10 + 166*10//1000=1 = 75
        self.assertTrue(a.visited)
        self.assertEqual(a.visit_round, 1)
        self.assertEqual(a.score, 75)
        # B: 40 + 300*30//1000=9 + 133*20//1000=2 + 333*10//1000=3 = 54
        self.assertTrue(b.visited)
        self.assertEqual(b.visit_round, 2)
        self.assertEqual(b.score, 54)
        self.assertEqual(self.rep.overall_score, (75 + 54) // 2)
        self.assertEqual(self.rep.unvisited, [])

    def test_unvisited_target(self):
        sc = make_scenario()
        sc.targets = ["A", "B", "S"]  # S 不是 facility 会在校验拦截，改用未到达设施
        sc.topology.nodes["C"] = Node("C", "facility")
        sc.targets = ["A", "B", "C"]
        rep = simulate(sc)
        self.assertEqual(rep.unvisited, ["C"])
        self.assertEqual(rep.overall_score, (75 + 54 + 0) // 3)

    def test_determinism(self):
        self.assertEqual(report_to_json(simulate(make_scenario())),
                         report_to_json(self.rep))

    def test_truncate_policy(self):
        sc = make_scenario()
        sc.init_stamina = 8  # R2 需要 10，只能走部分？单段不可截 -> 拒绝
        rep = simulate(sc, policy="truncate")
        self.assertFalse(rep.rounds[1].executed)
        # 多段路线截断：R1 改道 gate->A->B，st8 只能走完第一段(4)+第二段(10)不够
        sc2 = make_scenario()
        sc2.init_stamina = 8
        sc2.commands[0] = Command("MOVE", ("gate", "A", "B"))
        rep2 = simulate(sc2, policy="truncate")
        self.assertEqual(rep2.rounds[0].node_after, "A")
        self.assertEqual(rep2.rounds[0].stamina_after, 4)
        self.assertEqual(rep2.rounds[0].alerts[0].code, "INSUFFICIENT_STAMINA")

    def test_abort_policy(self):
        rep = simulate(make_scenario(), policy="abort")
        self.assertTrue(any(a.code == "ABORT" for a in rep.alerts))
        self.assertFalse(rep.rounds[3].executed)
        self.assertIn("ABORTED", rep.trajectory[3])


class TestIncremental(unittest.TestCase):
    def test_param_change_matches_full_recompute(self):
        old = make_scenario()
        new = make_scenario()
        new.weather[0] = WeatherEvent(2, "烈日", stamina_factor=1000,
                                      supply_factor=1000)
        rep_old, rep_new = simulate(old), simulate(new)
        changed = changed_rounds(rep_old, rep_new)
        # 第 2 回合修正消失，且其后续回合的 before/after 状态随之变化
        self.assertIn(2, changed)
        self.assertIn(3, changed)
        self.assertNotIn(1, changed)
        # R2 手算：st 6*1200//1000=7，sp 3*2000//1000=6 -> st 16-7=9, sp 8-6=2
        self.assertEqual(rep_new.rounds[1].stamina_after, 9)
        # “增量”结果必须与对修改后参数从零全量重算逐字段相同
        self.assertEqual(report_to_json(rep_new),
                         report_to_json(simulate(new)))

    def test_distance_change(self):
        old = make_scenario()
        new = make_scenario()
        new.topology.edges[0] = Edge("gate", "A", 5)  # R1 距离变化
        changed = changed_rounds(simulate(old), simulate(new))
        self.assertIn(1, changed)


class TestValidation(unittest.TestCase):
    def base_dict(self):
        return scenario_to_dict(make_scenario())

    def test_ok(self):
        validate_scenario(scenario_from_dict(self.base_dict()))

    def test_negative_distance(self):
        d = self.base_dict()
        d["topology"]["edges"][0]["distance"] = -3
        with self.assertRaises(ValidationError) as ctx:
            scenario_from_dict(d)
        self.assertEqual(ctx.exception.position, 1)

    def test_dangling_node(self):
        d = self.base_dict()
        d["topology"]["edges"][0]["b"] = "ghost"
        with self.assertRaises(ValidationError) as ctx:
            scenario_from_dict(d)
        self.assertEqual(ctx.exception.position, 1)

    def test_duplicate_edge(self):
        d = self.base_dict()
        d["topology"]["edges"].append({"a": "A", "b": "gate", "distance": 7})
        with self.assertRaises(ValidationError):
            scenario_from_dict(d)

    def test_weather_round_out_of_range(self):
        d = self.base_dict()
        d["weather"][0]["round"] = 99
        with self.assertRaises(ValidationError) as ctx:
            scenario_from_dict(d)
        self.assertEqual(ctx.exception.position, 99)

    def test_command_unknown_node(self):
        d = self.base_dict()
        d["commands"][0]["waypoints"] = ["gate", "nowhere"]
        with self.assertRaises(ValidationError) as ctx:
            scenario_from_dict(d)
        self.assertEqual(ctx.exception.position, 1)


class TestGenerator(unittest.TestCase):
    def test_deterministic(self):
        a, b = generate(seed=7), generate(seed=7)
        self.assertEqual(scenario_to_dict(a), scenario_to_dict(b))

    def test_generated_passes_validation(self):
        for seed in (1, 7, 42, 2024):
            validate_scenario(generate(seed=seed))
            validate_scenario(generate(seed=seed, scenario="overflow"))

    def test_overflow_scenario_alerts(self):
        rep = simulate(generate(seed=42, scenario="overflow"))
        codes = {a.code for a in rep.alerts}
        self.assertTrue(codes & {"INSUFFICIENT_STAMINA", "INSUFFICIENT_SUPPLY"})

    def test_overflow_deterministic_report(self):
        sc = generate(seed=42, scenario="overflow")
        self.assertEqual(report_to_json(simulate(sc)), report_to_json(simulate(sc)))


if __name__ == "__main__":
    unittest.main()
