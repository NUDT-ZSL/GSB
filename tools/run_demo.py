"""端到端演示：构造样例 -> 模拟锁/截断 -> 两批处理 -> 错误配置 -> 逐条核查需求 1~10。

运行：
    python -I tools/run_demo.py
不依赖第三方库；锁占用通过子进程运行 hold_lock.py 实现。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，统一切到 UTF-8 输出，避免中文报告乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# 子进程统一按 UTF-8 输出与解码
_CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

from handover.config import load_config  # noqa: E402
from handover.pipeline import (  # noqa: E402
    Pipeline,
    RECORDS_FILE_NAME,
    REPORT_JSON_NAME,
    REPORT_MD_NAME,
)
from handover.report import write_reports  # noqa: E402
from handover.errors import ConfigError  # noqa: E402
from tools import make_samples  # noqa: E402

WORK = ROOT / "demo_workspace"
RUN_AT = "2026-11-14T12:00:00+08:00"

results: list[tuple[str, bool, str]] = []


def check(no: str, title: str, ok: bool, detail: str = "") -> None:
    results.append((f"需求{no}", ok, detail))
    flag = "PASS" if ok else "FAIL"
    print(f"[{flag}] 需求{no} {title}" + (f" -- {detail}" if detail else ""))


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def run_pipeline(input_dir: Path, config_path: Path, output_dir: Path) -> dict:
    config = load_config(config_path)
    report = Pipeline(config, input_dir, output_dir, run_at=RUN_AT).run()
    write_reports(
        report,
        output_dir / REPORT_JSON_NAME,
        output_dir / REPORT_MD_NAME,
    )
    return report


def start_lock_holder(target: Path, seconds: int = 90) -> subprocess.Popen:
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [sys.executable, "-I", str(ROOT / "tools" / "hold_lock.py"),
         str(target), "--seconds", str(seconds)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
        text=True,
        encoding="utf-8",
        env=_CHILD_ENV,
    )
    # 等待锁实际生效
    import time
    for _ in range(50):
        try:
            with open(target, "r+b"):
                pass
            time.sleep(0.1)
        except OSError:
            break
    return proc


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    samples = WORK / "samples"
    make_samples.generate(samples)
    input_dir = samples / "input"
    config_path = samples / "config" / "mapping.json"

    out1 = WORK / "out_batch"
    out_bad1 = WORK / "out_bad_target"
    out_bad2 = WORK / "out_bad_syntax"

    # 锁定 source_a_locked.jsonl
    lock_proc = start_lock_holder(input_dir / "source_a_locked.jsonl")

    try:
        # ============================ 第一批（锁未释放、截断存在） ==============
        print("\n=== 第一批处理：locked + truncated 文件同时存在 ===")
        report1 = run_pipeline(input_dir, config_path, out1)
        records1 = load_jsonl(out1 / RECORDS_FILE_NAME)
        by_pk1 = {r["record_id"]: r for r in records1}

        # 需求 1：字段名统一、来源特有字段按配置丢弃/保留
        expected_fields = set(make_samples.CANONICAL_FIELDS)
        actual_fields = set()
        for r in records1:
            actual_fields.update(r.keys())
        check("1", "字段名统一且与 canonical_fields 一致，未映射字段被丢弃",
              actual_fields <= expected_fields and actual_fields,
              f"实际字段集合={sorted(actual_fields)}")
        # channel 只在来源 A 映射中出现：B 侧记录没有该键，A 侧保留
        check("1b", "来源特有字段按配置保留（A 侧 channel 保留，B 侧无该键）",
              "channel" in by_pk1.get("A002", {}) and "channel" not in by_pk1.get("B002", {}))

        # 需求 2：日期统一格式，带/不带时区归一
        b002 = by_pk1.get("B002", {})
        b003 = by_pk1.get("B003", {})
        check("2", "带时区(UTC)与无时区日期均转为统一规范格式并换算到默认时区",
              b002.get("biz_time") == "2026-11-14T09:30:00+08:00"
              and b003.get("biz_time") == "2026-11-14T11:00:00+08:00",
              f"B002={b002.get('biz_time')}, B003={b003.get('biz_time')}")

        # 需求 3：重复主键合并，取业务时间最新
        pks = [r["record_id"] for r in records1]
        check("3", "重复主键合并为一条且取最新业务时间记录",
              len(pks) == len(set(pks))
              and "A001" in by_pk1
              and by_pk1["A001"]["amount"] == 1280.0
              and by_pk1["A001"]["biz_time"] == "2026-11-14T10:05:00+08:00",
              f"主键列表={pks}")

        # 需求 4：缺必填拒绝并逐条列出主键与缺失项
        rejects = [rj for g in report1["file_groups"] for rj in g["rejections"]]
        reject_map = {rj["pk"]: rj["missing_fields"] for rj in rejects}
        check("4", "缺必填记录不进规范集且报告逐条列出主键与缺失项",
              "A003" in reject_map and reject_map["A003"] == ["amount"]
              and "A003" not in by_pk1
              and any("record_id" in m for m in reject_map.values()),
              f"拒绝明细={reject_map}")

        # 需求 5：锁/截断不影响其他文件，报告列出两个未完成文件
        unfinished = report1["conclusion"]["unfinished"]
        other_ok = report1["summary"]["files_complete"] == 3
        check("5", "锁定与截断文件被标记未完成，其他文件记录不受影响",
              "source_a_locked.jsonl" in unfinished
              and "source_b_truncated.jsonl" in unfinished
              and other_ok,
              f"未完成={unfinished}")
        truncated_group = next(
            g for g in report1["file_groups"] if g["file"] == "source_b_truncated.jsonl"
        )
        check("5b", "截断文件报告包含异常行号与半截内容预览",
              truncated_group["bad_line_no"] == 3
              and "Half Line" in truncated_group["bad_line_preview"])

        # 需求 8：分组顺序与主键排序固定、两类结论正确
        group_names = [g["file"] for g in report1["file_groups"]]
        check("8", "报告按文件名固定分组、组内主键排序、两类结论齐全",
              group_names == sorted(group_names)
              and all(g["accepted_pks"] == sorted(g["accepted_pks"])
                      for g in report1["file_groups"])
              and set(report1["conclusion"]) == {"complete_usable", "unfinished"},
              f"分组顺序={group_names}")

        # 需求 7：来源优先级裁决并记录字段与主键
        conflicts = report1["events"]["field_conflicts"]
        a001_conflicts = [e for e in conflicts if e["pk"] == "A001"]
        conflict_fields = {e["field"] for e in a001_conflicts}
        check("7", "同主键跨来源字段冲突按优先级裁决且报告记录主键与字段",
              {"customer_name", "amount", "remark"} <= conflict_fields
              and all(e["resolved_to"]["source"] == "source_a" for e in a001_conflicts)
              and by_pk1["A001"]["amount"] == 1280.0,
              f"冲突字段={sorted(conflict_fields)}")
        merges = report1["events"]["duplicate_merges"]
        check("7b", "重复合并事件记录胜出方与淘汰方",
              any(e["pk"] == "A001" and len(e["losers"]) == 2 for e in merges))

        # ============================ 第二批：解锁 + 修复截断 ==================
        print("\n=== 第二批处理：解除锁定并补全截断文件后重跑同一批次 ===")
    finally:
        lock_proc.terminate()
        lock_proc.wait(timeout=15)

    # 修复截断文件：写回完整两行
    good_rows = make_samples.SOURCE_B_TRUNCATED_GOOD
    (input_dir / "source_b_truncated.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in good_rows),
        encoding="utf-8",
    )

    report2 = run_pipeline(input_dir, config_path, out1)
    records2 = load_jsonl(out1 / RECORDS_FILE_NAME)
    pks2 = [r["record_id"] for r in records2]

    # 需求 6：补齐、不重复、只增加原缺失部分（同一输出目录续跑）
    added = sorted(set(pks2) - set(pks))
    check("6", "解锁重跑后无重复主键，记录数只增加原缺失部分",
          len(pks2) == len(set(pks2))
          and added == ["L001", "L002", "T001", "T002"]
          and {r["record_id"] for r in records1} <= set(pks2),
          f"新增主键={added}，第一批 {len(records1)} 条 -> 第二批 {len(records2)} 条")
    check("6b", "第二批报告中所有文件均为完整可用",
          report2["conclusion"]["unfinished"] == []
          and report2["summary"]["files_complete"] == 5)
    # 同目录再跑第三次：幂等，记录数不变
    report3 = run_pipeline(input_dir, config_path, out1)
    records3 = load_jsonl(out1 / RECORDS_FILE_NAME)
    check("6c", "同目录重复运行幂等，已成功记录不重复写入",
          len(records3) == len(records2)
          and report3["summary"]["duplicate_merge_events"]
          == report2["summary"]["duplicate_merge_events"])

    # 需求 9：错误配置整体拒绝、无半成品、错误信息含键名
    print("\n=== 错误配置拒绝执行验证 ===")
    target_error = None
    try:
        load_config(samples / "config" / "bad_mapping_target.json")
    except ConfigError as exc:
        target_error = str(exc)
    check("9", "映射目标不存在时拒绝并指出所在键名",
          target_error is not None and "mappings.cust_name" in target_error
          and "customer_name_typo" in target_error,
          detail=target_error or "未抛出 ConfigError")

    syntax_error = None
    try:
        load_config(samples / "config" / "bad_mapping_syntax.json")
    except ConfigError as exc:
        syntax_error = str(exc)
    check("9b", "JSON 语法错误时拒绝并指出行列位置",
          syntax_error is not None and "行" in syntax_error and "列" in syntax_error,
          detail=syntax_error or "未抛出 ConfigError")

    # 通过 CLI 真实执行错误配置，验证输出目录无任何规范记录文件
    cli_env_out = WORK / "out_cli_error"
    proc = subprocess.run(
        [sys.executable, "-I", str(ROOT / "run_handover.py"),
         "--input-dir", str(input_dir),
         "--config", str(samples / "config" / "bad_mapping_target.json"),
         "--output-dir", str(cli_env_out)],
        capture_output=True, text=True, encoding="utf-8", env=_CHILD_ENV,
    )
    no_artifact = (not cli_env_out.exists()) or not any(
        (cli_env_out).glob("canonical_records.jsonl")
    )
    check("9c", "CLI 运行错误配置退出码为 2 且不产生规范记录文件",
          proc.returncode == 2 and no_artifact,
          detail=f"exit={proc.returncode}, stderr={(proc.stderr or '').strip()[:120]}")

    # 需求 10：Windows 可执行命令入口，产物落到指定路径
    cli_out = WORK / "out_cli"
    proc = subprocess.run(
        [sys.executable, "-I", str(ROOT / "run_handover.py"),
         "--input-dir", str(input_dir),
         "--config", str(config_path),
         "--output-dir", str(cli_out)],
        capture_output=True, text=True, encoding="utf-8", env=_CHILD_ENV,
    )
    artifacts = [RECORDS_FILE_NAME, REPORT_JSON_NAME, REPORT_MD_NAME]
    check("10", "命令行入口生成规范记录与交接报告到指定路径",
          proc.returncode == 0
          and all((cli_out / name).is_file() for name in artifacts),
          detail=f"exit={proc.returncode}; 产物={artifacts}")

    # Markdown 报告可人工阅读且含两类结论标题；此时全部文件完整可用
    md = (cli_out / REPORT_MD_NAME).read_text(encoding="utf-8")
    check("10b", "Markdown 交接报告含两类结论与文件明细",
          "结论一：完整可用" in md and "结论二：未完成" in md
          and "source_a_locked.jsonl" in md
          and "（无）" in md.split("## 结论二：未完成（需要跟进）", 1)[1].split("##", 1)[0]
          )

    # 汇总
    print("\n================ 核查汇总 ================")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")
    print(f"\n通过 {passed}/{len(results)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
