#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""内部工单系统 - 附件预览接口越权读取检测脚本（仅限本地实例）

用法：
    python detect.py
    python detect.py --targets http://127.0.0.1:5000:vulnerable http://127.0.0.1:5001:fixed
    python detect.py --out reports/report.json --log logs/detect.log

行为：
  * 只允许访问 127.0.0.1 / localhost / ::1 上的实例，其他地址直接拒绝执行；
  * 对每个实例依次发送 正常附件名、目录穿越附件名、绝对路径 三类预览请求；
  * 单个实例不可达不影响其余实例，结论记为“未完成”；
  * 每个请求的参数、状态码、响应摘要逐行写入 detect.log；
  * 最终输出 JSON 报告，包含实例地址、请求参数、判定结论与修复建议。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
ATTACHMENT_DIR = (PROJECT_ROOT / "attachments").resolve()
OUTSIDE_DIR = (PROJECT_ROOT / "outside_attachments").resolve()
MARKER_PATH = OUTSIDE_DIR / "secret_marker.txt"
MARKER_TOKEN = "P66-LOCAL-MARKER-A7F3C9"
NORMAL_TOKEN = "P66-HANDBOOK-2026"

ENDPOINT_PATH = "/api/attachment/preview"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
TIMEOUT_SECONDS = 3
SNIPPET_LENGTH = 160

VERDICT_RISK = "风险"
VERDICT_FIXED = "已修复"
VERDICT_INCOMPLETE = "未完成"

REPAIR_RECOMMENDATION = (
    "附件预览参数不得直接用于拼接文件路径。应先将客户端参数做路径规范化"
    "（如 os.path.realpath / pathlib.Path.resolve），得到最终绝对路径，再校验"
    "该规范化路径确实位于附件目录内（如 candidate.relative_to(attachment_root)），"
    "不满足即返回 403 拒绝；同时拒绝绝对路径、../ 与反斜杠变体、空字节等输入，"
    "并改用文件 ID 映射或附件白名单寻址，避免客户端可控路径片段直接落到文件系统。"
)


def build_parameters() -> list[dict[str, str]]:
    """三类请求参数：正常附件名、目录穿越、绝对路径。"""
    windows_abs = str(MARKER_PATH)
    forward_slash_abs = str(MARKER_PATH).replace("\\", "/")
    return [
        {
            "param_class": "normal",
            "name": "员工手册-2026.txt",
            "description": "附件目录内的正常附件名",
        },
        {
            "param_class": "traversal",
            "name": "../outside_attachments/secret_marker.txt",
            "description": "使用 ../ 跳出附件目录",
        },
        {
            "param_class": "traversal",
            "name": "..\\outside_attachments\\secret_marker.txt",
            "description": "使用 Windows 反斜杠跳出附件目录",
        },
        {
            "param_class": "absolute",
            "name": windows_abs,
            "description": "Windows 绝对路径直接指向附件目录外文件",
        },
        {
            "param_class": "absolute",
            "name": forward_slash_abs,
            "description": "正斜杠形式的绝对路径指向附件目录外文件",
        },
    ]


@dataclass
class Target:
    address: str
    variant: str
    host: str
    reachable: bool = False
    unreachable_reason: str | None = None
    verdict: str = VERDICT_INCOMPLETE

    @property
    def label(self) -> str:
        return f"{self.address} ({self.variant})"


TARGET_PATTERN = re.compile(
    r"^(?P<base>https?://[^/:]+(?::\d+)?)(?::(?P<variant>[A-Za-z0-9_-]+))?/?$"
)


def parse_target(raw: str) -> Target:
    """解析 http://127.0.0.1:5000:vulnerable 形式的实例描述。"""
    match = TARGET_PATTERN.match(raw.strip())
    if not match:
        raise ValueError(f"实例地址格式应为 http://127.0.0.1:PORT:variant：{raw}")
    address = match.group("base")
    variant = match.group("variant") or "unknown"
    # urlsplit 在 host:port 之后出现额外冒号时会把整段塞进 netloc，
    # 所以先按上面的正则剥离变体名，再解析主机。
    parsed = urllib.parse.urlsplit(address)
    host = parsed.hostname or ""
    if not host:
        raise ValueError(f"实例地址缺少主机名：{raw}")
    if host.lower() not in ALLOWED_HOSTS:
        raise ValueError(f"拒绝访问非本地实例：{raw}（仅允许 127.0.0.1/localhost）")
    return Target(address=address.rstrip("/"), variant=variant, host=host)


