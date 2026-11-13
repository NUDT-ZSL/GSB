#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ssti_probe.py — 本地授权 Web 端点模板注入（SSTI）离线检测工具

仅用于安全团队自建的本地授权演示端点。从 JSON 清单读取目标
（id / url / param / payloads），逐个发起探测请求，根据响应判定
目标状态，并输出 JSON 与 CSV 报告。

目标状态取值（仅限以下五种）：
  存在漏洞 / 疑似漏洞 / 未发现漏洞 / 响应异常 / 超时
"""

import argparse
import csv
import json
import logging
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ---------------- 约定常量 ----------------
USER_AGENT = "SSTI-LocalProbe/1.0 (+authorized-local-only)"
MAX_BODY_BYTES = 1024 * 1024          # 响应体超过 1MB 记为响应异常
CONNECT_TIMEOUT = 3.0                 # 连接/读取超时（秒）
LENGTH_DIFF_THRESHOLD = 0.10          # 非运算载荷长度偏差阈值 10%
BASELINE_VALUE = "probebaseline9f8d"  # 基线探测使用的无害值

STATUS_VULNERABLE = "存在漏洞"
STATUS_SUSPECTED = "疑似漏洞"
STATUS_CLEAN = "未发现漏洞"
STATUS_ABNORMAL = "响应异常"
STATUS_TIMEOUT = "超时"

RESULT_EXECUTED = "已执行"
RESULT_NOT_EXECUTED = "未执行"
RESULT_SUSPECTED = "疑似执行"

REQUIRED_FIELDS = ("id", "url", "param", "payloads")

# 形如 ${7*7} 的算术载荷
ARITH_RE = re.compile(r"\$\{\s*(\d+)\s*\*\s*(\d+)\s*\}")


# ---------------- 网络层 ----------------
def build_url(url, param, value):
    """把 URL 编码后的载荷放入指定查询参数。"""
    sep = "&" if urllib.parse.urlparse(url).query else "?"
    return "{}{}{}={}".format(
        url, sep,
        urllib.parse.quote(param, safe=""),
        urllib.parse.quote(value, safe=""),
    )


def fetch(url):
    """
    发起 GET 探测，仅携带约定 User-Agent，不附加其他自定义请求头。
    返回 (status_code, body_bytes, error_kind)；error_kind ∈ None/"timeout"/"network"。
    响应体最多读取 MAX_BODY_BYTES+1 字节，用于判定超限且避免无界下载。
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT) as resp:
            return resp.getcode(), resp.read(MAX_BODY_BYTES + 1), None
    except urllib.error.HTTPError as e:  # 非 2xx 状态
        return e.code, b"", None
    except (socket.timeout, TimeoutError):
        return None, b"", "timeout"
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason).lower():
            return None, b"", "timeout"
        return None, b"", "network"


# ---------------- 判定逻辑 ----------------
def standalone_number(text, num):
    """num 作为独立数字出现（前后不是其他数字）。"""
    return re.search(r"(?<!\d)" + re.escape(num) + r"(?!\d)", text) is not None


def analyze_payload(payload, body_bytes, baseline_len):
    """根据单个载荷的响应得出逐载荷结果。返回 (result, detail)。"""
    text = body_bytes.decode("utf-8", errors="replace")
    encoded = urllib.parse.quote(payload, safe="")

    # 规则1：响应含载荷原文或其 URL 编码形式 → 未被模板执行
    if payload in text or encoded.lower() in text.lower():
        return RESULT_NOT_EXECUTED, "响应包含载荷原文或其URL编码形式，判定未被模板执行"

    # 规则2：算术载荷 ${A*B}，响应中乘积作为独立数字出现 → 已被执行
    m = ARITH_RE.search(payload)
    if m:
        expected = str(int(m.group(1)) * int(m.group(2)))
        if standalone_number(text, expected):
            return RESULT_EXECUTED, "响应中出现运算结果 {}（独立数字），载荷被模板引擎执行".format(expected)
        return RESULT_NOT_EXECUTED, "未发现载荷反射或运算结果"

    # 规则3：非运算载荷，无反射且与基线长度偏差 >10% → 疑似
    diff = abs(len(body_bytes) - baseline_len) / max(baseline_len, 1)
    if diff > LENGTH_DIFF_THRESHOLD:
        return RESULT_SUSPECTED, (
            "响应未含载荷原文，长度 {} 与基线 {} 偏差 {:.1%} 超过 10%".format(
                len(body_bytes), baseline_len, diff)
        )
    return RESULT_NOT_EXECUTED, "未发现载荷反射、执行迹象或显著长度差异"


def final_status(payload_results):
    """目标最终状态仅依据其各载荷结果得出。"""
    results = [r["result"] for r in payload_results]
    if RESULT_EXECUTED in results:
        return STATUS_VULNERABLE
    if RESULT_SUSPECTED in results:
        return STATUS_SUSPECTED
    return STATUS_CLEAN


