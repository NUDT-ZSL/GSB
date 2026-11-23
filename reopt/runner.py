"""运行编排：逐批生成、提交、执行、观测、漂移裁决与形态切换。

每个批次在一个事务内完成“提交批次 + 记录观测 + 漂移触发 + 形态切换”，
进程在批次执行或形态切换中途被强制终止后不会留下部分状态；
恢复路径只重放水位之后的批次，且观测与逻辑时钟累计幂等。
"""

from __future__ import annotations

import json
import os

from . import datagen, executor, reoptimizer
from .drift import evaluate_step
from .errors import (
    E_DIGEST_MISMATCH,
    E_INJECTION_MANIFEST_INVALID,
    E_OBSERVATION_SAMPLE_ZERO,
    E_SWITCH_BUDGET_EXCEEDED,
    BusinessError,
    err,
)
from .ledger import ErrorLedger, LedgerEntry
from .schema import (
    BatchSpec,
    Config,
    Plan,
    parse_batches,
    parse_config,
    parse_plan,
    parse_tables,
    predicates_from_plan,
)
from .state import StateStore

INJECTION_PHASES = ("generate", "observe", "switch")
INJECTION_KINDS = {
    "generate": ("digest_mismatch",),
    "observe": ("zero_sample",),
    "switch": ("illegal_budget",),
}


def parse_injections(payload: dict) -> tuple[dict, ...]:
    raw = payload.get("injections")
    if not isinstance(raw, list):
        raise err(E_INJECTION_MANIFEST_INVALID, "injections 必须为数组")
    for item in raw:
        phase = item.get("phase")
        kind = item.get("kind")
        if phase not in INJECTION_PHASES or kind not in INJECTION_KINDS.get(phase, ()):
            raise err(E_INJECTION_MANIFEST_INVALID,
                      f"非法注入项: phase={phase} kind={kind}")
    return tuple(dict(item) for item in raw)


def _plan_to_payload(plan: Plan) -> dict:
    return {
        "algorithms_enum": list(plan.algorithms_enum),
        "steps": [
            {"left": s.left, "right": s.right, "left_key": s.left_key,
             "right_key": s.right_key, "algorithm": s.algorithm,
             "est_selectivity": s.est_selectivity, "est_cost": s.est_cost}
            for s in plan.steps
        ],
    }


def _order_of_plan(plan: Plan) -> tuple[str, ...]:
    order = [plan.steps[0].left]
    order.extend(step.right for step in plan.steps)
    return tuple(order)