def http_get(url: str) -> tuple[int | None, str, str | None]:
    """返回 (状态码, 响应体文本, 错误描述)。HTTP 4xx/5xx 仍返回状态码。"""
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "p66-local-probe/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read()
            return response.status, decode_body(body), None
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp is not None else b""
        return exc.code, decode_body(body), None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            return None, "", "请求超时（timeout）"
        if isinstance(reason, ConnectionRefusedError):
            return None, "", "连接被拒绝（ConnectionRefusedError）"
        return None, "", f"连接失败（URLError）：{reason}"
    except TimeoutError:
        return None, "", "请求超时（timeout）"
    except OSError as exc:
        return None, "", f"网络错误：{exc}"


def decode_body(body: bytes) -> str:
    if not body:
        return ""
    try:
        return body.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - 理论不可达
        return repr(body)


def summarize(text: str, limit: int = SNIPPET_LENGTH) -> str:
    compact = " / ".join(line.strip() for line in text.splitlines() if line.strip())
    if len(compact) > limit:
        compact = compact[:limit] + "…"
    return compact


def run_target(target: Target, parameters: list[dict[str, str]], log_lines: list[str]) -> list[dict[str, Any]]:
    """对单个实例逐条发送请求；实例不可达时仍为每条参数生成“未完成”记录。"""
    results: list[dict[str, Any]] = []

    for parameter in parameters:
        query = urllib.parse.urlencode({"name": parameter["name"]})
        url = f"{target.address}{ENDPOINT_PATH}?{query}"
        status, body, error = http_get(url)

        marker_leaked = False
        snippet = ""
        if error is not None:
            target.reachable = False
            target.unreachable_reason = error
            target.verdict = VERDICT_INCOMPLETE
            conclusion = f"未执行：实例不可达（{error}）"
        else:
            target.reachable = True
            snippet = summarize(body)
            marker_leaked = status == 200 and MARKER_TOKEN in body
            normal_ok = (
                parameter["param_class"] == "normal"
                and status == 200
                and NORMAL_TOKEN in body
            )

            if parameter["param_class"] == "normal":
                conclusion = "正常附件可预览（基线请求）" if normal_ok else "正常附件预览异常"
            elif marker_leaked:
                conclusion = "越界读取成功，响应包含附件目录外标记文件内容"
                target.verdict = VERDICT_RISK
            elif status in (400, 403, 404):
                conclusion = "请求被拒绝，响应不含目录外内容"
                if target.verdict != VERDICT_RISK:
                    target.verdict = VERDICT_FIXED
            elif status == 200:
                # 200 但未携带标记内容：本次未泄露，但不能据此判定响应被拒绝。
                conclusion = "响应 200 但未包含目录外标记文件内容"
                if target.verdict != VERDICT_RISK:
                    target.verdict = VERDICT_FIXED
            else:
                conclusion = f"响应异常（状态码 {status}），未得出风险或已修复结论"

        log_lines.append(
            f"{datetime.now().isoformat(timespec='seconds')} | instance={target.address} | "
            f"variant={target.variant} | param_class={parameter['param_class']} | "
            f"name={parameter['name']} | status={status if error is None else 'unreachable'} | "
            f"response: {snippet if error is None else error}"
        )

        evidence = ""
        if error is None:
            if status == 200 and MARKER_TOKEN in body:
                evidence = (
                    f"状态码 {status}，响应体包含目录外标记文件内容片段："
                    f"“{summarize(body, 80)}”"
                )
            elif parameter["param_class"] == "normal":
                evidence = f"状态码 {status}，响应片段：“{summarize(body, 80)}”"
            else:
                evidence = f"状态码 {status}，响应体未包含标记文件内容 MARKER_TOKEN；片段：“{summarize(body, 80)}”"
        else:
            evidence = f"请求未完成：{error}（{url}）"

        results.append(
            {
                "instance": target.address,
                "variant": target.variant,
                "param_class": parameter["param_class"],
                "parameter": parameter["name"],
                "parameter_description": parameter["description"],
                "request_url": url,
                "status_code": status,
                "response_snippet": snippet,
                "marker_leaked": bool(status == 200 and MARKER_TOKEN in body) if error is None else False,
                "request_conclusion": conclusion,
                "evidence": evidence,
                # 结论随实例：风险 / 已修复 / 未完成（不可达实例固定为未完成）。
                "verdict": VERDICT_INCOMPLETE if error is not None else target.verdict,
            }
        )

        if error is not None:
            # 实例不可达：其余参数无需再尝试发送，但仍为每条参数记录未完成。
            for rest in parameters[parameters.index(parameter) + 1:]:
                rest_query = urllib.parse.urlencode({"name": rest["name"]})
                rest_url = f"{target.address}{ENDPOINT_PATH}?{rest_query}"
                log_lines.append(
                    f"{datetime.now().isoformat(timespec='seconds')} | instance={target.address} | "
                    f"variant={target.variant} | param_class={rest['param_class']} | "
                    f"name={rest['name']} | status=unreachable | response={error}"
                )
                results.append(
                    {
                        "instance": target.address,
                        "variant": target.variant,
                        "param_class": rest["param_class"],
                        "parameter": rest["name"],
                        "parameter_description": rest["description"],
                        "request_url": rest_url,
                        "status_code": None,
                        "response_snippet": "",
                        "marker_leaked": False,
                        "request_conclusion": f"未执行：实例不可达（{error}）",
                        "evidence": f"请求未完成：{error}（{rest_url}）",
                        "verdict": VERDICT_INCOMPLETE,
                    }
                )
            break

    return results


