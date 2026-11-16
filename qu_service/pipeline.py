"""管道编排：分阶段处理、线程池并发、失败隔离、快照/恢复、信号退出语义。

确定性保证：
  - 样本按输入文件顺序处理；并发仅发生在单个 chunk 内部；
  - chunk 内结果先按查询 ID 收集，再按输入顺序确定性合并；
  - 每条样本任务携带由查询 ID 与阶段名派生的稳定排序键；
  - 任何并发度下最终输出与单线程逐字段一致。
"""
from __future__ import annotations

import json
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import injection as inj
from .errors import (E_INTERNAL, ForcedTermination, GracefulExit, StageError)
from .hashing import file_sha256, fingerprint, sha256_text, stable_sort_key
from .intent import score_intents
from .normalize import normalize_text
from .report import build_report, write_report
from .rewrite import generate_rewrites, validate_rules
from .slots import extract_slots
from .snapshot import STAGES, clear_snapshots, read_snapshot, write_snapshot


def _load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict]:
    records = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


class Pipeline:
    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)
        self.config = _load_json(self.config_path)
        base = self.config_path.parent

        def resolve(p):
            p = Path(p)
            return p if p.is_absolute() else (base / p).resolve()

        data_cfg = self.config["data"]
        self.queries_path = resolve(data_cfg["queries"])
        self.vocab_path = resolve(data_cfg["intent_vocab"])
        self.dict_path = resolve(data_cfg["entity_dict"])
        self.rules_path = resolve(data_cfg["rewrite_rules"])
        self.injection_path = resolve(data_cfg["injection"]) if data_cfg.get("injection") else None
        self.snapshot_dir = resolve(self.config["snapshot_dir"])
        self.report_path = resolve(self.config["report_path"])

        self.run_name = self.config.get("run_name", "run")
        self.max_workers = max(1, int(self.config.get("max_workers", 1)))
        self.max_rewrites = max(1, int(self.config.get("max_rewrites", 5)))
        self.snapshot_interval = max(1, int(self.config.get("snapshot_interval", 4)))
        # 仅用于实验台模拟强制终止：完成指定阶段后立即异常退出（快照已持久化）
        self.stop_after_stage = self.config.get("stop_after_stage")

        # 配置哈希：配置文件本身 + 全部数据文件，保证任一变更都会使快照失效
        hash_parts = [self.config_path.read_bytes(), self.queries_path.read_bytes(),
                      self.vocab_path.read_bytes(), self.dict_path.read_bytes(),
                      self.rules_path.read_bytes()]
        import hashlib
        self.config_hash = hashlib.sha256(b"".join(hash_parts)).hexdigest()
        self.injection_hash = (file_sha256(self.injection_path)
                               if self.injection_path else sha256_text(""))

        # 加载数据
        self.queries = _load_jsonl(self.queries_path)
        self.vocab = _load_json(self.vocab_path)
        raw_dicts = _load_json(self.dict_path)["dictionaries"]
        self.rules = _load_json(self.rules_path)["rules"]

        # 注入实验台
        self.plan = inj.load_injection(self.injection_path)
        inj.apply_to_queries(self.queries, self.plan)
        self.dictionaries = inj.apply_to_dicts(raw_dicts, self.plan)

        # 规则引用校验（在注入后的词典上进行）：缺失引用 -> E_RULE_MISSING 并跳过
        self.valid_rules, self.rule_missing = validate_rules(self.rules, self.dictionaries)

        # 每条样本任务携带的稳定排序键：sha256(query_id::stage)
        self.sort_keys = {
            q["query_id"]: {stage: stable_sort_key(q["query_id"], stage) for stage in STAGES}
            for q in self.queries
        }
        self.query_order = [q["query_id"] for q in self.queries]
        self._stop_event = threading.Event()

    # ---- 单样本单阶段处理（纯函数式，线程安全） ----

    def _stage_compute(self, stage: str, query: dict, results: dict) -> dict:
        qid = query["query_id"]
        if stage == "normalize":
            return normalize_text(query.get("text"))
        norm = results["normalize"][qid]
        text = norm["text"]
        if stage == "intent":
            return score_intents(text, self.vocab, self.valid_rules,
                                 forced_scores=self.plan.score_oor.get(qid))
        if stage == "slots":
            return extract_slots(text, self.dictionaries)
        if stage == "rewrite":
            slots = results["slots"][qid]
            return generate_rewrites(text, slots["slots"], self.valid_rules,
                                     self.max_rewrites, self.plan.rule_conflicts)
        raise ValueError(f"unknown stage {stage}")

    def _safe_stage(self, stage: str, query: dict, results: dict, failed: set) -> tuple:
        """失败隔离：返回 (query_id, result|None, failure|None)。"""
        qid = query["query_id"]
        if qid in failed:
            return qid, None, None
        try:
            return qid, self._stage_compute(stage, query, results), None
        except StageError as exc:
            failure = {"query_id": qid, "stage": stage, "code": exc.code,
                       "exc_type": type(exc).__name__,
                       "sort_key": self.sort_keys[qid][stage]}
            return qid, None, failure
        except Exception as exc:  # 未预期异常同样隔离
            failure = {"query_id": qid, "stage": stage, "code": E_INTERNAL,
                       "exc_type": type(exc).__name__,
                       "sort_key": self.sort_keys[qid][stage]}
            return qid, None, failure

    # ---- 信号处理：收到可处理信号时，当前 chunk 快照已持久化，随后以 0 退出 ----

    def _install_signal_handlers(self):
        if threading.current_thread() is not threading.main_thread():
            return []
        previous = []
        def handler(signum, frame):
            self._stop_event.set()
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous.append((sig, signal.signal(sig, handler)))
        return previous

    def _restore_signal_handlers(self, previous):
        for sig, old in previous:
            signal.signal(sig, old)

    # ---- 主流程 ----

    def run(self, resume: bool = False) -> dict:
        if not resume:
            clear_snapshots(self.snapshot_dir)

        results: dict[str, dict] = {stage: {} for stage in STAGES}
        failures: list[dict] = []
        failed_qids: set[str] = set()
        timings: dict[str, float] = {}

        # 恢复：逐阶段校验并加载快照；哈希不符 -> E_CONFIG_MISMATCH 拒绝恢复
        if resume:
            for stage in STAGES:
                snap = read_snapshot(self.snapshot_dir, stage, self.config_hash,
                                     self.injection_hash)
                if snap is None:
                    break
                results[stage] = dict(snap["results"])
                failures = list(snap["failures"])
            failed_qids = {f["query_id"] for f in failures}

        previous_handlers = self._install_signal_handlers()
        try:
            for stage in STAGES:
                t0 = time.perf_counter()
                cursor = len(results[stage])
                pending = self.queries[cursor:]
                for start in range(0, len(pending), self.snapshot_interval):
                    if self._stop_event.is_set():
                        raise GracefulExit(f"stop signal at stage={stage} cursor={cursor}")
                    chunk = pending[start:start + self.snapshot_interval]
                    outcomes = {}
                    with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                        futures = [pool.submit(self._safe_stage, stage, q, results, failed_qids)
                                   for q in chunk]
                        for fut in futures:
                            qid, result, failure = fut.result()
                            outcomes[qid] = (result, failure)
                    # 确定性合并：严格按输入顺序
                    for query in chunk:
                        qid = query["query_id"]
                        result, failure = outcomes[qid]
                        results[stage][qid] = result
                        if failure is not None:
                            failures.append(failure)
                            failed_qids.add(qid)
                    cursor += len(chunk)
                    # 阶段快照：临时文件 + 原子替换
                    write_snapshot(self.snapshot_dir, stage, self.config_hash,
                                   self.injection_hash, cursor, results[stage], failures)
                timings[stage] = round(time.perf_counter() - t0, 6)
                if self.stop_after_stage == stage and not resume:
                    raise ForcedTermination(f"simulated crash after stage={stage}")
        finally:
            self._restore_signal_handlers(previous_handlers)

        report = build_report(
            run_name=self.run_name,
            config_hash=self.config_hash,
            injection_hash=self.injection_hash,
            queries=self.queries,
            query_order=self.query_order,
            results=results,
            failures=failures,
            rule_missing=self.rule_missing,
            timings=timings,
            vocab=self.vocab,
        )
        write_report(self.report_path, report)
        return report
