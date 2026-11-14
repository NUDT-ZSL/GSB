# -*- coding: utf-8 -*-
"""
run_demo.py —— LFI 体检工具的本地确定性验收演示（覆盖任务全部 11 条验收点）

本脚本自动完成：
  1. 启动内置的易受攻击 / 安全 / 慢响应本地 HTTP 服务；
  2. 以子进程方式调用 lfi_scanner.py（与业务人员真实使用方式一致）；
  3. 制造“无监听端口”“先不可用后恢复”“连接超时”等场景；
  4. 对每份 JSON 报告做断言，逐条映射验收点并打印 [通过]/[失败]。

全程仅访问 127.0.0.1，不联网、不访问任何未授权目标、不使用容器。

运行：
  python run_demo.py
"""

import json
import locale
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

# -I 隔离模式下 cwd 不在 sys.path 中，显式加入脚本自身目录以导入同目录模块
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sample_server

# Windows 控制台默认编码可能是 GBK，统一按 UTF-8 输出，保证中文不乱码
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
SCANNER = ROOT / "lfi_scanner.py"
WORK = ROOT / "demo_work"          # 演示产物目录（清单、报告）
APP_ROOT = ROOT                    # 应用根目录参数就用本项目根，保证存在

failures = []


def check(point, condition, detail=""):
    """单条验收断言：记录并打印结果。"""
    status = "[通过]" if condition else "[失败]"
    print("%s %s %s" % (status, point, detail))
    if not condition:
        failures.append(point + (" " + detail if detail else ""))


