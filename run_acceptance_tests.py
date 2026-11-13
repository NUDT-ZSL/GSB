"""端到端验收测试：覆盖任务要求的 14 个验收点（仅标准库，本地回环）。

运行方式：
  python run_acceptance_tests.py

每个测试在 tests_workspace 下使用独立子目录与独立临时端口，互不干扰。
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

# Windows 控制台默认可能是 GBK，统一强制 UTF-8 输出
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT / "samples"
WORKSPACE = ROOT / "tests_workspace"
PYTHON = sys.executable

# 确定性样本对应的期望数据行（来源, 商品名, 报价）
# 排序口径：Python 字符串默认的 Unicode 码位字母序（标准库无拼音排序，
# 码位序同样是确定性的全序，重复运行结果一致）
EXPECTED_ROWS = [
    ("博远机电", "三相异步电机 3kW", "899.99"),
    ("博远机电", "交流接触器 25A", "62.45"),
    ("宏发五金", "不锈钢螺丝 M6x20", "0.85"),
    ("宏发五金", "六角螺母 M6", "0.32"),
    ("宏发五金", "平垫片 6mm", "0.10"),
    ("鑫源建材", "水泥 42.5 袋装", "28.50"),
    ("鑫源建材", "河沙 立方米", "145.00"),
]
ALL_NAMES = sorted(["博远机电", "宏发五金", "鑫源建材"])

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str, bool, str]] = []


def check(accept_id: str, title: str, condition: bool, detail: str = "") -> None:
    results.append((accept_id, title, condition, detail))
    mark = "✔" if condition else "✘"
    print(f"[{mark}] {accept_id} {title}" + (f" —— {detail}" if detail and not condition else ""))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(samples_dir: Path, flaky_page: str | None = None,
                 flaky_fail: int = 0) -> tuple[ThreadingHTTPServer, threading.Thread, int]:
    sys.path.insert(0, str(ROOT))
    from supplier_server import make_server  # noqa: E402

    httpd = make_server("127.0.0.1", free_port(), samples_dir,
                        flaky_page=flaky_page, flaky_fail=flaky_fail)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    # 就绪握手：确认端口可连接后再返回
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.05)
    return httpd, thread, port


def stop_server(httpd: ThreadingHTTPServer) -> None:
    httpd.shutdown()
    httpd.server_close()


def make_config(scene_dir: Path, port: int, suppliers: list[dict],
                output_subdir: str = "output", extra: dict | None = None) -> Path:
    cfg = {
        "server": {"host": "127.0.0.1", "port": port},
        "output": {
            "dir": output_subdir,
            "csv_file": "quotes.csv",
            "report_file": "report.json",
            "log_file": "collector.log",
        },
        "http": {"timeout_seconds": 3, "retry_attempts": 4, "backoff_seconds": 0.1},
        "suppliers": suppliers,
    }
    if extra:
        cfg.update(extra)
    path = scene_dir / "config.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_collector(config_path: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return subprocess.run(
        [PYTHON, "-I", str(ROOT / "collector.py"), "--config", str(config_path)],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(ROOT),
    )


def read_csv_rows(csv_path: Path) -> list[list[str]]:
    with csv_path.open(encoding="utf-8", newline="") as fh:
        return list(csv.reader(fh))


def read_report(report_path: Path) -> dict:
    return json.loads(report_path.read_text(encoding="utf-8"))


def main() -> int:
    if WORKSPACE.exists():
        shutil.rmtree(WORKSPACE)
    WORKSPACE.mkdir(parents=True)

    httpd, thread, port = start_server(SAMPLES)
    try:
        # ---- 场景 A：三个正常来源全成功（验收 2/3/4/5/11/12/13/14 的基础） ----
        scene_a = WORKSPACE / "scene_a_full"
        scene_a.mkdir()
        cfg_a = make_config(scene_a, port, [
            {"name": "宏发五金", "page_id": "hongfa"},
            {"name": "鑫源建材", "page_id": "xinyuan"},
            {"name": "博远机电", "page_id": "boyuan"},
        ])
        proc_a = run_collector(cfg_a)
        out_a = scene_a / "output"
        rows_a = read_csv_rows(out_a / "quotes.csv")
        report_a = read_report(out_a / "report.json")
        log_a = (out_a / "collector.log").read_text(encoding="utf-8")

        # 验收 2：回环访问返回与样本一致的内容和成功状态
        import urllib.request  # noqa: E402
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/hongfa", timeout=3) as resp:
            status = resp.status
            served = resp.read().decode("utf-8")
        check("验收2", "回环按页面标识返回样本内容与成功状态",
              status == 200 and served == (SAMPLES / "hongfa.html").read_text(encoding="utf-8"),
              f"status={status}")

        # 验收 3：名称/报价与页面一致，报价两位小数且为数值写法
        price_col = [r[2] for r in rows_a[1:]]
        prices_numeric = all(p.count(".") == 1 and len(p.split(".")[1]) == 2
                             and float(p) == float(p) for p in price_col)
        sample_match = ("不锈钢螺丝 M6x20", "0.85") in [(r[1], r[2]) for r in rows_a[1:]]
        check("验收3", "提取名称/报价与页面一致且报价保留两位小数",
              prices_numeric and sample_match)

        # 验收 4：CSV 表头顺序与成功记录数
        check("验收4", "CSV 表头顺序为 来源,商品名,报价 且记录数为 7",
              rows_a[0] == ["来源", "商品名", "报价"] and len(rows_a) - 1 == len(EXPECTED_ROWS),
              f"表头={rows_a[0]} 数据行={len(rows_a) - 1}")

        # 验收 5：报告两个列表字母序且与实际一致
        check("验收5", "报告完整/未完成列表均字母升序且与实际一致",
              report_a["completed_sources"] == ALL_NAMES and report_a["incomplete_sources"] == []
              and report_a["completed_sources"] == sorted(report_a["completed_sources"]),
              f"report={report_a}")

        # 验收 11（全成功分支）：退出码 0
        check("验收11a", "全部来源完整时退出码为 0", proc_a.returncode == 0,
              f"returncode={proc_a.returncode}, stderr={proc_a.stderr.strip()}")

        # 验收 12：输出目录自动创建 + ISO 8601 可解析
        scene_fresh = WORKSPACE / "scene_fresh"
        scene_fresh.mkdir()
        cfg_fresh = make_config(scene_fresh, port, [
            {"name": "宏发五金", "page_id": "hongfa"},
        ], output_subdir="nested/new_output")
        proc_fresh = run_collector(cfg_fresh)
        fresh_out = scene_fresh / "nested" / "new_output"
        files_in_place = all((fresh_out / name).is_file()
                             for name in ("quotes.csv", "report.json", "collector.log"))
        iso_ok = False
        if (fresh_out / "report.json").is_file():
            ts = read_report(fresh_out / "report.json")["collected_at"]
            datetime.fromisoformat(ts.replace("Z", "+00:00"))  # 标准解析器不抛异常即可
            iso_ok = True
        check("验收12", "全新输出目录自动创建且采集时间为可解析 ISO 8601",
              proc_fresh.returncode == 0 and files_in_place and iso_ok)

        # 验收 13：日志为纯文本，每个来源均有结果行且含可读耗时
        result_lines = [ln for ln in log_a.splitlines() if "来源结果:" in ln]
        each_source = all(any(f"来源结果: {n} " in ln for ln in result_lines) for n in ALL_NAMES)
        each_elapsed = all("耗时: " in ln and "秒" in ln for ln in result_lines)
        check("验收13", "日志逐来源记录结果行与可读耗时",
              len(result_lines) == 3 and each_source and each_elapsed,
              f"结果行数={len(result_lines)}")

        # 验收 14：同一样本重复运行，CSV 数据行内容与排序完全一致
        scene_a2 = WORKSPACE / "scene_a_rerun"
        scene_a2.mkdir()
        cfg_a2 = make_config(scene_a2, port, [
            {"name": "宏发五金", "page_id": "hongfa"},
            {"name": "鑫源建材", "page_id": "xinyuan"},
            {"name": "博远机电", "page_id": "boyuan"},
        ])
        run_collector(cfg_a2)
        rows_a2 = read_csv_rows(scene_a2 / "output" / "quotes.csv")
        check("验收14", "重复运行 CSV 数据行内容与排序完全一致",
              rows_a == rows_a2 and [tuple(r) for r in rows_a[1:]] == EXPECTED_ROWS,
              "与期望行不一致" if [tuple(r) for r in rows_a[1:]] != EXPECTED_ROWS else "")

        # ---- 验收 1：合法配置可启动；缺字段配置提示具体缺失项 ----
        bad_dir = WORKSPACE / "scene_bad_config"
        bad_dir.mkdir()
        bad_cfg = bad_dir / "bad_config.json"
        good = json.loads(cfg_a.read_text(encoding="utf-8"))
        del good["suppliers"][0]["page_id"]
        bad_cfg.write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
        proc_bad = run_collector(bad_cfg)
        check("验收1", "缺字段配置提示具体缺失项且拒绝启动",
              proc_bad.returncode == 2 and "suppliers[0].page_id" in proc_bad.stdout
              and "缺少必填字段" in proc_bad.stdout,
              f"stdout={proc_bad.stdout.strip()}")

        # ---- 验收 6 & 10：来源不可访问（连接拒绝）不影响其他来源 ----
        dead_port = free_port()  # 无进程监听 -> 连接拒绝
        scene_dead = WORKSPACE / "scene_dead"
        scene_dead.mkdir()
        cfg_dead = make_config(scene_dead, port, [
            {"name": "宏发五金", "page_id": "hongfa"},
            {"name": "鑫源建材", "page_id": "xinyuan"},
            {"name": "失联供应商", "page_id": "missing_page", "port": dead_port},
        ])
        proc_dead = run_collector(cfg_dead)
        out_dead = scene_dead / "output"
        rows_dead = read_csv_rows(out_dead / "quotes.csv")
        report_dead = read_report(out_dead / "report.json")
        dead_sources = {r[0] for r in rows_dead[1:]}
        no_null = all(all(cell.strip() != "" for cell in r) for r in rows_dead[1:])
        check("验收6", "不可访问来源列入未完成且其他来源数据完整",
              "失联供应商" in report_dead["incomplete_sources"]
              and report_dead["completed_sources"] == sorted(["宏发五金", "鑫源建材"])
              and "失联供应商" not in dead_sources and len(rows_dead) - 1 == 5)
        check("验收10", "持续不可用来源不导致成功数据丢失或空值污染，退出码为 1",
              proc_dead.returncode == 1 and no_null
              and report_dead["incomplete_sources"] == ["失联供应商"],
              f"returncode={proc_dead.returncode}")

        # ---- 验收 7：结构异常页面，整来源未完成且不写入任何商品 ----
        scene_broken = WORKSPACE / "scene_broken"
        scene_broken.mkdir()
        cfg_broken = make_config(scene_broken, port, [
            {"name": "宏发五金", "page_id": "hongfa"},
            {"name": "异常供应商", "page_id": "broken"},
        ])
        proc_broken = run_collector(cfg_broken)
        out_broken = scene_broken / "output"
        rows_broken = read_csv_rows(out_broken / "quotes.csv")
        report_broken = read_report(out_broken / "report.json")
        broken_sources = {r[0] for r in rows_broken[1:]}
        check("验收7", "结构异常页面来源不写入任何商品并列为未完成",
              "异常供应商" in report_broken["incomplete_sources"]
              and "异常供应商" not in broken_sources
              and report_broken["completed_sources"] == ["宏发五金"]
              and proc_broken.returncode == 1)

        # ---- 验收 8：N/A 报价只跳过该商品，同页其他商品正常 ----
        scene_na = WORKSPACE / "scene_na"
        scene_na.mkdir()
        cfg_na = make_config(scene_na, port, [{"name": "鑫源建材", "page_id": "xinyuan"}])
        proc_na = run_collector(cfg_na)
        rows_na = read_csv_rows(scene_na / "output" / "quotes.csv")
        names_na = [r[1] for r in rows_na[1:]]
        check("验收8", "N/A 报价商品被跳过且同页其余商品正常入 CSV",
              proc_na.returncode == 0 and names_na == ["水泥 42.5 袋装", "河沙 立方米"]
              and "红砖 千块" not in names_na and len(rows_na) - 1 == 2)

    finally:
        stop_server(httpd)
        thread.join(timeout=5)

    # ---- 验收 9：短暂不可用后恢复，重试后仍进入完整列表（独立 flaky 服务） ----
    flaky_httpd, flaky_thread, flaky_port = start_server(SAMPLES, flaky_page="boyuan", flaky_fail=2)
    try:
        scene_flaky = WORKSPACE / "scene_flaky"
        scene_flaky.mkdir()
        cfg_flaky = make_config(scene_flaky, flaky_port, [
            {"name": "博远机电", "page_id": "boyuan"},
        ])
        proc_flaky = run_collector(cfg_flaky)
        report_flaky = read_report(scene_flaky / "output" / "report.json")
        rows_flaky = read_csv_rows(scene_flaky / "output" / "quotes.csv")
        check("验收9", "来源短暂不可用恢复后经重试完成采集并进入完整列表",
              proc_flaky.returncode == 0
              and report_flaky["completed_sources"] == ["博远机电"]
              and report_flaky["incomplete_sources"] == []
              and len(rows_flaky) - 1 == 2,
              f"returncode={proc_flaky.returncode}")
    finally:
        stop_server(flaky_httpd)
        flaky_thread.join(timeout=5)

    # ---- 验收 11（部分未完成分支）已在验收 6/7 覆盖，这里显式汇总断言 ----
    check("验收11b", "存在未完成来源时退出码为 1",
          proc_dead.returncode == 1 and proc_broken.returncode == 1)

    total = len(results)
    fail_count = sum(1 for _id, _title, ok, _detail in results if not ok)
    print("\n================ 验收汇总 ================")
    for accept_id, title, ok, _detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {accept_id} {title}")
    print(f"共 {total} 项，通过 {total - fail_count} 项，失败 {fail_count} 项")
    return 1 if fail_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
