# -*- coding: utf-8 -*-
"""项目 67 验收检查套件：覆盖任务书 14 项要求，全部基于本地模拟服务，不访问公网。

运行（需先装好 Playwright 浏览器或系统 Chrome/Edge）：
  python tests/run_checks.py

实现方式：
- 线程内启动 mock_server（随机本地端口），通过 /control/* 端点确定性注入故障
- 以子进程方式运行 employee_importer.cli，模拟真实命令行使用
- 每项检查独立断言，最终汇总打印检查结果
"""

from __future__ import annotations

import csv
import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mock_server.server import build_server  # noqa: E402

GROUPS = ("成功", "失败", "不可用")
WORKDIR = Path(os.environ.get("PROJECT67_TEST_WORKDIR",
                              str(ROOT / "tests" / "_work")))
SAMPLE_CSV = ROOT / "samples" / "data" / "employees.csv"
EXPECTED_SUMMARY = ROOT / "samples" / "expected" / "expected_summary.json"
PYTHON = sys.executable


# ---------------------------------------------------------------- 基础工具
class CheckError(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise CheckError(message)


class MockServer:
    def __init__(self) -> None:
        self.server = build_server("127.0.0.1", 0)
        self.host, self.port = self.server.server_address[:2]
        self.base_url = f"http://127.0.0.1:{self.port}"
        import threading
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "MockServer":
        self.thread.start()
        self.wait_ready()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def wait_ready(self, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f"{self.base_url}/healthz", timeout=2) as resp:
                    if resp.getcode() == 200:
                        return
            except (urllib.error.URLError, OSError):
                time.sleep(0.1)
        raise RuntimeError("模拟服务未在限定时间内就绪")

    def _post(self, path: str, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path, data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def reset(self) -> None:
        self._post("/control/reset", {})

    def set_mode(self, mode: str) -> None:
        self._post("/control/mode", {"mode": mode})

    def schedule(self, rows: list[dict]) -> None:
        self._post("/control/schedule", {"rows": rows})

    def snapshot(self) -> dict:
        with urllib.request.urlopen(
                f"{self.base_url}/control/snapshot", timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))["snapshot"]


def write_csv(path: Path, header: list[str], data_rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(data_rows)


def run_cli(input_path: Path, report_path: Path, url: str,
            extra: list[str] | None = None) -> subprocess.CompletedProcess:
    cmd = [PYTHON, "-m", "employee_importer.cli",
           "--input", str(input_path),
           "--report", str(report_path),
           "--url", url]
    if extra:
        cmd.extend(extra)
    # 强制子进程 UTF-8 输出，避免 Windows 管道默认 GBK 导致中文解码失败
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", env=env, timeout=180)


def load_report(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def flat_entries(report: dict) -> dict[int, dict]:
    result: dict[int, dict] = {}
    for group in GROUPS:
        for entry in report[group]:
            result[entry["行号"]] = entry
    return result


def group_line_nos(report: dict) -> dict[str, list[int]]:
    return {group: [e["行号"] for e in report[group]] for group in GROUPS}


# ---------------------------------------------------------------- 14 项检查
def check_01_empty_values_and_line_numbers(srv: MockServer, work: Path) -> None:
    """1) 缺值不中断整批；报告行号与源文件物理行号逐行对应。"""
    csv_path = work / "c01.csv"
    # 第 3 行为全空白物理行（应跳过），后续行物理行号保持 4、5
    write_csv(csv_path, ["姓名", "邮箱", "部门"], [
        ["甲", "a@example.com", "研发部"],          # 物理行 2
        ["", "b@example.com", "市场部"],             # 物理行 3 之前插一空行 -> 见下
    ])
    with open(csv_path, "a", encoding="utf-8", newline="") as fh:
        fh.write(",,\n")                              # 物理行 4：全空行，跳过
        fh.write("丙,c@example.com,财务部\n")         # 物理行 5
        fh.write("丁,,\n")                            # 物理行 6：两个缺值
    report_path = work / "r01.json"
    proc = run_cli(csv_path, report_path, srv.base_url)
    check(proc.returncode == 0, f"CLI 退出码非 0: {proc.stderr}")
    report = load_report(report_path)
    lines = sorted(flat_entries(report).keys())
    check(lines == [2, 3, 5, 6], f"行号与源文件不对应: {lines}（预期 [2,3,5,6]，全空物理行 4 跳过）")
    check(report["汇总"]["总条数"] == 4, "缺值行导致批处理中断或计数错误")
    check(report["汇总"]["成功"] == 4, f"正常行应全部成功: {report['汇总']}")


def check_02_empty_fields_submitted_empty(srv: MockServer, work: Path) -> None:
    """2) 空字段留空提交，不提交占位内容。"""
    srv.reset()
    csv_path = work / "c02.csv"
    write_csv(csv_path, ["姓名", "邮箱", "部门"], [
        ["李明", "", "市场部"],       # 邮箱空
        ["", "no-name@example.com", ""],  # 姓名、部门空
    ])
    report_path = work / "r02.json"
    proc = run_cli(csv_path, report_path, srv.base_url)
    check(proc.returncode == 0, f"CLI 失败: {proc.stderr}")
    snap = srv.snapshot()
    records = snap["records"]
    check(len(records) == 2, f"应保存 2 条记录，实际 {len(records)}")
    first = next(r for r in records if r["name"] == "李明")
    check(first["email"] == "" and first["department"] == "市场部",
          f"空邮箱未以空字符串提交: {first}")
    second = next(r for r in records if r["email"] == "no-name@example.com")
    check(second["name"] == "" and second["department"] == "",
          f"空字段被填入占位内容: {second}")
    for key in ("name", "email", "department"):
        for record in records:
            check(isinstance(record[key], str), f"字段 {key} 类型异常")


def check_03_missing_record_id(srv: MockServer, work: Path) -> None:
    """3) 提交响应缺记录标识 -> 失败，且不触发回读。"""
    srv.reset()
    srv.schedule([
        {"submit": "no_id"},
        {},
    ])
    csv_path = work / "c03.csv"
    write_csv(csv_path, ["姓名", "邮箱", "部门"], [
        ["无标识", "noid@example.com", "研发部"],
        ["正常", "ok@example.com", "研发部"],
    ])
    report_path = work / "r03.json"
    proc = run_cli(csv_path, report_path, srv.base_url)
    check(proc.returncode == 0, f"CLI 失败: {proc.stderr}")
    report = load_report(report_path)
    entries = flat_entries(report)
    check(entries[2]["状态"] == "失败", f"缺标识行应判失败: {entries[2]}")
    check("记录标识" not in entries[2], "失败行不应携带记录标识")
    check(entries[3]["状态"] == "成功", "后续正常行应成功")
    # 缺标识的提交没有 record_id，状态查询计数应为 0；正常行查询 1 次
    snap = srv.snapshot()
    check(sum(snap["status_queries"].values()) == 1,
          f"缺标识行触发了回读: status_queries={snap['status_queries']}")


def check_04_pending_then_confirmed(srv: MockServer, work: Path) -> None:
    """4) 首次回读无结论，再次查询取得真实状态。"""
    srv.reset()
    srv.schedule([
        {"status": "pending_then_confirmed"},
    ])
    csv_path = work / "c04.csv"
    write_csv(csv_path, ["姓名", "邮箱", "部门"], [
        ["待确认", "pending@example.com", "研发部"],
    ])
    report_path = work / "r04.json"
    proc = run_cli(csv_path, report_path, srv.base_url)
    check(proc.returncode == 0, f"CLI 失败: {proc.stderr}")
    report = load_report(report_path)
    entries = flat_entries(report)
    check(entries[2]["状态"] == "成功", f"应在重试后成功: {entries[2]}")
    check(entries[2]["最终状态"] == "confirmed", "最终状态应为 confirmed")
    snap = srv.snapshot()
    record_id = entries[2]["记录标识"]
    check(snap["status_queries"].get(record_id) == 2,
          f"该记录应被查询 2 次: {snap['status_queries']}")


def check_05_page_failures_isolated(srv: MockServer, work: Path) -> None:
    """5) 元素缺失/页面加载失败仅取消该行，后续行正常。"""
    srv.reset()
    srv.schedule([
        {"page": "missing_element"},
        {"page": "fail_load"},
        {},
    ])
    csv_path = work / "c05.csv"
    write_csv(csv_path, ["姓名", "邮箱", "部门"], [
        ["缺陷页", "broken@example.com", "研发部"],
        ["加载败", "loadfail@example.com", "研发部"],
        ["正常行", "fine@example.com", "研发部"],
    ])
    report_path = work / "r05.json"
    proc = run_cli(csv_path, report_path, srv.base_url)
    check(proc.returncode == 0, f"CLI 失败: {proc.stderr}")
    report = load_report(report_path)
    entries = flat_entries(report)
    check(entries[2]["状态"] == "失败" and "元素缺失" in entries[2]["消息"],
          f"元素缺失行应失败: {entries[2]}")
    check(entries[3]["状态"] == "失败" and "加载失败" in entries[3]["消息"],
          f"加载失败行应失败: {entries[3]}")
    check(entries[4]["状态"] == "成功", f"后续行应正常成功: {entries[4]}")
    snap = srv.snapshot()
    check(len(snap["records"]) == 1, "失败行不应产生任何记录")


def check_06_transient_submit_single_success(srv: MockServer, work: Path) -> None:
    """6) 提交暂时故障后恢复；记录只成功一次，无重复无污染。"""
    srv.reset()
    srv.schedule([
        {"submit": "transient"},
        {},
    ])
    csv_path = work / "c06.csv"
    write_csv(csv_path, ["姓名", "邮箱", "部门"], [
        ["抖动行", "flaky@example.com", "研发部"],
        ["稳定行", "stable@example.com", "研发部"],
    ])
    report_path = work / "r06.json"
    proc = run_cli(csv_path, report_path, srv.base_url)
    check(proc.returncode == 0, f"CLI 失败: {proc.stderr}")
    report = load_report(report_path)
    groups = group_line_nos(report)
    check(groups["成功"] == [2, 3], f"两行均应成功: {groups}")
    check(groups["失败"] == [] and groups["不可用"] == [], "不应有失败/不可用条目")
    snap = srv.snapshot()
    check(len(snap["records"]) == 2,
          f"应恰好保存 2 条记录（无重复）: {len(snap['records'])}")
    # 注意：JSON 快照的字典键为字符串，行序号 0 即抖动行
    check(snap["submit_attempts"].get("0") == 2,
          f"抖动行应提交 2 次（首败次成）: {snap['submit_attempts']}")
    check(snap["submit_attempts"].get("1") == 1,
          f"稳定行应只提交 1 次: {snap['submit_attempts']}")
    # 内容核对：无占位、无串写
    flaky = next(r for r in snap["records"] if r["email"] == "flaky@example.com")
    check(flaky["name"] == "抖动行" and flaky["department"] == "研发部",
          f"重试成功的记录内容被污染: {flaky}")
    success_lines = [e["行号"] for e in report["成功"]]
    check(success_lines.count(2) == 1, "同一行在成功区出现多次")


def check_07_persistent_unavailable(srv: MockServer, work: Path) -> None:
    """7) 服务持续不可用 -> 需跟进区，不混入成功区。"""
    srv.reset()
    srv.schedule([
        {"status": "error_500"},   # 单记录状态接口持续故障
        {},
    ])
    csv_path = work / "c07a.csv"
    write_csv(csv_path, ["姓名", "邮箱", "部门"], [
        ["状态故障", "statusbad@example.com", "研发部"],
        ["正常行", "ok7@example.com", "研发部"],
    ])
    report_path = work / "r07a.json"
    proc = run_cli(csv_path, report_path, srv.base_url)
    check(proc.returncode == 0, f"CLI 失败: {proc.stderr}")
    report = load_report(report_path)
    entries = flat_entries(report)
    check(entries[2]["状态"] == "不可用", f"持续状态故障应为不可用: {entries[2]}")
    check(entries[3]["状态"] == "成功", "其他来源记录不受影响")
    check(group_line_nos(report)["成功"] == [3], "不可用记录混入成功区")

    # 提交接口全局持续 500
    srv.reset()
    srv.set_mode("submit_500")
    csv_path2 = work / "c07b.csv"
    write_csv(csv_path2, ["姓名", "邮箱", "部门"], [
        ["提交故障", "submitbad@example.com", "研发部"],
    ])
    report_path2 = work / "r07b.json"
    proc2 = run_cli(csv_path2, report_path2, srv.base_url)
    check(proc2.returncode == 0, f"CLI 失败: {proc2.stderr}")
    report2 = load_report(report_path2)
    entries2 = flat_entries(report2)
    check(entries2[2]["状态"] == "不可用",
          f"持续提交故障应为不可用: {entries2[2]}")
    check(report2["成功"] == [], "持续故障记录混入成功区")
    srv.set_mode("normal")


def check_08_prior_success_preserved(srv: MockServer, work: Path) -> None:
    """8) 中途注入故障，先前已回读成功的记录保留且状态不变。"""
    srv.reset()
    srv.schedule([
        {},
        {"status": "error_500"},
        {},
    ])
    csv_path = work / "c08.csv"
    write_csv(csv_path, ["姓名", "邮箱", "部门"], [
        ["先行成功", "first@example.com", "研发部"],
        ["中途故障", "middle@example.com", "研发部"],
        ["后续成功", "later@example.com", "研发部"],
    ])
    report_path = work / "r08.json"
    proc = run_cli(csv_path, report_path, srv.base_url)
    check(proc.returncode == 0, f"CLI 失败: {proc.stderr}")
    report = load_report(report_path)
    entries = flat_entries(report)
    check(entries[2]["状态"] == "成功" and entries[2]["最终状态"] == "confirmed",
          f"先前成功记录被改变: {entries[2]}")
    check(entries[3]["状态"] == "不可用", f"故障行应为不可用: {entries[3]}")
    check(entries[4]["状态"] == "成功", f"后续行应成功: {entries[4]}")
    # 再跑一次同输入前，先重置；这里直接断言首行记录标识在报告中稳定
    first_id = entries[2]["记录标识"]
    check(isinstance(first_id, str) and first_id.startswith("EMP-"),
          "成功记录缺少稳定标识")


def check_09_report_structure(work: Path) -> None:
    """9) 报告文件存在，条目数与输入行数一致，每条含行号/状态/消息。"""
    # 复用检查 1 的报告
    report = load_report(work / "r01.json")
    total = report["汇总"]["总条数"]
    entries = flat_entries(report)
    check(len(entries) == total, f"条目数 {len(entries)} 与汇总 {total} 不一致")
    for line_no, entry in entries.items():
        for key in ("行号", "状态", "消息"):
            check(key in entry, f"行 {line_no} 缺少字段 {key}")
        check(entry["状态"] in GROUPS,
              f"行 {line_no} 状态取值非法: {entry['状态']}")
        check(isinstance(entry["行号"], int), "行号必须为整数")


def check_10_groups_sorted(srv: MockServer, work: Path) -> None:
    """10) 成功/失败/不可用三组分列，组内按行号升序，组间不交叉。"""
    srv.reset()
    srv.schedule([
        {},                                  # 行2 成功
        {"submit": "no_id"},                 # 行3 失败
        {"status": "error_500"},             # 行4 不可用
        {"page": "missing_element"},         # 行5 失败
        {},                                  # 行6 成功
        {"page": "fail_load"},               # 行7 失败
    ])
    csv_path = work / "c10.csv"
    write_csv(csv_path, ["姓名", "邮箱", "部门"], [
        ["甲", "a1@example.com", "研发部"],
        ["乙", "a2@example.com", "研发部"],
        ["丙", "a3@example.com", "研发部"],
        ["丁", "a4@example.com", "研发部"],
        ["戊", "a5@example.com", "研发部"],
        ["己", "a6@example.com", "研发部"],
    ])
    report_path = work / "r10.json"
    proc = run_cli(csv_path, report_path, srv.base_url)
    check(proc.returncode == 0, f"CLI 失败: {proc.stderr}")
    report = load_report(report_path)
    groups = group_line_nos(report)
    check(groups["成功"] == sorted(groups["成功"]) == [2, 6],
          f"成功组排序/内容错误: {groups['成功']}")
    check(groups["失败"] == sorted(groups["失败"]) == [3, 5, 7],
          f"失败组排序/内容错误: {groups['失败']}")
    check(groups["不可用"] == sorted(groups["不可用"]) == [4],
          f"不可用组排序/内容错误: {groups['不可用']}")
    # 组间不交叉
    all_lines = groups["成功"] + groups["失败"] + groups["不可用"]
    check(len(all_lines) == len(set(all_lines)) == 6, "三组之间存在交叉条目")


def check_11_windows_paths(srv: MockServer, work: Path) -> None:
    """11) 命令行参数指定输入/报告路径，符合 Windows 写法并真实生效。"""
    srv.reset()
    report_path = work / "r11.json"
    if report_path.exists():
        report_path.unlink()
    win_input = str(SAMPLE_CSV)        # Windows 下 str() 即盘符+反斜杠写法
    win_report = str(report_path)
    check((":" in win_input and "\\" in win_input),
          f"路径不像 Windows 写法: {win_input}")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.run(
        [PYTHON, "-m", "employee_importer.cli",
         "--input", win_input, "--report", win_report,
         "--url", srv.base_url],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", env=env, timeout=180)
    check(proc.returncode == 0, f"Windows 路径运行失败: {proc.stderr}")
    check(report_path.exists(), f"报告未写到指定 Windows 路径: {win_report}")
    report = load_report(report_path)
    check(report["汇总"]["总条数"] == 7, "按指定路径读取的 CSV 条目数不符")
    check(report["输入文件"] == win_input, "报告中记录的输入路径与参数不一致")


def check_12_missing_input_no_report(work: Path) -> None:
    """12) 输入文件不存在 -> 报错退出且不生成报告。"""
    missing = work / "does_not_exist.csv"
    report_path = work / "r12.json"
    if report_path.exists():
        report_path.unlink()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.run(
        [PYTHON, "-m", "employee_importer.cli",
         "--input", str(missing), "--report", str(report_path),
         "--url", "http://127.0.0.1:9"],   # 端口无效也无妨：应在联网前退出
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", env=env)
    check(proc.returncode == 2, f"退出码应为 2，实际 {proc.returncode}")
    check("不存在" in proc.stderr or "无法读取" in proc.stderr,
          f"错误信息不符合预期: {proc.stderr}")
    check(not report_path.exists(), "输入缺失时仍生成了报告文件")


def check_13_console_output(srv: MockServer, work: Path) -> None:
    """13) 控制台逐行输出行号与状态，且与报告一致。"""
    srv.reset()
    srv.schedule([{"submit": "no_id"}, {}])
    csv_path = work / "c13.csv"
    write_csv(csv_path, ["姓名", "邮箱", "部门"], [
        ["控制台甲", "ca@example.com", "研发部"],
        ["控制台乙", "cb@example.com", "研发部"],
    ])
    report_path = work / "r13.json"
    proc = run_cli(csv_path, report_path, srv.base_url)
    check(proc.returncode == 0, f"CLI 失败: {proc.stderr}")
    report = load_report(report_path)
    entries = flat_entries(report)
    for line_no, entry in entries.items():
        token = f"行 {line_no} | {entry['状态']}"
        check(token in proc.stdout,
              f"控制台缺少逐行输出 {token!r}\n实际输出:\n{proc.stdout}")
    # 每行至少出现一次状态行
    for line_no in (2, 3):
        check(f"行 {line_no} |" in proc.stdout, f"行 {line_no} 无任何状态输出")


def check_14_end_to_end_sample(srv: MockServer, work: Path) -> None:
    """14) 可重复运行的本地样例：完整填报 -> 报告与预期一致。"""
    srv.reset()
    report_path = work / "r14_sample.json"
    proc = run_cli(SAMPLE_CSV, report_path, srv.base_url)
    check(proc.returncode == 0, f"样例运行失败: {proc.stderr}")
    check(report_path.exists(), "样例报告未生成")
    sys.path.insert(0, str(ROOT / "tests"))
    from compare_sample import compare
    problems = compare(load_report(report_path), load_report_json(EXPECTED_SUMMARY))
    check(not problems, "样例报告与预期不一致:\n" + "\n".join(problems))
    # 控制台与报告一致性（现场核对）
    report = load_report(report_path)
    for entry in report["成功"]:
        check(f"行 {entry['行号']} | 成功" in proc.stdout,
              f"控制台与报告不一致，缺少行 {entry['行号']} 成功输出")


def load_report_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- 主流程
def main() -> int:
    WORKDIR.mkdir(parents=True, exist_ok=True)
    checks = [
        ("01 缺值不中断且行号对应", check_01_empty_values_and_line_numbers),
        ("02 空字段留空提交", check_02_empty_fields_submitted_empty),
        ("03 缺记录标识判失败不回读", check_03_missing_record_id),
        ("04 无结论后再次确认", check_04_pending_then_confirmed),
        ("05 页面故障仅影响该行", check_05_page_failures_isolated),
        ("06 暂时故障恢复且无重复", check_06_transient_submit_single_success),
        ("07 持续不可用进需跟进区", check_07_persistent_unavailable),
        ("08 先前成功记录保持不变", check_08_prior_success_preserved),
        ("09 报告结构三字段", check_09_report_structure),
        ("10 三组分别列出且升序", check_10_groups_sorted),
        ("11 Windows 路径参数", check_11_windows_paths),
        ("12 输入缺失报错无报告", check_12_missing_input_no_report),
        ("13 控制台逐行输出", check_13_console_output),
        ("14 端到端样例与预期一致", check_14_end_to_end_sample),
    ]
    results: list[tuple[str, bool, str]] = []
    with MockServer() as srv:
        print(f"模拟服务已就绪: {srv.base_url}\n")
        for name, func in checks:
            try:
                params = func.__code__.co_varnames[:func.__code__.co_argcount]
                kwargs = {}
                if "srv" in params:
                    kwargs["srv"] = srv
                if "work" in params:
                    kwargs["work"] = WORKDIR
                func(**kwargs)
            except Exception as exc:  # noqa: BLE001 - 检查套件需要逐项隔离
                results.append((name, False, f"{type(exc).__name__}: {exc}"))
                print(f"[失败] {name}: {exc}")
            else:
                results.append((name, True, ""))
                print(f"[通过] {name}")

    print("\n" + "=" * 64)
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        marker = "PASS" if ok else "FAIL"
        line = f"[{marker}] {name}"
        if not ok:
            line += f" -> {detail}"
        print(line)
    print("=" * 64)
    print(f"合计 {passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
