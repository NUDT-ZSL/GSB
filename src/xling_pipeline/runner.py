"""管线编排器：四阶段分批执行、快照恢复、线程池并发与优雅退出。"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor

from .bridge import build_translation_index
from .config import config_hash, translation_hash
from .errors import E_ITEM_FAILED, make_failure
from .evaluation import STAGE as EVAL_STAGE, evaluate, load_qrels
from .index import MultiLanguageIndex
from .loader import STAGE as NORM_STAGE, load_all, normalize_corpus
from .scorer import STAGE as RETRIEVAL_STAGE, score_query
from .snapshot import SnapshotStore, SNAPSHOT_VERSION
from .io_utils import atomic_write_json

STAGES = (NORM_STAGE, "index", RETRIEVAL_STAGE, EVAL_STAGE)


class ExitController:
    """统一管理操作系统退出信号与配置故障注入请求。"""

    def __init__(self):
        self._event = threading.Event()
        self.reason = ""

    def request_exit(self, reason):
        self.reason = reason
        self._event.set()

    @property
    def exit_requested(self):
        return self._event.is_set()


class PipelineRunner:
    def __init__(self, config, exit_controller=None):
        self.config = config
        self.exit_controller = exit_controller or ExitController()
        self.store = SnapshotStore(config["paths"]["snapshot_dir"])
        self.cfg_hash = config_hash(config)
        self.inputs = load_all(config)
        self.translations = self.inputs["translations"]
        self.trans_hash = translation_hash(self.translations)
        self.translation_index = build_translation_index(self.translations)

        self.state = {
            "docs": [],
            "queries": [],
            "index_state": None,
            "retrieval_results": [],
            "evaluation_metrics": None,
            "failures": [],
            "stage_batches": {stage: [] for stage in STAGES},
            "reused_batches": 0,
            "reused_items": 0,
            "injected_exits": {},
            "processed_items": 0,
            "invalid_items": 0,
            "seen_doc_ids": [],
            "seen_query_ids": [],
        }
        self.version = 0
        self.stage_index = 0
        self.stage_cursor = {}
        self.exited_early = False
        self._seen_doc_ids = set()
        self._seen_query_ids = set()

    # ---------- 恢复 ----------
    def resume_if_possible(self):
        payload = self.store.load_latest()
        if payload is None:
            return None
        self.store.validate_resume(payload, self.cfg_hash, self.trans_hash)
        self.version = payload["version"]
        saved_state = payload["state"]
        self.state["docs"] = saved_state["docs"]
        self.state["queries"] = saved_state["queries"]
        self.state["index_state"] = saved_state["index_state"]
        self.state["retrieval_results"] = saved_state["retrieval_results"]
        self.state["evaluation_metrics"] = saved_state["evaluation_metrics"]
        self.state["failures"] = saved_state["failures"]
        self.state["stage_batches"] = saved_state["stage_batches"]
        self.state["injected_exits"] = saved_state.get("injected_exits", {})
        self.state["reused_batches"] = payload["completed_batches"]
        self.state["reused_items"] = saved_state.get("reused_items", 0)
        self.state["invalid_items"] = saved_state.get("invalid_items", 0)
        self.state["processed_items"] = saved_state.get("processed_items", 0)
        self.state["seen_doc_ids"] = saved_state.get("seen_doc_ids", [])
        self.state["seen_query_ids"] = saved_state.get("seen_query_ids", [])
        self._seen_doc_ids = set(self.state["seen_doc_ids"])
        self._seen_query_ids = set(self.state["seen_query_ids"])
        for stage in STAGES:
            done = len(self.state["stage_batches"].get(stage, []))
            self.stage_cursor[stage] = done
        return payload

    # ---------- 快照 ----------
    def _snapshot(self, stage):
        self.version += 1
        completed_batches = sum(
            len(batches) for batches in self.state["stage_batches"].values()
        )
        payload = {
            "snapshot_version": SNAPSHOT_VERSION,
            "version": self.version,
            "config_hash": self.cfg_hash,
            "translation_hash": self.trans_hash,
            "stage": stage,
            "completed_batches": completed_batches,
            "state": {
                "docs": self.state["docs"],
                "queries": self.state["queries"],
                "index_state": self.state["index_state"],
                "retrieval_results": self.state["retrieval_results"],
                "evaluation_metrics": self.state["evaluation_metrics"],
                "failures": self.state["failures"],
                "stage_batches": self.state["stage_batches"],
                "reused_items": self.state["reused_items"],
                "injected_exits": self.state.get("injected_exits", {}),
                "invalid_items": self.state["invalid_items"],
                "processed_items": self.state["processed_items"],
                "seen_doc_ids": sorted(self._seen_doc_ids),
                "seen_query_ids": sorted(self._seen_query_ids),
            },
            "summary": self._summary(stage, completed_batches),
        }
        self.store.commit(payload)
        return payload

    def _summary(self, stage, completed_batches):
        return {
            "stage": stage,
            "completed_batches": completed_batches,
            "docs_normalized": len(self.state["docs"]),
            "queries_normalized": len(self.state["queries"]),
            "index_built": self.state["index_state"] is not None,
            "queries_scored": len(self.state["retrieval_results"]),
            "evaluation_done": self.state["evaluation_metrics"] is not None,
            "failures": len(self.state["failures"]),
        }

    def _batch_size(self):
        return self.config["batch"]["batch_size"]

    def _should_commit(self, stage):
        interval = self.config["batch"]["snapshot_interval"]
        done = len(self.state["stage_batches"][stage])
        return done % interval == 0

    def _register_exit_requests(self, stage):
        """在批次快照提交前登记故障注入请求，确保标记随快照持久化。"""
        injection = self.config.get("fault_injection", {})
        target_stage = injection.get("exit_after_stage")
        target_batches = injection.get("exit_after_batches")
        stage_sequence_no = len(self.state["stage_batches"][stage])
        already_injected = self.state.get("injected_exits", {}).get(stage, 0)
        if (
            target_stage
            and stage == target_stage
            and target_batches is not None
            and already_injected == 0
            and stage_sequence_no >= target_batches
        ):
            self.state.setdefault("injected_exits", {})[stage] = stage_sequence_no
            self.exit_controller.request_exit(
                f"fault_injection:{stage}:{target_batches}"
            )

    def _check_exit(self, stage, batch_no):
        if self.exit_controller.exit_requested:
            self.exited_early = True
            return True
        return False

    @staticmethod
    def _chunks(items, size):
        for start in range(0, len(items), size):
            yield start // size, items[start:start + size]

    # ---------- 阶段一：归一化 ----------
    def run_normalize(self):
        stage = NORM_STAGE
        tokenizer = self.inputs["tokenizer"]
        vocab = self.inputs["translation_vocab"]
        # 文档与查询拼成统一作业流，保证批次号在阶段内全局连续
        jobs = [("document", row) for row in self.inputs["raw_docs"]]
        jobs += [("query", row) for row in self.inputs["raw_queries"]]
        start_batch = self.stage_cursor.get(stage, 0)
        for batch_no, chunk in self._chunks(jobs, self._batch_size()):
            if batch_no < start_batch:
                continue
            # 边界批次可能同时含文档与查询，按 kind 分组后分别归一化
            grouped = {}
            for kind, row in chunk:
                grouped.setdefault(kind, []).append(row)
            for kind, raw_rows in grouped.items():
                seen = (
                    self._seen_doc_ids if kind == "document"
                    else self._seen_query_ids
                )
                normalized, failures = normalize_corpus(
                    kind, raw_rows, self.config, tokenizer, vocab, seen
                )
                if kind == "document":
                    self.state["docs"].extend(normalized)
                else:
                    self.state["queries"].extend(normalized)
                self.state["failures"].extend(failures)
                self.state["invalid_items"] += len(failures)
            self.state["processed_items"] += len(chunk)
            if not self._post_batch(stage, batch_no, len(chunk)):
                return False
        self.state["docs"].sort(key=lambda item: item["doc_id"])
        self.state["queries"].sort(key=lambda item: item["query_id"])
        self.state["seen_doc_ids"] = sorted(self._seen_doc_ids)
        self.state["seen_query_ids"] = sorted(self._seen_query_ids)
        self._snapshot(stage)
        return True

    # ---------- 阶段二：索引 ----------
    def run_index(self):
        stage = "index"
        start_batch = self.stage_cursor.get(stage, 0)
        index = MultiLanguageIndex(self.config["languages"]["allowed"])
        for batch_no, chunk in self._chunks(self.state["docs"], self._batch_size()):
            if batch_no < start_batch:
                continue
            for doc in chunk:
                index.add_document(doc)
            self.state["processed_items"] += len(chunk)
            if not self._post_batch(stage, batch_no, len(chunk)):
                return False
        index.finalize()
        self.state["index_state"] = index.to_state()
        self._snapshot(stage)
        return True

    # ---------- 阶段三：并发检索 ----------
    def run_retrieval(self):
        stage = RETRIEVAL_STAGE
        start_batch = self.stage_cursor.get(stage, 0)
        done_ids = {item["query_id"] for item in self.state["retrieval_results"]}
        pending = [q for q in self.state["queries"] if q["query_id"] not in done_ids]
        multi_index = MultiLanguageIndex.from_state(self.state["index_state"])
        workers = self.config["batch"]["max_workers"]

        for batch_no, chunk in self._chunks(pending, self._batch_size()):
            global_batch = start_batch + batch_no
            batch_results = []
            batch_failures = []
            if workers > 1:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {
                        pool.submit(
                            self._score_one, query, multi_index
                        ): query for query in chunk
                    }
                    for future in futures:
                        query = futures[future]
                        try:
                            batch_results.append(future.result())
                        except Exception as exc:
                            batch_failures.append(make_failure(
                                query["query_id"], stage, E_ITEM_FAILED,
                                type(exc).__name__, str(exc),
                            ))
            else:
                for query in chunk:
                    result, failure = self._score_one_safe(query, multi_index)
                    if result is not None:
                        batch_results.append(result)
                    if failure is not None:
                        batch_failures.append(failure)
            # 无论线程如何调度，结果严格按查询 ID 字典序落盘
            batch_results.sort(key=lambda item: item["query_id"])
            batch_failures.sort(key=lambda item: item["item_id"])
            self.state["retrieval_results"].extend(batch_results)
            self.state["failures"].extend(batch_failures)
            self.state["retrieval_results"].sort(key=lambda item: item["query_id"])
            self.state["processed_items"] += len(chunk)
            if not self._post_batch(stage, global_batch, len(chunk)):
                return False
        return True

    def _score_one(self, query, multi_index):
        return score_query(
            query, multi_index, self.translation_index, self.config
        )

    def _score_one_safe(self, query, multi_index):
        try:
            return self._score_one(query, multi_index), None
        except Exception as exc:
            return None, make_failure(
                query["query_id"], RETRIEVAL_STAGE, E_ITEM_FAILED,
                type(exc).__name__, str(exc),
            )

    # ---------- 阶段四：评估 ----------
    def run_evaluation(self):
        stage = EVAL_STAGE
        start_batch = self.stage_cursor.get(stage, 0)
        qrels = load_qrels(self.config["paths"]["qrels"])
        results = self.state["retrieval_results"]
        for batch_no, chunk in self._chunks(results, self._batch_size()):
            if batch_no < start_batch:
                continue
            self.state["processed_items"] += len(chunk)
            if not self._post_batch(stage, batch_no, len(chunk)):
                return False
        metrics = evaluate(results, qrels)
        self.state["evaluation_metrics"] = metrics
        self._snapshot(stage)
        return True

    def _post_batch(self, stage, batch_no, item_count):
        """批次收尾：先提交批次快照，再登记退出请求并补快照，最后判定是否退出。"""
        self._finish_batch(stage, batch_no, item_count)
        self._register_exit_requests(stage)
        if self.exit_controller.exit_requested:
            # 注入标记必须随快照持久化，否则续跑会重复触发
            self._snapshot(stage)
        return not self._check_exit(stage, batch_no)

    def _finish_batch(self, stage, batch_no, item_count):
        self.state["stage_batches"][stage].append({
            "batch_no": batch_no,
            "item_count": item_count,
        })
        if self._should_commit(stage):
            self._snapshot(stage)

    # ---------- 总入口 ----------
    def run(self):
        existing = self.resume_if_possible()
        if existing is not None:
            self.state["reused_items"] = self._count_resumed_items()
        stage_runners = {
            NORM_STAGE: self.run_normalize,
            "index": self.run_index,
            RETRIEVAL_STAGE: self.run_retrieval,
            EVAL_STAGE: self.run_evaluation,
        }
        for stage in STAGES:
            already_done = len(self.state["stage_batches"][stage])
            if already_done > 0 and self._stage_complete(stage):
                continue
            if already_done > 0:
                # 归一化支持精确批次续跑；其余阶段为纯函数，整体重建更安全
                if stage != NORM_STAGE:
                    self._reset_stage(stage)
            ok = stage_runners[stage]()
            if not ok:
                return {"status": "interrupted", "reason": self.exit_controller.reason}
        report = self._write_report()
        return {"status": "complete", "report_path": report}

    def _stage_complete(self, stage):
        """只有“批次覆盖全部输入且阶段产物已最终化”才算完成，可安全跳过。"""
        if stage == NORM_STAGE:
            expected = len(self.inputs["raw_docs"]) + len(self.inputs["raw_queries"])
            seen = sum(b["item_count"] for b in self.state["stage_batches"][stage])
            return seen >= expected
        if stage == "index":
            expected = len(self.state["docs"])
            seen = sum(b["item_count"] for b in self.state["stage_batches"][stage])
            return (
                seen >= expected
                and self.state["index_state"] is not None
            )
        if stage == RETRIEVAL_STAGE:
            expected = len(self.state["queries"])
            seen = sum(b["item_count"] for b in self.state["stage_batches"][stage])
            return seen >= expected
        # evaluation
        expected = len(self.state["retrieval_results"])
        seen = sum(b["item_count"] for b in self.state["stage_batches"][stage])
        return (
            seen >= expected
            and self.state["evaluation_metrics"] is not None
        )

    def _reset_stage(self, stage):
        """中断后重跑某阶段前，清空该阶段的不完整产物，保证从批次 0 重建。"""
        self.stage_cursor[stage] = 0
        self.state["stage_batches"][stage] = []
        if stage == "index":
            self.state["index_state"] = None
        elif stage == RETRIEVAL_STAGE:
            self.state["retrieval_results"] = []
        elif stage == EVAL_STAGE:
            self.state["evaluation_metrics"] = None

    def _count_resumed_items(self):
        return sum(
            batch["item_count"]
            for batches in self.state["stage_batches"].values()
            for batch in batches
        )

    def _write_report(self):
        failures = sorted(
            self.state["failures"],
            key=lambda item: (item["stage"], item["item_id"], item["code"]),
        )
        report = {
            "status": "complete",
            "snapshot_version": SNAPSHOT_VERSION,
            "latest_snapshot_version": self.version,
            "config_hash": self.cfg_hash,
            "translation_hash": self.trans_hash,
            "metrics": self.state["evaluation_metrics"],
            "stage_batches": self.state["stage_batches"],
            "reused_batches": self.state["reused_batches"],
            "reused_items": self.state["reused_items"],
            "processed_items_this_run": self.state["processed_items"],
            "total_items_in_snapshots": self._count_resumed_items(),
            "invalid_items": self.state["invalid_items"],
            "failure_count": len(failures),
            "failures": failures,
            "retrieval_results": self.state["retrieval_results"],
        }
        path = self.config["paths"]["report_path"]
        atomic_write_json(path, report)
        return path