# ---------------- 目标探测 ----------------
def probe_target(target, log):
    tid, url, param = target["id"], target["url"], target["param"]
    payloads = target["payloads"]
    found_at = datetime.now(timezone.utc).isoformat()
    entry = {"id": tid, "url": url, "status": None,
             "found_at": found_at, "payload_results": []}

    log.info("开始检测目标 id=%s url=%s 载荷数=%d", tid, url, len(payloads))

    # 基线请求：无害值，用于长度对比
    code, body, err = fetch(build_url(url, param, BASELINE_VALUE))
    if err:
        entry["status"] = STATUS_TIMEOUT
        log.warning("目标 id=%s 基线请求%s，记为超时", tid,
                    "超时" if err == "timeout" else "连接失败（端点不可用）")
        log.info("目标 id=%s 检测结束 状态=%s", tid, entry["status"])
        return entry
    if code != 200 or len(body) > MAX_BODY_BYTES:
        entry["status"] = STATUS_ABNORMAL
        log.warning("目标 id=%s 基线响应异常（状态码=%s 长度=%d），不参与漏洞判定",
                    tid, code, len(body))
        log.info("目标 id=%s 检测结束 状态=%s", tid, entry["status"])
        return entry
    baseline_len = len(body)
    log.info("目标 id=%s 基线响应 状态码=%d 长度=%d", tid, code, baseline_len)

    for payload in payloads:
        code, body, err = fetch(build_url(url, param, payload))
        if err:
            entry["status"] = STATUS_TIMEOUT
            entry["payload_results"].append({
                "payload": payload, "result": "未检测",
                "detail": "请求{}，目标记为超时".format(
                    "超时" if err == "timeout" else "连接失败")})
            log.warning("目标 id=%s 载荷探测%s，记为超时", tid,
                        "超时" if err == "timeout" else "连接失败")
            log.info("目标 id=%s 检测结束 状态=%s", tid, entry["status"])
            return entry
        if code != 200 or len(body) > MAX_BODY_BYTES:
            entry["status"] = STATUS_ABNORMAL
            entry["payload_results"].append({
                "payload": payload, "result": "未检测",
                "detail": "响应异常（状态码={} 长度={}），不参与漏洞判定".format(code, len(body))})
            log.warning("目标 id=%s 载荷响应异常（状态码=%s 长度=%d），不参与漏洞判定",
                        tid, code, len(body))
            log.info("目标 id=%s 检测结束 状态=%s", tid, entry["status"])
            return entry
        result, detail = analyze_payload(payload, body, baseline_len)
        entry["payload_results"].append(
            {"payload": payload, "result": result, "detail": detail})
        log.info("目标 id=%s 载荷结果=%s（响应状态码=%d 长度=%d）",
                 tid, result, code, len(body))

    entry["status"] = final_status(entry["payload_results"])
    log.info("目标 id=%s 检测结束 状态=%s", tid, entry["status"])
    return entry


# ---------------- 报告输出 ----------------
def write_reports(entries, json_path, csv_path, log):
    report = {
        "tool": "ssti_probe",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "targets": entries,
    }
    with open(json_path, "w", encoding="utf-8", newline="") as f:  # UTF-8，缩进两格，\n 换行
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:  # UTF-8，逗号分隔
        w = csv.writer(f)
        w.writerow(["目标ID", "URL", "状态", "发现时间"])
        for e in entries:
            w.writerow([e["id"], e["url"], e["status"], e["found_at"]])
    log.info("报告已写出：%s / %s（目标数=%d）", json_path, csv_path, len(entries))


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser(description="本地授权端点 SSTI 离线检测工具")
    ap.add_argument("--targets", required=True, help="目标清单 JSON 文件")
    ap.add_argument("--json-out", default="report.json")
    ap.add_argument("--csv-out", default="report.csv")
    ap.add_argument("--log", default="probe.log")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(args.log, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )
    log = logging.getLogger("ssti_probe")

    with open(args.targets, "r", encoding="utf-8") as f:
        targets = json.load(f)
    if not isinstance(targets, list):
        log.error("目标清单顶层必须是数组")
        return 2

    if len(targets) == 0:
        log.warning("目标清单为空，生成空报告")
        write_reports([], args.json_out, args.csv_out, log)
        return 0

    entries = []
    for i, t in enumerate(targets):
        missing = [k for k in REQUIRED_FIELDS if k not in t]
        if missing or not isinstance(t.get("payloads"), list):
            log.warning("第 %d 个目标缺少必要字段 %s 或 payloads 非数组，跳过该目标",
                        i, missing or ["payloads"])
            continue
        entries.append(probe_target(t, log))

    write_reports(entries, args.json_out, args.csv_out, log)
    for e in entries:
        log.info("汇总：id=%s 状态=%s", e["id"], e["status"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
