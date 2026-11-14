#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sqli_check.py — 离线 SQL 注入检测与修复验证工具（仅 Python 标准库）。

接收漏洞版与修复版两个启动命令，分别拉起本地服务、等待就绪、
执行联合查询与布尔盲注两类载荷检测，输出可复核的 JSON 报告。

用法:
  python sqli_check.py --vuln-cmd "python samples/vuln_server.py --port {port}" ^
                       --fixed-cmd "python samples/fixed_server.py --port {port}" ^
                       --report report.json

  启动命令中的 {port} 会被替换为工具分配的实际端口。
  不带参数运行：生成仅含 null 占位的 report.json。
  参数错误：argparse 输出用法提示并以退出码 2 结束，不崩溃。

报告四个固定字段:
  vulnerable_version  漏洞版结果（未执行/不可用为 null 或带 reason）
  fixed_version       修复版结果
  comparison_summary  两版对比摘要与修复有效性结论
  evidence            全部载荷证据列表（无执行则为 null）

测试钩子（用于验收模拟，不影响正常逻辑）:
  --simulate-timeout-payload PAYLOAD_ID  使指定载荷请求必然超时，验证"未完成"标注
  --simulate-cleanup-failure             模拟清理失败，验证报告警告与残留标识
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

DEFAULT_VULN_PORT = 18301
DEFAULT_FIXED_PORT = 18302
EVIDENCE_SNIPPET_LEN = 200

# ---------------------------------------------------------------- 载荷定义
# 样本库 users 表内容: admin/S3cret!Admin, alice/wonderland123, bob/builder456
PAYLOADS = [
    {
        "id": "union_select_users",
        "type": "union",
        "description": "联合查询注入: ' UNION SELECT 提取 users 表全部凭据",
        "params": {
            "username": "' UNION SELECT username, password FROM users-- ",
            "password": "x",
        },
        # 未以 alice/bob 身份登录，响应出现其口令即证明 UNION 提取成功
        "markers": ["wonderland123", "builder456", "S3cret!Admin"],
    },
    {
        "id": "boolean_blind_and",
        "type": "boolean",
        "description": "布尔盲注: admin' AND '1'='1'-- 与 admin' AND '1'='2'-- 的响应差异",
        "true_params": {"username": "admin' AND '1'='1'-- ", "password": "x"},
        "false_params": {"username": "admin' AND '1'='2'-- ", "password": "x"},
    },
]


# ---------------------------------------------------------------- 基础工具
def http_get(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "sqli-check/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def snippet(body):
    return body[:EVIDENCE_SNIPPET_LEN]


class BlackHole:
    """本机黑洞 socket：接受连接但永不响应，用于确定性地模拟载荷超时。"""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]

    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 载荷执行
def run_payload(base_url, payload, timeout, hang_base_url=None):
    target = hang_base_url or base_url
    result = {
        "id": payload["id"],
        "type": payload["type"],
        "description": payload["description"],
        "status": None,      # 检出 / 未检出 / 未完成
        "evidence": None,    # 响应片段
        "detail": None,
    }
    try:
        if payload["type"] == "union":
            body = http_get(target + "/login?" + urlencode(payload["params"]), timeout)
            hits = [m for m in payload["markers"] if m in body]
            result["evidence"] = snippet(body)
            if hits:
                result["status"] = "检出"
                result["detail"] = "响应包含未登录用户的敏感数据标记: " + ", ".join(hits)
            else:
                result["status"] = "未检出"
                result["detail"] = "响应未包含敏感数据标记"
        else:  # boolean
            true_body = http_get(
                target + "/login?" + urlencode(payload["true_params"]), timeout
            )
            false_body = http_get(
                target + "/login?" + urlencode(payload["false_params"]), timeout
            )
            result["evidence"] = "真条件响应: %s || 假条件响应: %s" % (
                snippet(true_body),
                snippet(false_body),
            )
            if "登录成功" in true_body and "登录失败" in false_body:
                result["status"] = "检出"
                result["detail"] = "真/假条件响应存在稳定差异，可逐位推断数据"
            else:
                result["status"] = "未检出"
                result["detail"] = "真/假条件响应无差异"
    except Exception as exc:  # 超时、连接错误等一律标注为未完成，不影响其他载荷
        result["status"] = "未完成"
        result["evidence"] = None
        result["detail"] = "请求未完成: %s: %s" % (type(exc).__name__, exc)
    return result