def finalize_verdict(target: Target, results: list[dict[str, Any]]) -> str:
    """根据请求结果确定实例级结论（风险 / 已修复 / 未完成）。"""
    if not target.reachable:
        return VERDICT_INCOMPLETE
    if target.verdict == VERDICT_RISK:
        return VERDICT_RISK
    if target.verdict == VERDICT_FIXED:
        return VERDICT_FIXED
    return VERDICT_INCOMPLETE


def build_report(
    targets: list[Target], results: list[dict[str, Any]]
) -> dict[str, Any]:
    instances = []
    for target in targets:
        verdict = finalize_verdict(target, results)
        # 回填实例结论，保证每条记录与实例结论一致。
        for row in results:
            if row["instance"] == target.address and target.reachable:
                row["verdict"] = verdict
            elif row["instance"] == target.address:
                row["verdict"] = VERDICT_INCOMPLETE
        instances.append(
            {
                "address": target.address,
                "variant": target.variant,
                "reachable": target.reachable,
                "verdict": verdict,
                "unreachable_reason": target.unreachable_reason,
                "request_count": sum(1 for row in results if row["instance"] == target.address),
                "risk_evidence_count": sum(
                    1
                    for row in results
                    if row["instance"] == target.address and row["marker_leaked"]
                ),
                "summary": (
                    "实例不可达，未完成检测"
                    if not target.reachable
                    else (
                        "存在目录穿越/绝对路径越权读取，可读取附件目录外文件"
                        if verdict == VERDICT_RISK
                        else "越界请求被拒绝或未泄露目录外内容"
                        if verdict == VERDICT_FIXED
                        else "检测未能得出风险或已修复结论"
                    )
                ),
            }
        )

    unreachable = [
        {
            "address": item["address"],
            "variant": item["variant"],
            "reason": item["unreachable_reason"],
        }
        for item in instances
        if not item["reachable"]
    ]

    return {
        "report_title": "内部工单系统附件预览接口越权读取核验报告（本地实例）",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scope": {
            "mode": "local-only",
            "allowed_hosts": sorted(ALLOWED_HOSTS),
            "endpoint": ENDPOINT_PATH,
            "attachment_dir": str(ATTACHMENT_DIR),
            "outside_marker_file": str(MARKER_PATH),
            "marker_token": MARKER_TOKEN,
            "parameter_classes": ["normal", "traversal", "absolute"],
        },
        "instances": instances,
        "unreachable_instances": unreachable,
        "results": results,
        "repair_recommendation": REPAIR_RECOMMENDATION,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="工单系统附件预览接口越权读取检测（本地）")
    parser.add_argument(
        "--targets",
        nargs="+",
        default=[
            "http://127.0.0.1:5000:vulnerable",
            "http://127.0.0.1:5001:fixed",
        ],
        help="实例地址，格式 http://127.0.0.1:PORT:variant",
    )
    parser.add_argument("--out", default=str(PROJECT_ROOT / "reports" / "report.json"))
    parser.add_argument("--log", default=str(PROJECT_ROOT / "logs" / "detect.log"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        targets = [parse_target(raw) for raw in args.targets]
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2

    parameters = build_parameters()
    log_lines: list[str] = [
        "# 工单系统附件预览越权读取检测日志（仅本地实例）",
        f"# 开始时间 {datetime.now().isoformat(timespec='seconds')}",
        f"# 格式：时间 | instance | variant | param_class | name | status | response",
    ]
    all_results: list[dict[str, Any]] = []
    for target in targets:
        all_results.extend(run_target(target, parameters, log_lines))

    report = build_report(targets, all_results)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_lines.append(f"# 结束时间 {datetime.now().isoformat(timespec='seconds')}")
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    print(f"[+] 报告已写入：{out_path}")
    print(f"[+] 日志已写入：{log_path}")
    for instance in report["instances"]:
        print(
            f"    - {instance['address']} ({instance['variant']}): "
            f"{instance['verdict']}"
            + (f"（不可达：{instance['unreachable_reason']}）" if not instance['reachable'] else "")
        )
    if report["unreachable_instances"]:
        print("[!] 不可达实例：")
        for item in report["unreachable_instances"]:
            print(f"    - {item['address']}（{item['variant']}）：{item['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
