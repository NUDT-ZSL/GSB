"""逐回合推演引擎。

定点整数修正规则（舍入规则）：
  修正后值 = 修正前值 * 千分比因子 // 1000（整数除法，向下取整）。
  同一回合内先按配置顺序施加天气事件，再施加客流系数；
  每施加一个因子立即向下取整一次，并记录一条 ModifierTrace。
  全部运算为整数运算，无浮点，同一输入重复推演结果逐比特一致。

拒绝政策（policy）：
  skip     整条指令不执行（先整路线预检），记录告警，继续后续回合（默认）；
  truncate 逐段执行 MOVE，走到资源不足为止，已走段落生效；
  abort    记录告警并立即中止整局推演。
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from .graph import Graph, base_stamina_cost, base_supply_cost
from .model import (Alert, Command, FacilityScore, ModifierTrace, Report,
                    RoundRecord, Scenario, SegmentCost)

POLICIES = ("skip", "truncate", "abort")


def _modifiers_for_round(sc: Scenario, round_no: int) -> List[Tuple[str, str, int]]:
    """返回 [(source, field, factor), ...]，天气按配置顺序在前，客流在后。"""
    mods: List[Tuple[str, str, int]] = []
    for w in sc.weather:
        if w.round_no == round_no:
            if w.stamina_factor != 1000:
                mods.append((f"weather:{w.name}", "stamina", w.stamina_factor))
            if w.supply_factor != 1000:
                mods.append((f"weather:{w.name}", "supply", w.supply_factor))
    for c in sc.crowd:
        if c.round_no == round_no:
            if c.stamina_factor != 1000:
                mods.append(("crowd", "stamina", c.stamina_factor))
            if c.supply_factor != 1000:
                mods.append(("crowd", "supply", c.supply_factor))
    return mods


def _apply(value: int, mods, field: str, traces: List[ModifierTrace]) -> int:
    for source, f, factor in mods:
        if f != field:
            continue
        after = value * factor // 1000
        traces.append(ModifierTrace(source=source, field=field, factor=factor,
                                    before=value, after=after))
        value = after
    return value


class Engine:
    def __init__(self, sc: Scenario, policy: str = "skip"):
        if policy not in POLICIES:
            raise ValueError(f"未知拒绝政策: {policy}")
        self.sc = sc
        self.policy = policy
        self.graph = Graph(sc.topology)
        self.max_stamina = sc.init_stamina

    # ------------------------------------------------------------ 主流程
    def simulate(self) -> Report:
        sc = self.sc
        node = sc.start_node
        stamina, supply = sc.init_stamina, sc.init_supply
        rounds: List[RoundRecord] = []
        alerts: List[Alert] = []
        trajectory: List[str] = []
        total_st = total_sp = 0
        visit_round: Dict[str, int] = {}
        stay_rounds: Dict[str, int] = {}
        target_ptr = 0
        aborted = False

        for r in range(1, sc.total_rounds + 1):
            cmd = sc.commands[r - 1] if r - 1 < len(sc.commands) else Command("WAIT")
            mods = _modifiers_for_round(sc, r)
            rec = RoundRecord(
                round_no=r, command=self._cmd_str(cmd), node_before=node,
                node_after=node, stamina_before=stamina, stamina_after=stamina,
                supply_before=supply, supply_after=supply)
            if aborted:
                rec.executed = False
                rec.note = "已中止：跳过剩余回合"
                rounds.append(rec)
                trajectory.append(f"R{r:02d} ABORTED")
                continue

            if cmd.kind == "MOVE":
                node, stamina, supply, aborted = self._do_move(
                    cmd, r, mods, rec, alerts, node, stamina, supply)
            elif cmd.kind == "RESUPPLY":
                supply = self._do_resupply(cmd, r, rec, alerts, node, supply)
            elif cmd.kind == "REST":
                stamina = self._do_rest(cmd, r, rec, alerts, node, stamina)
            else:
                rec.note = rec.note or "停留"

            rec.node_after = node
            rec.stamina_after = stamina
            rec.supply_after = supply
            total_st += sum(s.final_stamina for s in rec.segments)
            total_sp += sum(s.final_supply for s in rec.segments)
            rec.alerts = [a for a in alerts if a.round_no == r]
            rounds.append(rec)

            # 目标设施按顺序访问判定
            if target_ptr < len(sc.targets) and node == sc.targets[target_ptr]:
                if node not in visit_round:
                    visit_round[node] = r
                target_ptr += 1
            if node in visit_round:
                stay_rounds[node] = stay_rounds.get(node, 0) + 1

            trajectory.append(self._traj_line(rec))
            if aborted:
                alerts.append(Alert(r, node, "ABORT",
                                    "拒绝政策为 abort，推演中止", "abort"))

        scores, unvisited, overall = self._score(visit_round, stay_rounds, rounds)
        return Report(rounds=rounds, alerts=alerts, facility_scores=scores,
                      unvisited=unvisited, overall_score=overall,
                      total_stamina_spent=total_st, total_supply_spent=total_sp,
                      trajectory=trajectory)

    # ------------------------------------------------------------ 指令执行
    def _cost_segments(self, cmd, mods) -> List[SegmentCost]:
        """把 MOVE 路线展开为修正后的逐段消耗（不扣资源）。"""
        raw = self.graph.route_segments(list(cmd.waypoints))
        if raw is None:
            return None  # type: ignore[return-value]
        segments: List[SegmentCost] = []
        for frm, to, dist in raw:
            traces: List[ModifierTrace] = []
            fst = _apply(base_stamina_cost(dist), mods, "stamina", traces)
            fsp = _apply(base_supply_cost(dist), mods, "supply", traces)
            segments.append(SegmentCost(
                frm=frm, to=to, distance=dist,
                base_stamina=base_stamina_cost(dist),
                base_supply=base_supply_cost(dist),
                final_stamina=fst, final_supply=fsp, modifiers=traces))
        return segments

    def _do_move(self, cmd, r, mods, rec, alerts, node, stamina, supply):
        segments = self._cost_segments(cmd, mods)
        if segments is None:
            alerts.append(Alert(r, node, "UNREACHABLE",
                                f"路线存在不可达段: {'->'.join(cmd.waypoints)}",
                                self.policy))
            rec.executed = False
            rec.note = "路线不可达，指令被拒绝"
            return node, stamina, supply, self.policy == "abort"

        if self.policy == "truncate":
            return self._move_truncate(segments, r, rec, alerts,
                                       node, stamina, supply)

        # skip / abort：整路线预检，资源不足则整段拒绝，不产生任何消耗
        need_st = sum(s.final_stamina for s in segments)
        need_sp = sum(s.final_supply for s in segments)
        if stamina < need_st:
            alerts.append(Alert(r, node, "INSUFFICIENT_STAMINA",
                                f"体力不足：整路线需要 {need_st}，剩余 {stamina}",
                                self.policy))
            rec.executed = False
            rec.note = "体力不足，指令被拒绝"
            return node, stamina, supply, self.policy == "abort"
        if supply < need_sp:
            alerts.append(Alert(r, node, "INSUFFICIENT_SUPPLY",
                                f"补给不足：整路线需要 {need_sp}，剩余 {supply}",
                                self.policy))
            rec.executed = False
            rec.note = "补给不足，指令被拒绝"
            return node, stamina, supply, self.policy == "abort"
        rec.segments = segments
        end = segments[-1].to if segments else node
        return end, stamina - need_st, supply - need_sp, False

    def _move_truncate(self, segments, r, rec, alerts, node, stamina, supply):
        cur = node
        done: List[SegmentCost] = []
        for seg in segments:
            if stamina < seg.final_stamina:
                alerts.append(Alert(r, cur, "INSUFFICIENT_STAMINA",
                                    f"体力不足：段 {seg.frm}->{seg.to} 需要 "
                                    f"{seg.final_stamina}，剩余 {stamina}，路线截断",
                                    "truncate"))
                rec.note = "体力不足，路线截断"
                break
            if supply < seg.final_supply:
                alerts.append(Alert(r, cur, "INSUFFICIENT_SUPPLY",
                                    f"补给不足：段 {seg.frm}->{seg.to} 需要 "
                                    f"{seg.final_supply}，剩余 {supply}，路线截断",
                                    "truncate"))
                rec.note = "补给不足，路线截断"
                break
            stamina -= seg.final_stamina
            supply -= seg.final_supply
            cur = seg.to
            done.append(seg)
        rec.segments = done
        if not done:
            rec.executed = False
            rec.note = rec.note or "资源不足，指令被拒绝"
        return cur, stamina, supply, False

    def _do_resupply(self, cmd, r, rec, alerts, node, supply):
        if self.sc.topology.nodes[node].kind != "supply":
            alerts.append(Alert(r, node, "INVALID_LOCATION",
                                f"当前节点 {node} 不是补给站，无法补给", "skip"))
            rec.executed = False
            rec.note = "非补给站，指令被拒绝"
            return supply
        new = min(self.sc.supply_capacity, supply + cmd.amount)
        if new < supply + cmd.amount:
            rec.note = f"补给超出容量，截断至容量 {self.sc.supply_capacity}"
        return new

    def _do_rest(self, cmd, r, rec, alerts, node, stamina):
        if self.sc.topology.nodes[node].kind != "rest":
            alerts.append(Alert(r, node, "INVALID_LOCATION",
                                f"当前节点 {node} 不是休息区，无法休息", "skip"))
            rec.executed = False
            rec.note = "非休息区，指令被拒绝"
            return stamina
        new = min(self.max_stamina, stamina + cmd.amount)
        if new < stamina + cmd.amount:
            rec.note = f"体力恢复超出上限，截断至 {self.max_stamina}"
        return new

    # ------------------------------------------------------------ 评分
    def _score(self, visit_round, stay_rounds, rounds):
        """满意度整数评分（0..100）：

        score = 40 基础分
              + 到达时剩余体力比（千分比）* 30 // 1000
              + 到达回合补给结余比（千分比）* 20 // 1000
              + 访问时长占比（停留回合/总回合，千分比）* 10 // 1000
        未访问设施 score = 0 并单独列出；整体评分 = 总分 // 目标数
        （未访问按 0 计入分母，口径固定）。
        """
        sc = self.sc
        scores: List[FacilityScore] = []
        unvisited: List[str] = []
        total = 0
        for t in sc.targets:
            if t not in visit_round:
                unvisited.append(t)
                scores.append(FacilityScore(t, False, None, 0, {}))
                continue
            vr = visit_round[t]
            rec = rounds[vr - 1]
            st_ratio = rec.stamina_after * 1000 // self.max_stamina
            sp_ratio = rec.supply_after * 1000 // sc.supply_capacity
            du_ratio = stay_rounds.get(t, 1) * 1000 // sc.total_rounds
            detail = {
                "base": 40,
                "stamina_points": st_ratio * 30 // 1000,
                "supply_points": sp_ratio * 20 // 1000,
                "duration_points": du_ratio * 10 // 1000,
            }
            score = min(100, sum(detail.values()))
            total += score
            scores.append(FacilityScore(t, True, vr, score, detail))
        overall = total // len(sc.targets) if sc.targets else 0
        return scores, unvisited, overall

    # ------------------------------------------------------------ 展示
    @staticmethod
    def _cmd_str(cmd: Command) -> str:
        if cmd.kind == "MOVE":
            return "MOVE " + "->".join(cmd.waypoints)
        if cmd.kind in ("RESUPPLY", "REST"):
            return f"{cmd.kind} {cmd.amount}"
        return "WAIT"

    @staticmethod
    def _traj_line(rec: RoundRecord) -> str:
        dst = f"{rec.stamina_after - rec.stamina_before:+d}"
        dsp = f"{rec.supply_after - rec.supply_before:+d}"
        flag = "ok" if rec.executed else "rej"
        return (f"R{rec.round_no:02d} {rec.command} "
                f"@ {rec.node_before}->{rec.node_after} "
                f"st={rec.stamina_after}({dst}) sp={rec.supply_after}({dsp}) {flag}")


def simulate(sc: Scenario, policy: str = "skip") -> Report:
    return Engine(sc, policy).simulate()


def changed_rounds(old: Report, new: Report) -> List[int]:
    """对比两份报告，返回消耗明细发生变化的回合号列表。"""
    from .report import round_to_dict
    changed = []
    old_map = {r.round_no: r for r in old.rounds}
    for rec in new.rounds:
        o = old_map.get(rec.round_no)
        if o is None or round_to_dict(o) != round_to_dict(rec):
            changed.append(rec.round_no)
    return changed