def free_port():
    """申请一个由操作系统分配的空闲端口。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class ServerThread(threading.Thread):
    """以守护线程方式运行样本 HTTP 服务，主线程结束时自动回收。"""

    def __init__(self, mode, port, delay=3.0):
        super().__init__(daemon=True)
        self.httpd = sample_server.build_server(mode, port, delay)
        self.port = port

    def run(self):
        self.httpd.serve_forever()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def write_url_list(name, urls):
    path = WORK / name
    path.write_text("\n".join(urls) + "\n", encoding="utf-8")
    return path


def run_scanner(url_list, report_name, timeout=5.0, retries=2,
                retry_delay=0.3, app_root=APP_ROOT):
    """以子进程方式调用扫描器，返回 (退出码, stdout, stderr, 报告路径)。"""
    report = WORK / report_name
    cmd = [
        sys.executable, "-I", str(SCANNER),
        str(app_root), str(url_list),
        "--output", str(report),
        "--timeout", str(timeout),
        "--retries", str(retries),
        "--retry-delay", str(retry_delay),
    ]
    proc = subprocess.run(cmd, capture_output=True)
    enc = locale.getpreferredencoding(False) or "utf-8"
    stdout = proc.stdout.decode(enc, errors="replace")
    stderr = proc.stderr.decode(enc, errors="replace")
    return proc.returncode, stdout, stderr, report


def load_report(path):
    with Path(path).open("r", encoding="utf-8") as fp:
        return json.load(fp)


def find_entry(report, url):
    for entry in report["逐条结果"]:
        if entry["url"] == url:
            return entry
    return None


def assert_common_fields(point, report, expected_total):
    """验收点3/7 共用的报告结构断言。"""
    check(point + "：报告为合法 JSON 且列表长度等于清单 URL 数",
          isinstance(report, dict) and
          len(report["逐条结果"]) == expected_total)
    summary = report["汇总"]
    counted = (summary["确认安全数"] + summary["疑似LFI数"]
               + summary["未完成数"])
    check(point + "：汇总数值与逐条结论计数一致",
          summary["总数"] == expected_total == counted)
    for entry in report["逐条结果"]:
        ok = (all(k in entry for k in
                  ("url", "结论", "检测时间", "证据", "错误说明"))
              and bool(str(entry["证据"]).strip()))
        check(point + "：每条含 URL/结论/时间戳且证据非空 -> %s" % entry["url"],
              ok)
        if entry["结论"] != "未完成":
            has_param = any("param" in r and r["param"]
                            for r in entry.get("request_evidence", []))
            check(point + "：请求参数证据非空 -> %s" % entry["url"], has_param)


def main():
    WORK.mkdir(exist_ok=True)
    print("=" * 72)
    print("LFI 授权批量体检 —— 本地确定性验收演示")
    print("工作目录: %s" % WORK)
    print("=" * 72)

    # 预分配所有端口
    vuln_port, safe_port, slow_port = free_port(), free_port(), free_port()
    closed_port = free_port()      # 始终不监听，模拟服务不可用
    flaky_port = free_port()       # 先不监听，演示中途再启动
    vuln_url = "http://127.0.0.1:%d/vuln" % vuln_port
    safe_url = "http://127.0.0.1:%d/safe" % safe_port
    slow_url = "http://127.0.0.1:%d/slow" % slow_port
    closed_url = "http://127.0.0.1:%d/vuln" % closed_port
    flaky_url = "http://127.0.0.1:%d/safe" % flaky_port

    # 启动三个常驻样本服务
    vuln_server = ServerThread("vulnerable", vuln_port)
    safe_server = ServerThread("safe", safe_port)
    slow_server = ServerThread("slow", slow_port, delay=3.0)
    for srv in (vuln_server, safe_server, slow_server):
        srv.start()
    time.sleep(0.3)

    # ------------------------------------------------------------------
    # 场景 S0（验收点1）：输入校验 —— 目录不存在 / 清单含非法 URL
    # ------------------------------------------------------------------
    print("\n--- 场景 S0：输入参数校验（验收点1） ---")
    missing_dir = WORK / "no_such_app_root"
    good_list = write_url_list("s0_good.txt", [safe_url])
    code, out, err, _ = run_scanner(good_list, "s0a.json",
                                    app_root=missing_dir)
    check("S0a 不存在的应用根目录 -> 非零退出并打印原因",
          code != 0 and ("不存在" in err or "错误" in err),
          "exit=%d stderr=%r" % (code, err.strip()))

    bad_list = write_url_list("s0_bad.txt", [
        safe_url, "ftp://127.0.0.1/x", "not-a-url",
    ])
    code, out, err, _ = run_scanner(bad_list, "s0b.json")
    check("S0b 清单含非法 URL -> 非零退出并打印原因",
          code != 0 and ("非法" in err or "错误" in err),
          "exit=%d stderr=%r" % (code, err.strip()))

    # ------------------------------------------------------------------
    # 场景 S1（验收点2/3/7/8/11）：易受攻击=疑似LFI，安全=确认安全
    # ------------------------------------------------------------------
    print("\n--- 场景 S1：易受攻击与安全两类服务（验收点2/3/7/8/11） ---")
    s1_list = write_url_list("s1_urls.txt", [vuln_url, safe_url])
    code, out, err, report_path = run_scanner(s1_list, "s1_report.json")
    check("S1 扫描器退出码为 0 且报告文件存在",
          code == 0 and report_path.is_file())
    report = load_report(report_path)
    assert_common_fields("S1", report, expected_total=2)

    vuln_entry = find_entry(report, vuln_url)
    safe_entry = find_entry(report, safe_url)
    check("S1 易受攻击服务结论=疑似LFI",
          vuln_entry is not None and vuln_entry["结论"] == "疑似LFI")
    check("S1 疑似条目命中敏感文件特征串且证据含特征内容",
          vuln_entry is not None and bool(vuln_entry["命中特征串"])
          and any(sig in vuln_entry["证据"]
                  for sig in vuln_entry["命中特征串"]))
    check("S1 安全服务结论=确认安全（无特征串不判疑似，验收点8）",
          safe_entry is not None and safe_entry["结论"] == "确认安全"
          and safe_entry["命中特征串"] == [])

    # ------------------------------------------------------------------
    # 场景 S2（验收点4/9/10）：无监听端口 + 慢响应超时 -> 未完成
    # ------------------------------------------------------------------
    print("\n--- 场景 S2：服务不可用与连接超时（验收点4/9/10） ---")
    s2_list = write_url_list("s2_urls.txt",
                             [vuln_url, closed_url, slow_url])
    # 超时设 1 秒：slow 服务睡眠 3 秒，稳定触发超时；不重试便于快速验证
    code, out, err, report_path = run_scanner(
        s2_list, "s2_report.json", timeout=1.0, retries=0)
    check("S2 扫描器退出码为 0（单条失败不影响整体，验收点6）",
          code == 0 and report_path.is_file())
    report = load_report(report_path)
    assert_common_fields("S2", report, expected_total=3)

    closed_entry = find_entry(report, closed_url)
    slow_entry = find_entry(report, slow_url)
    check("S2 无监听端口 URL 结论=未完成，且不在安全/疑似列表中（验收点4）",
          closed_entry is not None
          and closed_entry["结论"] == "未完成"
          and closed_url not in report["汇总"]["确认安全_URL清单"]
          and closed_url not in report["汇总"]["疑似LFI_URL清单"])
    check("S2 超时条目错误字段非空且提及超时（验收点9）",
          slow_entry is not None and slow_entry["结论"] == "未完成"
          and bool(slow_entry["错误说明"])
          and "超时" in slow_entry["错误说明"])
    check("S2 汇总区单独列出未完成 URL 清单且内容准确（验收点10）",
          report["汇总"]["未完成数"] == 2
          and set(report["汇总"]["未完成_URL清单"]) == {closed_url, slow_url})

    # ------------------------------------------------------------------
    # 场景 S3（验收点5）：服务先不可用，重试期间恢复 -> 得到正常结论
    # ------------------------------------------------------------------
    print("\n--- 场景 S3：暂时不可用后恢复，重试成功（验收点5） ---")
    s3_list = write_url_list("s3_urls.txt", [flaky_url])
    recovered = {"done": False}

    def bring_back():
        time.sleep(2.0)  # 前两次尝试时端口无监听
        srv = ServerThread("safe", flaky_port)
        srv.start()
        recovered["done"] = True
        recovered["server"] = srv

    threading.Thread(target=bring_back, daemon=True).start()
    code, out, err, report_path = run_scanner(
        s3_list, "s3_report.json", timeout=2.0, retries=4, retry_delay=1.0)
    check("S3 恢复线程已在重试期间启动服务", recovered["done"])
    check("S3 扫描器退出码为 0", code == 0 and report_path.is_file())
    report = load_report(report_path)
    flaky_entry = find_entry(report, flaky_url)
    check("S3 同一 URL 恢复后结论=确认安全（验收点5）",
          flaky_entry is not None
          and flaky_entry["结论"] in ("确认安全", "疑似LFI")
          and flaky_entry["结论"] != "未完成")
    check("S3 条目记录的尝试次数大于 1，证明确实发生过重试",
          flaky_entry is not None and flaky_entry["尝试次数"] > 1)

    # ------------------------------------------------------------------
    # 场景 S4（验收点6）：故障隔离 —— 混入不可用 URL，其余结论与 S1 一致
    # ------------------------------------------------------------------
    print("\n--- 场景 S4：单 URL 失败不影响其他 URL（验收点6） ---")
    s4_list = write_url_list("s4_urls.txt",
                             [vuln_url, closed_url, safe_url])
    code, out, err, report_path = run_scanner(s4_list, "s4_report.json")
    report = load_report(report_path)
    s4_vuln = find_entry(report, vuln_url)
    s4_safe = find_entry(report, safe_url)
    check("S4 混入不可用 URL 后，疑似LFI 条目结论与单独运行一致",
          s4_vuln is not None and s4_vuln["结论"]
          == vuln_entry["结论"] == "疑似LFI")
    check("S4 混入不可用 URL 后，确认安全条目结论与单独运行一致",
          s4_safe is not None and s4_safe["结论"]
          == safe_entry["结论"] == "确认安全")
    check("S4 不可用 URL 仍被正确标记为未完成且成功结果完整保留",
          find_entry(report, closed_url)["结论"] == "未完成"
          and report["汇总"]["总数"] == 3)

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------
    for srv in (vuln_server, safe_server, slow_server):
        srv.stop()
    if "server" in recovered:
        recovered["server"].stop()

    print("\n" + "=" * 72)
    if failures:
        print("演示结束：存在 %d 条失败项：" % len(failures))
        for item in failures:
            print("  - " + item)
        return 1
    print("演示结束：全部验收点通过。报告产物位于 %s" % WORK)
    print("可打开 s1_report.json / s2_report.json / s3_report.json 查看证据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