# ---------------------------------------------------------------- 服务管理
def launch_service(command, port):
    real_cmd = command.replace("{port}", str(port))
    creationflags = (
        subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    )
    proc = subprocess.Popen(
        real_cmd,
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    return proc, real_cmd


def wait_ready(port, timeout, proc):
    deadline = time.monotonic() + timeout
    url = "http://127.0.0.1:%d/health" % port
    last_err = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False, "服务进程已退出，退出码 %s（启动命令可能无效）" % proc.returncode
        try:
            http_get(url, 1.0)
            return True, None
        except Exception as exc:
            last_err = exc
            time.sleep(0.3)
    return False, "在 %.1f 秒内未就绪（最后错误: %s）" % (timeout, last_err)


def cleanup_service(proc, simulate_failure=False):
    cleanup = {"warnings": [], "residual_pids": []}
    if proc is None:
        return cleanup
    try:
        if simulate_failure:
            raise RuntimeError("模拟的清理失败（测试钩子 --simulate-cleanup-failure）")
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=15,
            )
        else:
            proc.kill()
        proc.wait(timeout=10)
    except Exception as exc:
        cleanup["warnings"].append(
            "清理服务进程失败: %s: %s" % (type(exc).__name__, exc)
        )
        cleanup["residual_pids"].append(proc.pid)
        # 记录警告后仍尽力清理，避免真实残留
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=15,
                )
            else:
                proc.kill()
        except Exception:
            pass
    return cleanup


# ---------------------------------------------------------------- 单版本流程
def run_version(label, command, port, args):
    if not command:
        return None
    result = {
        "label": label,
        "command": command,
        "status": None,     # 可用 / 不可用
        "reason": None,
        "verdict": None,    # 存在漏洞 / 未检出漏洞
        "payloads": None,
        "cleanup": {"warnings": [], "residual_pids": []},
    }
    proc = None
    blackhole = None
    try:
        try:
            proc, real_cmd = launch_service(command, port)
            result["command"] = real_cmd
        except Exception as exc:
            result["status"] = "不可用"
            result["reason"] = "启动命令执行失败: %s" % exc
            return result
        ready, reason = wait_ready(port, args.ready_timeout, proc)
        if not ready:
            result["status"] = "不可用"
            result["reason"] = reason
            return result
        result["status"] = "可用"
        if args.simulate_timeout_payload:
            blackhole = BlackHole()
        base_url = "http://127.0.0.1:%d" % port
        payload_results = []
        for p in PAYLOADS:
            hang = (
                blackhole.url()
                if (blackhole and p["id"] == args.simulate_timeout_payload)
                else None
            )
            payload_results.append(run_payload(base_url, p, args.request_timeout, hang))
        result["payloads"] = payload_results
        result["verdict"] = (
            "存在漏洞"
            if any(r["status"] == "检出" for r in payload_results)
            else "未检出漏洞"
        )
        return result
    finally:
        if blackhole:
            blackhole.close()
        result["cleanup"] = cleanup_service(proc, args.simulate_cleanup_failure)


