"""有界并发语料执行器。

- 每个用例在独立子进程中真实执行，失控用例由执行器 kill（非线程超时冒充）；
- 崩溃分类：length_oob / checksum_fail / parse_timeout / unrecoverable_abort；
- 检查点持久化：每完成一条用例即写 checkpoint.json（含语料版本指纹）；
- 恢复语义：指纹一致 -> 断点续跑；指纹变化 -> 整轮重启，旧偏移一律作废；
- 不可恢复终止：记录分类、写检查点（含未执行清单），不产出完成快照；
- 完成快照 snapshot.json 整体原子替换，内容确定性（无时间戳/PID），可哈希复算。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from . import codec, corpus as corpus_mod

CAT_OK = "ok"
CAT_LENGTH_OOB = "length_oob"
CAT_CHECKSUM_FAIL = "checksum_fail"
CAT_PARSE_TIMEOUT = "parse_timeout"
CAT_UNRECOVERABLE = "unrecoverable_abort"
CAT_BUSINESS_ERROR = "business_error"

CRASH_CATS = (CAT_LENGTH_OOB, CAT_PARSE_TIMEOUT, CAT_UNRECOVERABLE)

PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STATUS_COMPLETED = "completed"
STATUS_ABORTED = "aborted"
STATUS_INTERRUPTED = "interrupted"


def _classify(exitcode, timed_out, stdout, stderr):
    """把子进程结局映射为业务分类 + 业务码（崩溃时 code 为 None）。"""
    if timed_out:
        return CAT_PARSE_TIMEOUT, None
    if exitcode == 134:
        return CAT_UNRECOVERABLE, None
    if exitcode != 0:
        if "IndexError" in (stderr or ""):
            return CAT_LENGTH_OOB, None
        return CAT_UNRECOVERABLE, None
    try:
        code = int((stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return CAT_UNRECOVERABLE, None
    if code == codec.OK:
        return CAT_OK, code
    if code == codec.ERR_CHECKSUM:
        return CAT_CHECKSUM_FAIL, code
    return CAT_BUSINESS_ERROR, code


def _write_json_atomic(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def run_corpus(cases, defect, workdir, concurrency=4, case_timeout=2.0, max_cases=None):
    """执行语料，返回运行结论字典。

    max_cases: 完成 N 条后停止调度（用于模拟中断，产出检查点但无快照）。
    """
    os.makedirs(workdir, exist_ok=True)
    frames_dir = os.path.join(workdir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    ckpt_path = os.path.join(workdir, "checkpoint.json")
    snap_path = os.path.join(workdir, "snapshot.json")

    fp = corpus_mod.fingerprint(cases)
    done: dict = {}
    events: list = []

    # ---- 恢复判定：指纹一致才允许断点续跑，否则整轮重启 ----
    if os.path.exists(ckpt_path):
        with open(ckpt_path, "r", encoding="utf-8") as f:
            ckpt = json.load(f)
        if ckpt.get("fingerprint") == fp:
            done = dict(ckpt.get("done", {}))
            events = list(ckpt.get("events", []))
            events.append({
                "event": "checkpoint_resume",
                "done": len(done),
                "pending": len(cases) - len(done),
            })
        else:
            events.append({
                "event": "fingerprint_mismatch_restart",
                "old_fingerprint": ckpt.get("fingerprint"),
                "new_fingerprint": fp,
                "discarded_done": len(ckpt.get("done", {})),
            })
            done = {}

    def save_checkpoint(pending_ids):
        _write_json_atomic(ckpt_path, {
            "fingerprint": fp,
            "done": done,
            "events": events,
            "pending": sorted(pending_ids),
        })

    pending = [c for c in cases if c["id"] not in done]
    running: dict = {}  # id -> (Popen, start_ts, case)
    all_procs: list = []
    executed_this_run = 0
    max_concurrent = 0
    abort_detected = False

    def finish(case, proc, timed_out):
        nonlocal executed_this_run
        out, err = proc.communicate()
        category, code = _classify(proc.returncode, timed_out, out, err)
        done[case["id"]] = {
            "id": case["id"],
            "class": case["class"],
            "category": category,
            "code": code,
        }
        executed_this_run += 1
        return category

    while pending or running:
        # 调度：严格不超过并发上限；达到 max_cases 限量后停止调度新用例
        while (pending and not abort_detected
               and len(running) < concurrency
               and (max_cases is None or executed_this_run + len(running) < max_cases)):
            case = pending.pop(0)
            frame_path = os.path.join(frames_dir, case["id"] + ".bin")
            with open(frame_path, "wb") as f:
                f.write(bytes.fromhex(case["data_hex"]))
            proc = subprocess.Popen(
                [sys.executable, "-m", "framefuzz.sut",
                 "--defect", defect, frame_path],
                cwd=PACKAGE_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            all_procs.append(proc)
            running[case["id"]] = (proc, time.monotonic(), case)
            max_concurrent = max(max_concurrent, len(running))

        if not running:
            break
        time.sleep(0.02)

        for cid, (proc, start, case) in list(running.items()):
            if proc.poll() is not None:
                category = finish(case, proc, timed_out=False)
                del running[cid]
                if category == CAT_UNRECOVERABLE:
                    abort_detected = True
            elif time.monotonic() - start > case_timeout:
                proc.kill()  # 真实终止独立子进程
                finish(case, proc, timed_out=True)
                del running[cid]

        if abort_detected:
            # 不可恢复终止：强杀在途子进程，落检查点（含未执行清单），不出完成报告
            for cid, (proc, start, case) in list(running.items()):
                proc.kill()
                proc.communicate()
            unexecuted = sorted(list(running.keys()) + [c["id"] for c in pending])
            running.clear()
            events.append({"event": "unrecoverable_abort", "unexecuted": unexecuted})
            save_checkpoint(unexecuted)
            return _result(STATUS_ABORTED, fp, done, events, unexecuted,
                           all_procs, max_concurrent, executed_this_run)

    if pending:
        # 模拟中断：已达限量，剩余用例写入未执行清单，无完成快照
        unexecuted = sorted(c["id"] for c in pending)
        events.append({"event": "interrupted_limit", "unexecuted": unexecuted})
        save_checkpoint(unexecuted)
        return _result(STATUS_INTERRUPTED, fp, done, events, unexecuted,
                       all_procs, max_concurrent, executed_this_run)

    # ---- 正常完成：快照整体原子替换，携带语料版本指纹 ----
    save_checkpoint([])
    snapshot = {
        "fingerprint": fp,
        "defect": defect,
        "results": sorted(done.values(), key=lambda r: r["id"]),
        "crash_counts": _crash_counts(done.values()),
        "category_counts": _category_counts(done.values()),
        "events": events,
    }
    _write_json_atomic(snap_path, snapshot)
    return _result(STATUS_COMPLETED, fp, done, events, [],
                   all_procs, max_concurrent, executed_this_run)


def _crash_counts(results):
    counts: dict = {}
    for r in results:
        if r["category"] in CRASH_CATS:
            counts[r["category"]] = counts.get(r["category"], 0) + 1
    return counts


def _category_counts(results):
    counts: dict = {}
    for r in results:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    return counts


def _result(status, fp, done, events, unexecuted, all_procs,
            max_concurrent, executed_this_run):
    results = sorted(done.values(), key=lambda r: r["id"])
    return {
        "status": status,
        "fingerprint": fp,
        "results": results,
        "crash_counts": _crash_counts(results),
        "category_counts": _category_counts(results),
        "unexecuted": sorted(unexecuted),
        "recovery_events": events,
        "leftover_processes": [p.pid for p in all_procs if p.poll() is None],
        "max_concurrent_observed": max_concurrent,
        "executed_this_run": executed_this_run,
    }
