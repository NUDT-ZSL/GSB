"""并行调度引擎与报告汇总。

- ThreadPoolExecutor 并发解析，最大并发数取自配置；
- 预算扣减由 BudgetLedger 按 (句 ID, 步序号) 全序串行受理，保证与串行
  执行逐字段一致；
- 单句异常隔离到失败清单，其余句继续；
- 每完成一句即原子落盘结果并刷新 parse 阶段快照；
- 汇总阶段再写一次 summary 快照；
- SIGINT/SIGTERM 作为可处理退出信号：完成在途句、落盘快照后退出码 0。
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .budget import BudgetLedger
from .errors import (E_NOT_SCHEDULED, E_SENTENCE_EXCEPTION, NotScheduledError,
                     BenchError)
from .model import Token, TransitionModel, sha256_hex
from .parser import DependencyParser, ParseResult
from .snapshot import STAGE_PARSE, STAGE_SUMMARY, SnapshotStore


class ShutdownController:
    def __init__(self):
        self.event = threading.Event()
        self.signal_name: str | None = None

    def install(self) -> None:
        def handler(signum, _frame):
            self.signal_name = signal.Signals(signum).name
            self.event.set()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass

    @property
    def requested(self) -> bool:
        return self.event.is_set()


def _result_to_store_dict(result: ParseResult, extra: dict) -> dict:
    payload = result.sentence_dict()
    payload.update(extra)
    if result.failure is not None:
        payload["failure"] = result.failure
    return payload


class ParseEngine:
    def __init__(self, model: TransitionModel, config: dict,
                 sentences: list[tuple[str, list[Token]]],
                 snapshot_store: SnapshotStore | None = None,
                 shutdown: ShutdownController | None = None):
        self.model = model
        self.config = config
        self.sentences = sentences
        self.sentence_ids = [sid for sid, _ in sentences]
        self.store = snapshot_store
        self.shutdown = shutdown or ShutdownController()

        self.ledger = BudgetLedger(
            self.sentence_ids,
            config["global_step_budget"],
            config["per_sentence_quota"])
        self._lock = threading.Lock()
        self._serial_mode = False
        self._active = 0
        self.thread_peak = 0
        self.wait_ms: dict[str, float] = {}
        self.timeout_hits: dict[str, int] = {}
        self._dispatch_at: dict[str, float] = {}
        self.completed: dict[str, dict] = {}
        self.failures: list[dict] = []
        self.snapshot_version = 0

    # ---- 单句任务 -------------------------------------------------------
    def _run_one(self, sentence_id: str, tokens: list[Token]) -> None:
        queued_at = self._dispatch_at.pop(sentence_id, time.perf_counter())
        parser = DependencyParser(
            self.model,
            step_limit=self.config["max_steps_per_sentence"],
            quota=self.config["per_sentence_quota"],
            ledger=self.ledger)
        result: ParseResult
        try:
            result = parser.parse(sentence_id, tokens)
        except NotScheduledError:
            wait_ms = (time.perf_counter() - queued_at) * 1000.0
            self._record_observation(sentence_id, wait_ms)
            self.ledger.release(sentence_id)
            return
        except BenchError as exc:
            wait_ms = (time.perf_counter() - queued_at) * 1000.0
            self._record_observation(sentence_id, wait_ms)
            failure = exc.failure_record()
            if exc.code == E_SENTENCE_EXCEPTION:
                # 特征/打分阶段异常：补齐句 ID、步序号与异常类型
                failure["sentence_id"] = sentence_id
                failure["step_no"] = max(1, self.ledger.used_of(sentence_id))
                failure["exception_type"] = type(exc).__name__
                failure.setdefault("phase", "feature")
            result = ParseResult(
                sentence_id=sentence_id,
                status="failed",
                code=exc.code,
                steps_used=self.ledger.used_of(sentence_id),
                trace=[],
                arcs=[],
                log_score=None,
                failure=failure,
                exceeded_budget=(exc.code == "E_BUDGET_EXHAUSTED"),
                exceeded_step_limit=(exc.code == "E_STEP_LIMIT"),
            )
            with self._lock:
                self.failures.append(failure)
            self._finish_result(sentence_id, result)
            self.ledger.release(sentence_id)
            return
        except Exception as exc:  # 特征构造/打分等未知异常隔离
            wait_ms = (time.perf_counter() - queued_at) * 1000.0
            self._record_observation(sentence_id, wait_ms)
            record = {
                "code": E_SENTENCE_EXCEPTION,
                "message": str(exc),
                "phase": getattr(exc, "phase", "feature") or "feature",
                "sentence_id": sentence_id,
                "step_no": max(1, self.ledger.used_of(sentence_id)),
                "exception_type": type(exc).__name__,
            }
            result = ParseResult(
                sentence_id=sentence_id,
                status="failed",
                code=E_SENTENCE_EXCEPTION,
                steps_used=self.ledger.used_of(sentence_id),
                trace=[],
                arcs=[],
                log_score=None,
                failure=record,
            )
            with self._lock:
                self.failures.append(record)
            self._finish_result(sentence_id, result)
            self.ledger.release(sentence_id)
            return

        wait_ms = (time.perf_counter() - queued_at) * 1000.0
        self._record_observation(sentence_id, wait_ms)
        self._finish_result(sentence_id, result)
        self.ledger.release(sentence_id)

    def _record_observation(self, sentence_id: str, wait_ms: float) -> None:
        with self._lock:
            self.wait_ms[sentence_id] = round(wait_ms, 4)
            if wait_ms > self.config["timeout_observe_ms"]:
                self.timeout_hits[sentence_id] = 1

    def _finish_result(self, sentence_id: str, result: ParseResult) -> None:
        extra = {
            "quota": self.config["per_sentence_quota"],
            "step_limit": self.config["max_steps_per_sentence"],
            "exceeded_budget": result.exceeded_budget,
            "exceeded_step_limit": result.exceeded_step_limit,
        }
        payload = _result_to_store_dict(result, extra)
        with self._lock:
            self.completed[sentence_id] = payload
            self._active -= 1
            peak = max(self.thread_peak, self._active + 1)
            self.thread_peak = peak
            if self.store is not None:
                self.store.write_result(sentence_id, payload)
                ordered_ids = [sid for sid in self.sentence_ids
                               if sid in self.completed]
                self.snapshot_version += 1
                self.store.write_snapshot(
                    STAGE_PARSE, ordered_ids,
                    self.ledger.global_consumed, self.snapshot_version)

    # ---- 并发执行 -------------------------------------------------------
    def run(self) -> dict:
        max_workers = self.config["max_workers"]
        batch_size = self.config["batch_size"]
        index = dict(self.sentences)

        if max_workers == 1 and batch_size == 1:
            # 串行快路径：单线程按句 ID 顺序执行，天然满足全序预算扣减，
            # 也避免单工作线程在条件变量上自我等待造成死锁。
            self._serial_mode = True
            for sid, tokens in self.sentences:
                if self.shutdown.requested:
                    break
                if sid in self.completed:
                    continue
                with self._lock:
                    self._active += 1
                    self.thread_peak = max(self.thread_peak, self._active)
                    self._dispatch_at[sid] = time.perf_counter()
                self._run_one(sid, tokens)
            self._finalize_unscheduled()
            self._write_summary_snapshot()
            return self.build_report()

        with ThreadPoolExecutor(max_workers=max_workers,
                                thread_name_prefix="dp") as pool:
            futures = []
            for start in range(0, len(self.sentence_ids), batch_size):
                if self.shutdown.requested:
                    break
                batch = self.sentence_ids[start:start + batch_size]
                for sid in batch:
                    if self.shutdown.requested:
                        break
                    if sid in self.completed:
                        # 恢复路径：该句已由快照完整恢复，不再调度
                        continue
                    with self._lock:
                        self._active += 1
                        self.thread_peak = max(self.thread_peak,
                                               self._active)
                        self._dispatch_at[sid] = time.perf_counter()
                    futures.append(pool.submit(self._run_one, sid,
                                               index[sid]))
            for future in futures:
                future.result()

        self._finalize_unscheduled()
        self._write_summary_snapshot()
        return self.build_report()

    def _finalize_unscheduled(self) -> None:
        # 预算耗尽后仍未完成的句 ID：按升序逐一标记未调度
        for sid in self.ledger.mark_unscheduled():
            if sid in self.completed:
                continue
            if any(f.get("sentence_id") == sid and
                   f.get("code") == E_NOT_SCHEDULED for f in self.failures):
                continue
            record = NotScheduledError(
                "全局预算已耗尽，按句 ID 升序标记为未调度",
                phase="schedule", sentence_id=sid).failure_record()
            payload = {
                "sentence_id": sid,
                "status": "unscheduled",
                "code": E_NOT_SCHEDULED,
                "steps_used": 0,
                "log_score": None,
                "arcs": [],
                "trace": [],
                "quota": self.config["per_sentence_quota"],
                "step_limit": self.config["max_steps_per_sentence"],
                "exceeded_budget": False,
                "exceeded_step_limit": False,
                "failure": record,
            }
            self.completed[sid] = payload
            self.failures.append(record)
            if self.store is not None:
                self.store.write_result(sid, payload)

    def _write_summary_snapshot(self) -> None:
        if self.store is not None:
            ordered_ids = [sid for sid in self.sentence_ids
                           if sid in self.completed]
            self.snapshot_version += 1
            self.store.write_snapshot(
                STAGE_SUMMARY, ordered_ids,
                self.ledger.global_consumed, self.snapshot_version)

    # ---- 报告 -----------------------------------------------------------
    def build_report(self, resume_info: dict | None = None) -> dict:
        sentences = []
        scores = []
        per_sentence = []
        for sid in self.sentence_ids:
            payload = self.completed.get(sid)
            if payload is None:
                # 退出信号导致尚未调度
                continue
            sentences.append({
                "sentence_id": sid,
                "status": payload["status"],
                "code": payload["code"],
                "steps_used": payload["steps_used"],
                "log_score": payload["log_score"],
                "arcs": payload["arcs"],
                "trace": payload["trace"],
            })
            scores.append({"sentence_id": sid,
                           "log_score": payload["log_score"]})
            per_sentence.append({
                "sentence_id": sid,
                "used": payload["steps_used"],
                "quota": payload["quota"],
                "step_limit": payload["step_limit"],
                "exceeded_budget": payload["exceeded_budget"],
                "exceeded_step_limit": payload["exceeded_step_limit"],
            })

        failures = sorted(
            self.failures,
            key=lambda rec: (str(rec.get("sentence_id")),
                             rec.get("step_no") or 0,
                             rec.get("code") or ""))

        report = {
            "report_version": 1,
            "config": self.config,
            "config_hash": sha256_hex(self.config),
            "model_hash": self.model.content_hash(),
            "sentences": sentences,
            "scores": scores,
            "failures": failures,
            "budget": {
                "global_budget": self.ledger.global_budget,
                "global_consumed": self.ledger.global_consumed,
                "global_remaining": self.ledger.global_remaining,
                "per_sentence": per_sentence,
                "consumption_curve": self.ledger.curve(),
                "deduction_sequence": self.ledger.deductions(),
            },
            "observability": {
                "thread_peak": self.thread_peak,
                "timeout_observe_ms": self.config["timeout_observe_ms"],
                "wait_exceeded": sorted(self.timeout_hits.keys()),
                "max_wait_ms": round(
                    max(self.wait_ms.values()), 4)
                    if self.wait_ms else 0.0,
            },
            "consistency": {
                "determinism_digest": self.determinism_digest(sentences,
                                                               failures),
            },
            "resume": resume_info or {"resumed_from": None,
                                      "snapshot_completed": 0},
            "shutdown_signal": self.shutdown.signal_name,
        }
        return report

    def determinism_digest(self, sentences: list[dict],
                           failures: list[dict]) -> str:
        """确定性核心摘要：剔除耗时等非确定字段后的 SHA-256。"""
        core = {
            "sentences": [{
                "sentence_id": s["sentence_id"],
                "status": s["status"],
                "code": s["code"],
                "steps_used": s["steps_used"],
                "log_score": s["log_score"],
                "arcs": s["arcs"],
                "trace": s["trace"],
            } for s in sentences],
            "failures": failures,
            "deduction_sequence": self.ledger.deductions(),
            "curve": self.ledger.curve(),
        }
        return hashlib.sha256(
            json.dumps(core, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")).hexdigest()
