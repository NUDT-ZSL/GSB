"""采集引擎：三源编排、限速并发、重试熔断、断点续跑、中断与报告。

确定性提交顺序 rest -> ts -> csv；同一源可并发请求，但严格按游标顺序提交。
中断双通道：SIGINT/SIGBREAK 信号 或 控制文件出现 stop，二者均先完成
当前写事务再落 aborted 报告。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .adapters import CsvAdapter, RestAdapter, TimestampAdapter
from .applog import RunLogger
from .config import Config
from .errors import (
    CircuitOpenError, CsvPartialError, ReconError, StoppedError,
)
from .ratelimit import (
    ConcurrencyGate, IntervalLimiter, SlidingWindowRateLimiter, _WaitStats,
    _interruptible_sleep,
)
from .report import (
    build_report, find_latest_aborted, new_source_report, validate_report,
    write_report,
)
from .retry import CircuitBreaker, RetryExecutor
from .storage import Storage

EMPTY_WINDOW_LIMIT = 6  # 连续空窗口数达到后结束时间戳源


class Engine:
    def __init__(self, config: Config, failure_hook=None):
        self.cfg = config
        self.failure_hook = failure_hook  # 测试钩子：(source, batch_seq) -> None|raise
        self.run_id = "run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") \
                      + "-" + uuid.uuid4().hex[:8]
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.shutdown = threading.Event()
        self.stats = _WaitStats()
        self._state_lock = threading.Lock()
        self.storage: Storage | None = None
        self.logger: RunLogger | None = None
        self.session = None
        self.resumed_from_run_id: str | None = None
        self.resume_count = 0
        self.resume_detail: dict | None = None
        self.aborted_reason: str | None = None
        self._first_cursor_after_resume: dict[str, object] = {}
        self._control_mtime: float | None = None

    # ------------------------------------------------------------ lifecycle
    def _install_signal_handlers(self) -> None:
        import signal

        def handler(signum, frame):
            self.request_stop(f"signal:{signum}")

        if hasattr(signal, "SIGBREAK"):  # Windows CTRL_BREAK
            signal.signal(signal.SIGBREAK, handler)  # type: ignore[attr-defined]
        signal.signal(signal.SIGINT, handler)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, handler)

    def request_stop(self, reason: str) -> None:
        if not self.shutdown.is_set():
            self.aborted_reason = reason
            self.shutdown.set()

    def _watch_control_file(self) -> None:
        """轮询控制文件；主循环武装且过武装门后，出现 stop 即请求受控中断。"""
        path = self.cfg.control_file
        # 等待主循环开始，避免启动前残留的控制文件在初始化阶段误触发
        while not self.shutdown.is_set() and not getattr(self, "_armed", False):
            if self.shutdown.wait(0.02):
                return
        while not self.shutdown.is_set():
            # 配置武装门时，中断由主线程 _evaluate_gate_stop 确定性触发；
            # 监控线程仅在无门控（普通中断）时直接响应控制文件
            if getattr(self, "_arm_after_batches", None) is None:
                try:
                    if os.path.exists(path):
                        content = open(path, "r", encoding="utf-8").read().strip()
                        if content == "stop":
                            self.request_stop("control_file")
                            return
                except OSError:
                    pass
            if self.shutdown.wait(0.02):
                return

    def _committed_count(self, source: str) -> int:
        if self.storage is None:
            return 0
        progress = self.storage.get_progress(source)
        return progress["committed_batches"] if progress else 0

    def _evaluate_gate_stop(self) -> None:
        """主线程确定性门控：达到武装门且控制文件请求 stop 时才中断。

        在每批写事务提交后立即执行，保证中断点精确落在指定批次之后，
        不受监控线程 20ms 轮询时序影响。
        """
        gate = getattr(self, "_arm_after_batches", None)
        if gate is None:
            return
        source, need = gate
        if self._committed_count(source) < need:
            return
        try:
            if os.path.exists(self.cfg.control_file) and \
                    open(self.cfg.control_file, "r", encoding="utf-8").read().strip() == "stop":
                self.request_stop("control_file_gate")
        except OSError:
            pass

    def _check_stop(self) -> None:
        if self.shutdown.is_set():
            raise StoppedError(self.aborted_reason or "stop requested")

    def _write_state(self, source_reports: dict) -> None:
        payload = {
            "run_id": self.run_id,
            "ts": time.time(),
            "stats": self.stats.snapshot(),
            "sources": source_reports,
        }
        tmp = self.cfg.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.cfg.state_path)

    # ------------------------------------------------------------ resume
    def _resolve_resume(self) -> None:
        aborted = find_latest_aborted(self.cfg.report_dir)
        if aborted and aborted.get("run_status") == "aborted":
            self.resumed_from_run_id = aborted["run_id"]
            self.resume_count = 1
            self.resume_detail = {"from": "aborted_report",
                                  "aborted_started_at": aborted.get("started_at")}
            return
        # 强杀场景：没有 aborted 报告，依据 SQLite 持久化 run_id/进度恢复
        assert self.storage is not None
        last = self.storage.last_aborted_run()
        if last:
            self.resumed_from_run_id = last["run_id"]
            self.resume_count = 1
            self.resume_detail = {"from": "sqlite_run_meta",
                                  "aborted_started_at": last.get("started_at")}

    # ------------------------------------------------------------ generic
    def _sleep_interruptible(self, seconds: float) -> None:
        _interruptible_sleep(seconds, self.shutdown, self.stats)

    def _make_retry_executor(self, source: str, src_report: dict) -> RetryExecutor:
        def on_retry(attempt: int, code: str, delay: float) -> None:
            src_report["retry_count"] += 1
            if self.logger:
                self.logger.fetch_retry(source, attempt, code)

        return RetryExecutor(
            self.cfg.max_retries, self.cfg.backoff_base_ms,
            self._sleep_interruptible,
        )

    def _load_cursor(self, source: str, adapter, src_report: dict):
        assert self.storage is not None
        progress = self.storage.get_progress(source)
        if progress:
            cursor = json.loads(progress["cursor_value"])
            # 已提交 N 批（序号 0..N-1），下一批序号为 N
            next_seq = progress["committed_batches"]
            src_report["committed_batches"] = next_seq
            self._first_cursor_after_resume[source] = cursor
            return cursor, next_seq
        return adapter.initial_cursor(), 0

    # ------------------------------------------------------------ REST
    def run_rest(self, adapter: RestAdapter, src_report: dict) -> None:
        breaker = CircuitBreaker()
        retry = self._make_retry_executor("rest", src_report)
        cursor, batch_seq = self._load_cursor("rest", adapter, src_report)
        limiter = SlidingWindowRateLimiter(self.cfg.qps, self.shutdown, self.stats)
        gate = ConcurrencyGate(self.cfg.concurrency, self.shutdown, self.stats)
        pool = ThreadPoolExecutor(max_workers=self.cfg.concurrency)
        total_pages: int | None = None
        try:
            while True:
                self._check_stop()
                # 已知总页数后，若游标已越过末页则结束（避免恢复时多发空批次）
                if total_pages is not None and cursor > total_pages:
                    break
                # 流水线预取：最多 concurrency 个在途，但不得越过服务端总页数
                window = self.cfg.concurrency
                if total_pages is not None:
                    window = min(window, total_pages - cursor + 1)
                if window <= 0:
                    break
                futures: list = []
                page_cursors: list[int] = []
                for _ in range(window):
                    page_cursors.append(cursor)
                    fut = pool.submit(self._guarded_rest_fetch,
                                      adapter, cursor, limiter, gate,
                                      breaker, retry, src_report)
                    futures.append(fut)
                    cursor += 1
                last_result = None
                for cur_cursor, fut in zip(page_cursors, futures):
                    self._check_stop()  # 每批提交前检查中断，保证停在当前已提交点
                    result = fut.result()  # 异常在此抛出，按源级失败处理
                    self.stats.completed_requests += 1
                    last_result = result
                    if total_pages is None:
                        total_pages = result.extra["total_pages"]
                    # 越界空页（页码超过总页数）不构成批次，直接收敛结束
                    if cur_cursor > total_pages:
                        break
                    # 提交（含测试钩子）失败：本批已整体回滚，异常向外传播
                    # 终止本源，不会提交已预取的后续批次，杜绝半写/越界提交
                    self._commit_rest_batch(result, cur_cursor, batch_seq, src_report)
                    batch_seq += 1
                    # 确定性门控：主线程在每批提交后立即判定，不依赖轮询时序
                    self._evaluate_gate_stop()
                    if self.shutdown.is_set():
                        # 已完成当前写事务，下一轮 _check_stop 会落 aborted 报告
                        break
                if self.shutdown.is_set():
                    continue
                if last_result is not None and last_result.exhausted:
                    break
        finally:
            pool.shutdown(wait=True)
        src_report["status"] = "success"
        src_report["cursor_type"] = "page"

    def _guarded_rest_fetch(self, adapter, page, limiter, gate, breaker,
                            retry, src_report):
        def work():
            gate.acquire()
            try:
                limiter.acquire()
                return adapter.fetch(page, self.cfg.page_size)
            finally:
                gate.release()

        result, _, _ = retry.call(
            work, breaker,
            on_retry=lambda a, c, d: self._on_retry("rest", src_report, a, c),
        )
        return result

    def _on_retry(self, source: str, src_report, attempt: int, code: str) -> None:
        if src_report is not None:
            src_report["retry_count"] += 1
        if self.logger:
            self.logger.fetch_retry(source, attempt, code)

    def _commit_rest_batch(self, result, cursor_from, batch_seq, src_report) -> None:
        records, dupes = self._dedup(result.records)
        src_report["fetched"] += len(result.records)
        src_report["duplicate_dropped"] += len(dupes)
        self._commit_batch(
            "rest", batch_seq, "page", cursor_from, result.next_cursor,
            records, dupes, src_report,
        )
        src_report["cursor"] = result.next_cursor
        if self.logger:
            self.logger.batch_committed("rest", batch_seq, len(records))
        self._write_state({"rest": src_report})

    # ------------------------------------------------------------ TS
    def run_ts(self, adapter: TimestampAdapter, src_report: dict) -> None:
        breaker = CircuitBreaker()
        retry = self._make_retry_executor("ts", src_report)
        cursor, batch_seq = self._load_cursor("ts", adapter, src_report)
        interval = IntervalLimiter(self.cfg.ts_min_interval_ms,
                                   self.shutdown, self.stats)
        empty_windows = 0
        max_seen_ts: int | None = None  # 全局已见最大毫秒时间戳
        # 用连续空窗口收敛，结束后把游标精确对齐到 max_ts+1
        while True:
            self._check_stop()
            interval.acquire()

            def work():
                return adapter.fetch(cursor, self.cfg.page_size)

            result, _, _ = retry.call(
                work, breaker,
                on_retry=lambda a, c, d: self._on_retry("ts", src_report, a, c),
            )
            self.stats.completed_requests += 1
            records, dupes = self._dedup(result.records)
            src_report["fetched"] += len(result.records)
            src_report["duplicate_dropped"] += len(dupes)
            if result.extra.get("max_seen_ts") is not None:
                max_seen_ts = max(max_seen_ts, result.extra["max_seen_ts"]) \
                    if max_seen_ts is not None else result.extra["max_seen_ts"]
            self._commit_batch(
                "ts", batch_seq, "ts", cursor, result.next_cursor, records,
                dupes, src_report,
            )
            batch_seq += 1
            src_report["cursor"] = result.next_cursor
            if self.logger:
                self.logger.batch_committed("ts", batch_seq, len(records))
            self._write_state({"ts": src_report})
            if result.extra.get("empty"):
                empty_windows += 1
            else:
                empty_windows = 0
            if empty_windows >= EMPTY_WINDOW_LIMIT:
                break
            cursor = result.next_cursor

        # 精确收敛最终游标：下一区间起点 = 最大毫秒 + 1（不回退、不过冲）
        if max_seen_ts is not None:
            final_cursor = {"start": max_seen_ts + 1,
                            "end": max_seen_ts + 1 + adapter.window_ms}
            src_report["cursor"] = final_cursor
            assert self.storage is not None
            self.storage.conn.execute(
                "UPDATE source_progress SET cursor_value=? WHERE source='ts'",
                (json.dumps(final_cursor, ensure_ascii=False),),
            )
        src_report["status"] = "success"
        src_report["cursor_type"] = "ts"

    # ------------------------------------------------------------ CSV
    def run_csv(self, adapter: CsvAdapter, src_report: dict) -> None:
        breaker = CircuitBreaker()
        retry = self._make_retry_executor("csv", src_report)
        cursor, batch_seq = self._load_cursor("csv", adapter, src_report)
        while True:
            self._check_stop()
            result, _, _ = retry.call(
                lambda: adapter.fetch(cursor, self.cfg.page_size), breaker,
                on_retry=lambda a, c, d: self._on_retry("csv", src_report, a, c),
            )
            if result.should_retry:
                # CsvPartialError 路径由 RetryExecutor 处理；此处兜底
                raise CsvPartialError(result.error_code or "E_CSV_PARTIAL")
            records, dupes = self._dedup(result.records)
            src_report["fetched"] += len(result.records)
            src_report["duplicate_dropped"] += len(dupes)
            self._commit_batch(
                "csv", batch_seq, "offset", cursor, result.next_cursor,
                records, dupes, src_report,
            )
            batch_seq += 1
            cursor = result.next_cursor
            src_report["cursor"] = result.next_cursor
            if self.logger:
                self.logger.batch_committed("csv", batch_seq, len(records))
            self._write_state({"csv": src_report})
            if result.exhausted:
                break
        src_report["status"] = "success"
        src_report["cursor_type"] = "offset"

    # ------------------------------------------------------------ shared tx
    def _dedup(self, records: list[dict]) -> tuple[list[dict], set[str]]:
        """源内自然键去重；是否已入库由存储层主键判定。"""
        seen: set[str] = set()
        unique: list[dict] = []
        dupes: set[str] = set()
        for record in records:
            serial = record["serial_no"]
            if serial in seen:
                dupes.add(serial)
                continue
            seen.add(serial)
            unique.append(record)
        return unique, dupes

    def _commit_batch(self, source, batch_seq, cursor_type, cursor_from,
                      cursor_to, records, dupes, src_report=None) -> dict:
        assert self.storage is not None
        hook = None
        if self.failure_hook is not None:
            def hook():
                self.failure_hook(source, batch_seq)
        outcome = self.storage.commit_batch(
            run_id=self.run_id, source=source, batch_seq=batch_seq,
            cursor_type=cursor_type, cursor_from=cursor_from, cursor_to=cursor_to,
            records=records, duplicate_serials=dupes, failure_hook=hook,
        )
        if src_report is not None:
            # 跨批次/跨页/边界重复（存储层按同来源主键识别）
            src_report["duplicate_dropped"] += len(outcome["already_present"])
            # batch_seq 从 0 起，已提交批次数 = 最后序号 + 1
            src_report["committed_batches"] = batch_seq + 1
        if self.logger:
            self.logger.fetch_ok(source, batch_seq, len(records))
        return outcome
    # ------------------------------------------------------------ top level
    def run(self) -> dict:
        import requests as requests_lib

        os.makedirs(self.cfg.data_dir, exist_ok=True)
        self._install_signal_handlers()
        watcher = threading.Thread(target=self._watch_control_file, daemon=True)
        watcher.start()
        self.storage = Storage(self.cfg.db_path)
        self.logger = RunLogger(self.cfg.log_dir, self.run_id)
        self.session = requests_lib.Session()
        self._resolve_resume()
        self.storage.start_run(self.run_id, self.resumed_from_run_id)
        if self.logger:
            self.logger.run_event("run_start")

        adapters = {
            "rest": RestAdapter(self.cfg.rest_base_url, self.session),
            "ts": TimestampAdapter(self.cfg.ts_base_url),
            "csv": CsvAdapter(self.cfg.csv_path),
        }
        source_reports = {
            "rest": new_source_report("page"),
            "ts": new_source_report("ts"),
            "csv": new_source_report("offset"),
        }
        run_status = "success"
        # 可选武装门：rest 提交满 N 批后控制文件中断才生效（确定性中断时序）
        arm_after = self.cfg.extra.get("arm_stop_after_rest_batches")
        if arm_after is not None:
            self._arm_after_batches = ("rest", int(arm_after))
        self._armed = True  # 主循环开始：控制文件中断从此刻受门控生效

        for source in ("rest", "ts", "csv"):
            # 源边界检查点：保证"REST 已提交、TS 尚未开始"的确定性中断
            if self.shutdown.is_set():
                raise StoppedError(self.aborted_reason or "stop requested")
            self._check_stop()
            if self.logger:
                self.logger.source_start(source)
            try:
                if source == "rest":
                    self.run_rest(adapters["rest"], source_reports["rest"])
                elif source == "ts":
                    self.run_ts(adapters["ts"], source_reports["ts"])
                else:
                    self.run_csv(adapters["csv"], source_reports["csv"])
            except StoppedError as exc:
                source_reports[source]["status"] = "aborted"
                source_reports[source]["error_code"] = exc.code
                self._finalize_aborted(source_reports)
                return self._final_report(source_reports, "aborted")
            except CircuitOpenError as exc:
                src = source_reports[source]
                src["status"] = "failed"
                src["error_code"] = exc.code
                src["circuit_open"] = True
                if self.logger:
                    self.logger.error(source, exc.code)
                run_status = "partial"
            except ReconError as exc:
                src = source_reports[source]
                src["status"] = "failed"
                src["error_code"] = exc.code
                if self.logger:
                    self.logger.error(source, exc.code)
                run_status = "partial"
            except Exception as exc:  # 未知异常：标记失败、释放资源后向上抛出
                src = source_reports[source]
                src["status"] = "failed"
                src["error_code"] = "E_INTERNAL"
                if self.logger:
                    self.logger.error(source, "E_INTERNAL")
                # 未知错误（含测试钩子注入的事务失败）不作为 partial 吞掉，
                # 保证调用进程以非零退出码失败
                self._cleanup()
                raise

        # 全部源均未成功才算 failed；有失败有成功为 partial
        if run_status == "success":
            pass
        elif all(s["status"] != "success" for s in source_reports.values()):
            run_status = "failed"
        else:
            run_status = "partial"
        return self._final_report(source_reports, run_status)

    def _finalize_aborted(self, source_reports: dict) -> None:
        assert self.storage is not None and self.logger is not None
        self.storage.mark_aborted(self.run_id)
        report = build_report(
            self.run_id, "aborted", self.started_at, source_reports,
            self.resumed_from_run_id, self.resume_count, self.resume_detail,
        )
        write_report(self.cfg.report_dir, report, aborted=True)
        self.logger.run_event("run_aborted")
        self._write_state(source_reports)

    def _final_report(self, source_reports: dict, run_status: str) -> dict:
        assert self.storage is not None and self.logger is not None
        # 恢复后首个处理游标（任务 10 报告字段）
        if self.resume_detail:
            self.resume_detail["first_cursors"] = dict(self._first_cursor_after_resume)
        report = build_report(
            self.run_id, run_status, self.started_at, source_reports,
            self.resumed_from_run_id, self.resume_count, self.resume_detail,
        )
        errors = validate_report(report)
        if errors:
            raise AssertionError("报告未通过 Schema 校验: %s" % errors)
        write_report(self.cfg.report_dir, report, aborted=False)
        self.storage.finish_run(self.run_id, run_status)
        self.logger.run_event(f"run_{run_status}")
        self._write_state(source_reports)
        self._cleanup()
        return report

    def _cleanup(self) -> None:
        try:
            if self.session is not None:
                self.session.close()
        finally:
            if self.storage is not None:
                self.storage.close()
                self.storage = None
            if self.logger is not None:
                self.logger.close()

    def read_state(self) -> dict:
        """本地状态查询接口：运行统计（完成请求数/等待毫秒/在途数）。"""
        with open(self.cfg.state_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
