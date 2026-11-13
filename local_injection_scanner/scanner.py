"""扫描编排：逐参数独立探测、服务等待、报告组装。

关键设计：
  * 每个参数独立 try/except，单个参数请求出错不中断其他参数（验收第 6 条）；
  * 服务整体不可达时不产生任何"安全"判定（验收第 5 条）；
  * 仅当响应命中明确注入特征时才生成缺陷条目（验收第 10 条）。
"""

from __future__ import annotations

import time
from datetime import datetime
from urllib.parse import urlparse

from .detector import classify_response
from .http_client import LocalHttpClient, build_url
from .logger import TimestampLogger
from .payloads import BASELINE_VALUE, PAYLOADS

QUERY_PATH = "/query"


def wait_for_service(client: LocalHttpClient, target: str, wait_seconds: float,
                     logger: TimestampLogger) -> bool:
    """在 wait_seconds 内轮询目标，服务恢复后立即返回 True；超时返回 False。"""
    deadline = time.monotonic() + wait_seconds
    probe_url = build_url(target, QUERY_PATH, {"__probe": "1"})
    attempt = 0
    while True:
        attempt += 1
        resp = client.get(probe_url, param="__probe",
                          payload_category="reachability", payload="__probe=1")
        if resp.error is None:
            logger.info(f"目标服务已就绪（第 {attempt} 次探测成功，HTTP {resp.status_code}）")
            return True
        if time.monotonic() >= deadline:
            logger.warning(f"等待 {wait_seconds:.0f} 秒后目标仍不可达（共探测 {attempt} 次）")
            return False
        logger.info(f"目标暂不可用，1 秒后重试（{attempt}/{int(max(wait_seconds, 1))}）")
        time.sleep(1)


def _scan_one_param(client: LocalHttpClient, target: str, param: str,
                    logger: TimestampLogger) -> dict:
    """对单个参数完成基准请求 + 全部载荷探测；任何异常都被隔离在本参数内。"""
    result = {
        "parameter": param,
        "status": "safe",          # safe | vulnerable | error
        "injection_types": [],
        "findings": [],
        "request_errors": 0,
    }
    try:
        # 1) 基准请求：记录该参数的正常响应
        baseline_url = build_url(target, QUERY_PATH, {param: BASELINE_VALUE})
        baseline = client.get(baseline_url, param=param,
                              payload_category="baseline", payload=BASELINE_VALUE)
        if baseline.error is not None and baseline.status_code == 0:
            result["status"] = "error"
            result["request_errors"] += 1
            logger.warning(f"参数 {param!r} 基准请求即失败: {baseline.error}")
            return result
        logger.info(f"参数 {param!r} 基准响应 HTTP {baseline.status_code}")

        # 2) 逐载荷探测
        seen_types: set[str] = set()
        for payload, category, injection_type in PAYLOADS:
            url = build_url(target, QUERY_PATH, {param: payload})
            resp = client.get(url, param=param,
                              payload_category=category, payload=payload)
            if resp.error is not None and resp.status_code == 0:
                # 网络级失败：计错但继续下一个载荷
                result["request_errors"] += 1
                logger.warning(f"参数 {param!r} 载荷 {payload!r} 请求失败: {resp.error}")
                continue

            detection = classify_response(
                payload, resp.body, resp.status_code, False,
                payload_category=category, fallback_type=injection_type,
            )
            if detection.request_error and not detection.is_vulnerable:
                result["request_errors"] += 1
                logger.warning(
                    f"参数 {param!r} 载荷 {payload!r} 返回 HTTP {resp.status_code}（请求错误，不计缺陷）"
                )
                continue

            if detection.is_vulnerable:
                result["status"] = "vulnerable"
                seen_types.add(detection.injection_type or injection_type)
                result["findings"].append({
                    "parameter": param,
                    "injection_type": detection.injection_type or injection_type,
                    "payload_category": category,
                    "payload": payload,
                    "request_url": url,
                    "status_code": detection.status_code,
                    "evidence": detection.evidence,
                    "evidence_length": len(detection.evidence),
                })
                logger.info(
                    f"参数 {param!r} 命中缺陷 [{detection.injection_type}]，载荷={payload!r}"
                )

        result["injection_types"] = sorted(seen_types)
        if result["status"] == "safe" and result["request_errors"] > 0 and not result["findings"]:
            # 有请求错误但未发现缺陷：标注 error，避免误判为"已确认安全"
            result["status"] = "error"
        return result
    except Exception as exc:  # 隔离：单参数任何意外都不影响其他参数
        result["status"] = "error"
        result["request_errors"] += 1
        logger.error(f"参数 {param!r} 检测过程发生异常，已跳过: {exc!r}")
        return result


def scan_target(target: str, parameters: list[str], wait_seconds: float,
                logger: TimestampLogger) -> dict:
    """执行完整扫描并返回报告字典。"""
    parsed = urlparse(target)
    client = LocalHttpClient()

    report: dict = {
        "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "target": target,
        "target_host": parsed.hostname,
        "query_path": QUERY_PATH,
        "parameters_tested": list(parameters),
        "service_reachable": False,
        "conclusion": "",
        "summary": {},
        "findings": [],
        "parameter_results": [],
        "request_log": [],
    }

    # 1) 服务可达性（含可选延迟等待）
    reachable = wait_for_service(client, target, wait_seconds, logger)
    if not reachable:
        report["service_reachable"] = False
        report["conclusion"] = "服务不可达，无法完成检测（未对任何参数作出安全判定）"
        report["summary"] = {
            "parameters_tested_count": len(parameters),
            "vulnerable_parameters_count": 0,
            "findings_count": 0,
            "safe_parameters": [],
            "errored_parameters": list(parameters),
        }
        report["request_log"] = [vars(r) for r in client.records]
        return report

    report["service_reachable"] = True

    # 2) 逐参数独立检测
    for param in parameters:
        logger.info(f"开始检测参数 {param!r}（共 {len(PAYLOADS)} 个载荷）")
        param_result = _scan_one_param(client, target, param, logger)
        report["parameter_results"].append(param_result)
        report["findings"].extend(param_result["findings"])

    # 3) 汇总结论（不可达分支以外，safe 判定只在这里产生）
    vulnerable_params = [r["parameter"] for r in report["parameter_results"]
                         if r["status"] == "vulnerable"]
    safe_params = [r["parameter"] for r in report["parameter_results"]
                   if r["status"] == "safe"]
    errored_params = [r["parameter"] for r in report["parameter_results"]
                      if r["status"] == "error"]

    if report["findings"]:
        report["conclusion"] = f"发现缺陷：参数 {', '.join(vulnerable_params)} 存在注入缺陷"
    else:
        report["conclusion"] = "未发现缺陷：所有已成功检测的参数均未出现注入成功响应"

    report["summary"] = {
        "parameters_tested_count": len(parameters),
        "vulnerable_parameters_count": len(vulnerable_params),
        "findings_count": len(report["findings"]),
        "safe_parameters": safe_params,
        "errored_parameters": errored_params,
    }
    report["request_log"] = [vars(r) for r in client.records]
    return report