class Runner:
    def __init__(self, store: StateStore, seed: int, tables: dict,
                 batches: tuple[BatchSpec, ...], config: Config):
        self.store = store
        self.seed = seed
        self.tables = tables
        self.batches = {spec.seq: spec for spec in batches}
        self.config = config

    # ---- load / open ----
    @classmethod
    def load(cls, state_dir: str, tables_payload: dict, plan_payload: dict,
             batches_payload: dict, config_payload: dict) -> "Runner":
        """解析校验全部输入；非法输入抛业务错误码且不写入任何持久化状态。"""
        seed, tables = parse_tables(tables_payload)
        plan = parse_plan(plan_payload, tables)
        batches = parse_batches(batches_payload, tables)
        config = parse_config(config_payload)
        if os.path.exists(os.path.join(state_dir, "state.db")):
            raise err(E_INJECTION_MANIFEST_INVALID,
                      f"状态目录已初始化: {state_dir}")
        store = StateStore(state_dir)
        with store.transaction() as conn:
            store.set_meta(conn, "loaded", "1")
            store.set_meta(conn, "seed", str(seed))
            store.set_meta(conn, "tables_json", json.dumps(tables_payload, sort_keys=True))
            store.set_meta(conn, "batches_json", json.dumps(batches_payload, sort_keys=True))
            store.set_meta(conn, "config_json", json.dumps(config_payload, sort_keys=True))
            store.set_meta(conn, "watermark", "0")
            store.set_meta(conn, "first_seq", str(batches[0].seq))
            store.set_meta(conn, "logical_clock", "0")
            store.set_meta(conn, "form_version", "1")
            store.set_meta(conn, "form_json",
                           json.dumps(_plan_to_payload(plan), sort_keys=True))
        return cls(store, seed, tables, batches, config)

    @classmethod
    def open(cls, state_dir: str) -> "Runner":
        store = StateStore(state_dir)
        store.require_loaded()
        seed, tables = parse_tables(json.loads(store.get_meta("tables_json")))
        batches = parse_batches(json.loads(store.get_meta("batches_json")), tables)
        config = parse_config(json.loads(store.get_meta("config_json")))
        return cls(store, seed, tables, batches, config)

    def current_plan(self) -> Plan:
        return parse_plan(self.store.current_form(), self.tables)

    # ---- 数据再生（确定性，恢复时只重放水位之后的批次） ----
    def _generate(self, table_name: str, seq: int):
        table = self.tables[table_name]
        nrows = self.batches[seq].rows[table_name]
        return datagen.generate_batch(table, self.seed, seq, nrows)

    def _base_relations_after_commit(self, seq: int):
        """构造包含本批次在内的已提交批次基表（本批尚未落库，显式并入）。"""
        committed = self.store.committed_batches()
        base = {}
        for name, table in self.tables.items():
            batches = [self._generate(name, b["seq"]) for b in committed]
            batches.append(self._generate(name, seq))
            base[name] = executor.build_base_relation(table, batches)
        return base

    def _matching_injections(self, injections, phase: str, seq: int | None = None):
        return [inj for inj in injections or ()
                if inj.get("phase") == phase
                and (seq is None or inj.get("batch_seq") in (None, seq))]

    # ---- 主循环 ----
    def run(self, until_seq: int | None = None,
            injections: tuple[dict, ...] | None = None) -> dict:
        self.store.require_loaded()
        summary = {"committed": [], "triggers": [], "switches": [],
                   "precision_sensitive": [], "errors": []}
        for seq in sorted(self.batches):
            if seq <= self.store.watermark():
                continue  # 恢复路径：只推进水位之后的批次
            if until_seq is not None and seq > until_seq:
                break
            self._run_one_batch(seq, injections or (), summary)
        return summary

    def _run_one_batch(self, seq: int, injections: tuple[dict, ...],
                       summary: dict) -> None:
        spec = self.batches[seq]
        scale = self.config.fp_scale
        with self.store.transaction() as conn:
            if seq <= self.store.watermark():
                # 并发实例已提交该批次：事务内复查并跳过，保证只提交一次
                summary["committed"].append(seq)
                return
            form_version = self.store.form_version()
            plan = self.current_plan()
            ledger = ErrorLedger(scale)
            # 逻辑时钟：到达时钟与水位时钟 + 步进取大，保证单调
            clock_ms = max(spec.clock_ms,
                           self.store.logical_clock() + self.config.clock_step_ms)
            # 1) 生成阶段：确定性产出列式批次与每批摘要
            table_digests = {}
            for name, table in self.tables.items():
                columns = self._generate(name, seq)
                order = tuple(c.name for c in table.columns)
                table_digests[name] = datagen.batch_digest(name, seq, columns, order)
            for _ in self._matching_injections(injections, "generate", seq):
                raise err(E_DIGEST_MISMATCH,
                          f"批次 {seq} 生成阶段注入：摘要不匹配")
            digest = datagen.combined_digest(table_digests)
            # 2) 执行阶段：进行中的批次按原形态完成
            base = self._base_relations_after_commit(seq)
            intermediates, observations = executor.run_pipeline(plan, base)
            executor.verify_against_truth(plan, base, intermediates)
            res_digest = executor.result_digest(intermediates[-1])
            # 3) 观测阶段：只从已提交批次的完整结果导出实测观测
            for _ in self._matching_injections(injections, "observe", seq):
                raise err(E_OBSERVATION_SAMPLE_ZERO,
                          f"批次 {seq} 观测阶段注入：观测样本为零")
            self.store.commit_batch(conn, seq, form_version, digest, table_digests,
                                    spec.rows, res_digest, clock_ms)
            for obs in observations:
                sel_fp = ledger.to_fp(obs.output_rows, obs.product,
                                      f"obs:seq={seq}:step={obs.step_idx}")
                self.store.record_observation(
                    conn, seq, obs.step_idx, form_version,
                    obs.left_rows, obs.right_rows, obs.output_rows, sel_fp)
            # 4) 漂移判定与重优化触发裁决
            threshold_fp = int(self.config.drift_threshold * scale)
            obs_all = self.store.observations(form_version)
            for step_idx, step in enumerate(plan.steps):
                step_obs = [
                    executor.Observation(
                        seq=o["seq"], step_idx=o["step_idx"],
                        left_rows=o["left_rows"], right_rows=o["right_rows"],
                        output_rows=o["output_rows"])
                    for o in obs_all if o["step_idx"] == step_idx
                ]
                est_sel_fp = int(step.est_selectivity * scale)
                ledger.entries.append(LedgerEntry(
                    f"est:step={step_idx}", "to_fp", scale, "floor", 0, 1))
                verdict = evaluate_step(
                    step_obs, est_sel_fp, threshold_fp,
                    self.config.observation_window, ledger, step_idx,
                    source_table=step.right, join_key=step.right_key,
                    clock_ms=clock_ms,
                    last_trigger_clock=self.store.last_trigger_clock(),
                    cooldown_ms=self.config.cooldown_ms)
                if verdict is None or not verdict.triggered or verdict.blocked_by_cooldown:
                    continue
                self.store.record_trigger(conn, seq, verdict, clock_ms, form_version)
                summary["triggers"].append({
                    "seq": seq, "step_idx": verdict.step_idx,
                    "source_table": verdict.source_table,
                    "join_key": verdict.join_key, "ratio_fp": verdict.ratio_fp,
                    "window": [verdict.window_start, verdict.window_end],
                    "threshold_fp": verdict.threshold_fp,
                    "margin_fp": verdict.margin_fp})
                self._reoptimize(conn, seq, plan, form_version, ledger,
                                 verdict, clock_ms, injections, summary)
                break  # 每批次最多一次切换裁决
            self.store.record_ledger(conn, seq, ledger.entries)
            self.store.set_meta(conn, "logical_clock", str(clock_ms))
            summary["committed"].append(seq)

    # ---- 重优化与形态切换（批次边界） ----
    def _reoptimize(self, conn, seq: int, plan: Plan, form_version: int,
                    ledger: ErrorLedger, verdict, clock_ms: int,
                    injections: tuple[dict, ...], summary: dict) -> None:
        scale = self.config.fp_scale
        budget_fp = self.config.switch_cost_budget * scale
        for inj in self._matching_injections(injections, "switch", seq):
            budget_fp = int(inj.get("value", -1))  # 注入非法切换预算
        predicates = predicates_from_plan(plan, self.tables)
        table_names = sorted(self.tables)
        candidates = reoptimizer.enumerate_candidates(
            table_names, predicates, plan.algorithms_enum)
        current = reoptimizer.Candidate(
            order=_order_of_plan(plan),
            algorithms=tuple(s.algorithm for s in plan.steps))
        context = f"reopt:seq={seq}"
        # 实测基数：已提交批次的累计行数；实测选择率：当前形态观测的聚合
        card_rows = {name: 0 for name in self.tables}
        for batch in self.store.committed_batches():
            for name in self.tables:
                card_rows[name] += batch["rows"].get(name, 0)
        sel_overrides: dict[frozenset, int] = {}
        pairs = reoptimizer._step_pairs(current.order, predicates)
        obs_all = self.store.observations(form_version)
        for step_idx, pair in enumerate(pairs):
            step_obs = [o for o in obs_all if o["step_idx"] == step_idx]
            sum_out = sum(o["output_rows"] for o in step_obs)
            sum_prod = sum(o["left_rows"] * o["right_rows"] for o in step_obs)
            if sum_prod > 0:
                sel_overrides[frozenset(pair)] = ledger.to_fp(
                    sum_out, sum_prod, f"{context}:measured_sel:{step_idx}")
        costs = {}
        for cand in candidates:
            costs[cand] = reoptimizer.estimate_cost_fp(
                cand, self.tables, predicates,
                self.config.switch_cost * scale, ledger,
                f"{context}:{'-'.join(cand.order)}:{'-'.join(cand.algorithms)}",
                card_rows=card_rows, sel_overrides=sel_overrides)
        tolerance_fp = max(1, int(self.config.float_tolerance * scale))
        try:
            decision = reoptimizer.choose_form(
                current, candidates, costs, budget_fp, tolerance_fp, ledger, context)
        except BusinessError as exc:
            if exc.code == E_SWITCH_BUDGET_EXCEEDED:
                # 预算不足：保留原形态，返回独立业务错误码，批次仍然提交
                summary["errors"].append({"seq": seq, "code": exc.code,
                                          "message": exc.message})
                return
            raise
        if decision.precision_sensitive:
            steps_info = [
                {"context": e.context, "op": e.op, "scale": e.scale,
                 "rounding": e.rounding, "discarded": e.discarded,
                 "error_bound": e.error_bound}
                for e in ledger.entries if e.context.startswith(context)
            ]
            self.store.record_precision_flag(
                conn, seq, decision.sensitive_tables,
                verdict.window_start, verdict.window_end,
                steps_info, decision.error_bound_fp)
            summary["precision_sensitive"].append({
                "seq": seq, "tables": decision.sensitive_tables,
                "window": [verdict.window_start, verdict.window_end],
                "error_bound_fp": decision.error_bound_fp})
        if decision.chosen is None:
            return
        new_plan = reoptimizer.candidate_to_plan(
            decision.chosen, predicates, plan.algorithms_enum,
            est_selectivity=plan.steps[-1].est_selectivity,
            est_cost=decision.chosen_cost_fp / scale)
        self.store.switch_form(
            conn, form_version + 1, _plan_to_payload(new_plan), seq,
            decision.chosen.to_dict(), decision.rejected, clock_ms)
        summary["switches"].append({
            "seq": seq, "from_version": form_version,
            "to_version": form_version + 1,
            "chosen": decision.chosen.to_dict(),
            "rejected": decision.rejected})