# ---------------------------------------------------------------- 汇总
def build_comparison(vuln_res, fixed_res):
    if vuln_res is None and fixed_res is None:
        return None
    v = vuln_res.get("verdict") if vuln_res else None
    f = fixed_res.get("verdict") if fixed_res else None
    fixed_incomplete = bool(
        fixed_res
        and fixed_res.get("payloads")
        and any(p["status"] == "未完成" for p in fixed_res["payloads"])
    )
    if v == "存在漏洞" and f == "未检出漏洞" and not fixed_incomplete:
        effective = "是"
        summary = "漏洞版检出 SQL 注入，修复版全部载荷均未检出，修复有效。"
    elif v == "存在漏洞" and f == "存在漏洞":
        effective = "否"
        summary = "修复版仍检出 SQL 注入，修复无效。"
    elif v == "存在漏洞" and f == "未检出漏洞" and fixed_incomplete:
        effective = None
        summary = "修复版存在未完成的载荷，无法确认其通过全部检测，修复有效性待复核。"
    else:
        effective = None
        summary = "版本结果不完整（未执行或不可用），无法得出修复有效性结论。"
    return {
        "vulnerable_version_verdict": v,
        "fixed_version_verdict": f,
        "fix_effective": effective,
        "summary": summary,
    }


def build_evidence(vuln_res, fixed_res):
    evidence = []
    for key, res in (("vulnerable_version", vuln_res), ("fixed_version", fixed_res)):
        if not res or not res.get("payloads"):
            continue
        for p in res["payloads"]:
            evidence.append(
                {
                    "version": key,
                    "payload_id": p["id"],
                    "type": p["type"],
                    "status": p["status"],
                    "evidence": p["evidence"],
                    "detail": p["detail"],
                }
            )
    return evidence or None


# ---------------------------------------------------------------- 入口
def build_parser():
    parser = argparse.ArgumentParser(
        prog="sqli_check.py",
        description="离线 SQL 注入检测与修复验证工具（仅针对本地自建样本服务）",
    )
    parser.add_argument("--vuln-cmd", help="漏洞版服务启动命令，{port} 会被替换为实际端口")
    parser.add_argument("--fixed-cmd", help="修复版服务启动命令，{port} 会被替换为实际端口")
    parser.add_argument("--report", default="report.json", help="报告输出路径（默认 report.json）")
    parser.add_argument("--vuln-port", type=int, default=DEFAULT_VULN_PORT, help="漏洞版端口")
    parser.add_argument("--fixed-port", type=int, default=DEFAULT_FIXED_PORT, help="修复版端口")
    parser.add_argument("--ready-timeout", type=float, default=15.0, help="服务就绪等待秒数")
    parser.add_argument("--request-timeout", type=float, default=5.0, help="单次检测请求超时秒数")
    parser.add_argument(
        "--simulate-timeout-payload",
        metavar="PAYLOAD_ID",
        help="测试钩子：使指定载荷请求超时，验证未完成标注",
    )
    parser.add_argument(
        "--simulate-cleanup-failure",
        action="store_true",
        help="测试钩子：模拟清理失败，验证报告警告与残留标识",
    )
    return parser


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = build_parser().parse_args(argv)

    vuln_res = run_version("vulnerable", args.vuln_cmd, args.vuln_port, args)
    fixed_res = run_version("fixed", args.fixed_cmd, args.fixed_port, args)

    report = {
        "tool": "sqli_check",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "vulnerable_version": vuln_res,
        "fixed_version": fixed_res,
        "comparison_summary": build_comparison(vuln_res, fixed_res),
        "evidence": build_evidence(vuln_res, fixed_res),
    }
    with open(args.report, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    print("报告已写入: %s" % args.report)
    for name, res in (("漏洞版", vuln_res), ("修复版", fixed_res)):
        if res is None:
            print("  %s: 未执行 (null)" % name)
        elif res["status"] != "可用":
            print("  %s: 不可用 - %s" % (name, res["reason"]))
        else:
            print("  %s: %s" % (name, res["verdict"]))
    cmp_ = report["comparison_summary"]
    if cmp_:
        print("  修复有效: %s (%s)" % (cmp_["fix_effective"], cmp_["summary"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
