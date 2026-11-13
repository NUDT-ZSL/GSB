"""命令行入口。

典型用法：
  python -m order_sec_check.cli --cases samples/cases/order_cases.json \
      --report-dir reports --log-dir logs
  python -m order_sec_check.cli --cases ... --resume reports/detection_xxx.json
  python -m order_sec_check.cli --cases ... --compare-baseline reports/detection_xxx.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from . import report as report_mod
from .config import CaseValidationError, load_case_file
from . import redact
from .detector import (
    HttpResult,
    RuleVerdict,
    TargetUnreachable,
    evaluate_rules,
    send_request,
)
from .logging_setup import setup_logger
from .netpolicy import PolicyError, assert_local_url
from .validate import ParamValidationError, check_param_rules

# 退出码约定
EXIT_OK = 0
EXIT_CASE_FILE_INVALID = 2
EXIT_POLICY_DENIED = 3
EXIT_BASELINE_INVALID = 4


def _collect_secrets(data: dict[str, Any]) -> list[str]:
    secrets: list[str] = []
    for case in data["cases"]:
        for name, value in case["request"].get("headers", {}).items():
            from .redact import is_sensitive_header

            if is_sensitive_header(name) and value:
                secrets.append(value)
    return secrets


def _resolve_base_url(data: dict[str, Any], case: dict[str, Any], override: str | None) -> str:
    if override:
        return override.rstrip("/")
    if case.get("base_url"):
        return case["base_url"].rstrip("/")
    return data["target"]["base_url"].rstrip("/")


def _check_all_targets_local(data: dict[str, Any], override: str | None) -> None:
    """发请求前对所有目标地址统一做本地策略校验，出现外部主机整体拒绝。"""
    targets = {data["target"]["base_url"]}
    for case in data["cases"]:
        if case.get("base_url"):
            targets.add(case["base_url"])
    if override:
        targets.add(override)
    for url in targets:
        assert_local_url(url)


def _base_entry(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "type": case["type"],
        "description": case["description"],
        "status": None,
        "hit": None,
        "request_summary": None,
        "response_summary": None,
        "rule_detail": None,
        "reject_reasons": None,
        "error": None,
    }


def _run_one_case(
    case: dict[str, Any],
    base_url: str,
    secrets: list[str],
    logger,
) -> dict[str, Any]:
    entry = _base_entry(case)
    request_spec = case["request"]

    # 1) 业务参数合规校验：不通过直接拒绝，不发起请求
    if case.get("param_rules"):
        try:
            check_param_rules(request_spec.get("params", {}), case["param_rules"])
        except ParamValidationError as exc:
            entry["status"] = "rejected"
            entry["hit"] = False
            entry["reject_reasons"] = exc.reasons
            logger.warning(
                "用例 %s 参数业务合规校验未通过，已拒绝执行（不发起请求）：%s",
                case["case_id"],
                "；".join(exc.reasons),
            )
            return entry

    # 2) 发起检测请求
    safe_headers = redact.redact_headers(request_spec.get("headers", {}))
    logger.info(
        "用例 %s（%s）开始检测 -> %s%s，请求头(脱敏)=%s",
        case["case_id"], case["type"], base_url, request_spec["path"], safe_headers,
    )
    try:
        result: HttpResult = send_request(base_url, request_spec, secrets)
    except TargetUnreachable as exc:
        entry["status"] = "incomplete"
        entry["error"] = f"目标暂时不可达：{exc}"
        logger.error("用例 %s 目标暂时不可达，保留待重试：%s", case["case_id"], exc)
        return entry

    # 3) 规则判定
    verdict: RuleVerdict = evaluate_rules(result, case["rules"])
    entry["status"] = "hit" if verdict.hit else "miss"
    entry["hit"] = verdict.hit
    entry["request_summary"] = result.request_summary
    entry["response_summary"] = result.response_summary
    entry["rule_detail"] = {
        "matched_rule": verdict.matched_rule,
        "rule_results": verdict.rule_results,
    }
    logger.info(
        "用例 %s 检测完成：%s（HTTP %s）",
        case["case_id"],
        "命中" if verdict.hit else "未命中",
        result.status_code,
    )
    return entry


def _baseline_index(baseline: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["case_id"]: e for e in baseline.get("results", [])}


def _functional_compare(
    case: dict[str, Any],
    old: dict[str, Any],
    new: dict[str, Any],
    logger,
) -> dict[str, Any]:
    """正常订单查询的功能回退判据：修复前后响应体应一致。"""
    ignore = set(case.get("compare_ignore_fields", []))

    def _body(entry: dict[str, Any]) -> Any:
        summary = entry.get("response_summary") or {}
        return summary.get("body")

    old_body, new_body = _body(old), _body(new)
    consistent = old_body == new_body
    detail = "修复前后正常查询响应一致"
    if not consistent and ignore:
        # 对声明的易变字段做豁免后再比一次
        import json

        try:
            old_json = json.loads(old_body) if isinstance(old_body, str) else old_body
            new_json = json.loads(new_body) if isinstance(new_body, str) else new_body
            for field_name in ignore:
                for obj in (old_json, new_json):
                    if isinstance(obj, dict):
                        obj.pop(field_name, None)
            consistent = old_json == new_json
            if consistent:
                detail = f"忽略易变字段 {sorted(ignore)} 后响应一致"
        except (TypeError, ValueError):
            pass
    if not consistent:
        detail = "修复前后正常查询响应不一致，疑似功能回退"
        logger.error("用例 %s 功能回退：正常订单查询结果与基准不一致", case["case_id"])
    return {
        "case_id": case["case_id"],
        "type": case["type"],
        "regression": not consistent,
        "detail": detail,
        "baseline_response": old_body,
        "current_response": new_body,
    }


def _build_comparison_entry(
    case: dict[str, Any],
    old: dict[str, Any] | None,
    new: dict[str, Any],
) -> dict[str, Any]:
    entry = {
        "case_id": case["case_id"],
        "type": case["type"],
        "attack_intent": case.get("attack_intent", "attack"),
        "description": case["description"],
        "baseline_status": old.get("status") if old else None,
        "current_status": new["status"],
        "verdict": None,
        "request_summary": new.get("request_summary"),
        "response_summary": new.get("response_summary"),
        "reject_reasons": new.get("reject_reasons"),
        "error": new.get("error"),
    }
    if new["status"] == "incomplete":
        entry["verdict"] = "incomplete"
    elif new["status"] == "rejected":
        entry["verdict"] = "rejected"
    elif case["type"] == "business":
        # 业务用例本身无漏洞概念，功能一致性在 functional_regression_checks 中给出
        entry["verdict"] = "business_checked"
    elif old is None:
        entry["verdict"] = "baseline_missing"
    elif case.get("attack_intent") == "negative_control":
        # 阴性对照：修复前后都应维持既定结论（命中规则=正常业务可用）
        entry["verdict"] = (
            "still_clean" if new["status"] == old["status"] else "behavior_changed"
        )
    elif old["status"] == "hit":
        entry["verdict"] = "fixed" if new["status"] == "miss" else "unfixed"
    else:
        # 攻击用例基准本就未命中：保持干净则 still_clean，反之说明修复引入了新问题
        entry["verdict"] = "still_clean" if new["status"] == "miss" else "unfixed"
    return entry


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="订单查询接口安全自测工具（SQL 注入 / 越权），仅访问本地地址"
    )
    parser.add_argument("--cases", required=True, help="用例 JSON 文件路径")
    parser.add_argument("--report-dir", default="reports", help="报告输出目录")
    parser.add_argument("--log-dir", default="logs", help="日志输出目录")
    parser.add_argument(
        "--base-url",
        default=None,
        help="覆盖用例文件中的目标地址（仍须为本地回环地址）",
    )
    parser.add_argument(
        "--resume",
        default=None,
        help="基于已有检测报告，仅对其中未完成（暂时不可达）的用例再次检测",
    )
    parser.add_argument(
        "--compare-baseline",
        default=None,
        help="对比模式：给定修复前检测报告，对修复实例复用同一批用例并输出对比报告",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        data = load_case_file(args.cases)
    except CaseValidationError as exc:
        # 要求 1：字段级错误说明，终止且不发起任何请求
        print("用例文件校验失败，已终止且未发起任何请求：", file=sys.stderr)
        for item in exc.errors:
            print(f"  - {item}", file=sys.stderr)
        return EXIT_CASE_FILE_INVALID

    secrets = _collect_secrets(data)
    logger, log_path = setup_logger(Path(args.log_dir), secrets)
    logger.info("================ 安全检测开始 ================")
    logger.info("载入用例文件 %s，共 %d 条用例", args.cases, len(data["cases"]))

    # 要求 10：任何外部主机都在请求前被整体拒绝
    try:
        _check_all_targets_local(data, args.base_url)
    except PolicyError as exc:
        logger.error("本地访问策略校验失败，整体拒绝且不发起任何请求：%s", exc)
        print(f"已拒绝执行：{exc}", file=sys.stderr)
        return EXIT_POLICY_DENIED
    logger.info("本地地址策略校验通过，所有目标均为回环地址")

    report_dir = Path(args.report_dir)
    baseline: dict[str, Any] | None = None
    resume_ids: set[str] | None = None
    if args.resume:
        try:
            baseline = report_mod.load_report(args.resume)
        except (OSError, ValueError) as exc:
            logger.error("无法读取 resume 基准报告：%s", exc)
            return EXIT_BASELINE_INVALID
        resume_ids = {
            e["case_id"]
            for e in baseline.get("results", [])
            if e.get("status") == "incomplete"
        }
        logger.info("resume 模式：基准报告 %s，待重试用例 %s", args.resume, sorted(resume_ids))
        if not resume_ids:
            logger.info("基准报告中没有暂时不可达的用例，无需再次检测，进程结束")
            print("基准报告中没有未完成用例，无需 resume。")
            return EXIT_OK
    if args.compare_baseline:
        try:
            baseline = report_mod.load_report(args.compare_baseline)
        except (OSError, ValueError) as exc:
            logger.error("无法读取对比基准报告：%s", exc)
            return EXIT_BASELINE_INVALID
        logger.info("修复对比模式：基准报告 %s", args.compare_baseline)

    entries: list[dict[str, Any]] = []
    for case in data["cases"]:
        if resume_ids is not None:
            if case["case_id"] not in resume_ids:
                # resume 时直接沿用基准报告中的既有结论
                old = next(
                    e for e in baseline["results"] if e["case_id"] == case["case_id"]
                )
                entries.append(old)
                continue
        base_url = _resolve_base_url(data, case, args.base_url)
        entries.append(_run_one_case(case, base_url, secrets, logger))

    ordered = report_mod.sort_entries(entries)

    if args.compare_baseline:
        old_index = _baseline_index(baseline)
        new_index = {e["case_id"]: e for e in ordered}
        ordered_cases = report_mod.sort_entries(
            [{"case_id": c["case_id"]} for c in data["cases"]]
        )
        comparison_entries = [
            _build_comparison_entry(
                next(c for c in data["cases"] if c["case_id"] == item["case_id"]),
                old_index.get(item["case_id"]),
                new_index[item["case_id"]],
            )
            for item in ordered_cases
        ]
        functional_checks: list[dict[str, Any]] = []
        for case in [
            next(c for c in data["cases"] if c["case_id"] == item["case_id"])
            for item in ordered_cases
        ]:
            if case["type"] != "business":
                continue
            old = old_index.get(case["case_id"])
            new = next(e for e in ordered if e["case_id"] == case["case_id"])
            if old is None or new["status"] == "incomplete":
                functional_checks.append(
                    {
                        "case_id": case["case_id"],
                        "type": case["type"],
                        "regression": None,
                        "detail": "基准或当前结果不完整，无法比较",
                        "baseline_response": (old or {}).get("response_summary"),
                        "current_response": new.get("response_summary"),
                    }
                )
                continue
            functional_checks.append(_functional_compare(case, old, new, logger))

        payload = report_mod.build_comparison_report(
            target_base_url=args.base_url or data["target"]["base_url"],
            case_file=args.cases,
            baseline_report_path=args.compare_baseline,
            entries=comparison_entries,
            functional_checks=functional_checks,
        )
        out_path = report_mod.unique_report_path(report_dir, "comparison_report")
        report_mod.write_json(out_path, payload)
        logger.info("修复对比报告已写入：%s", out_path)
    else:
        payload = report_mod.build_detection_report(
            mode="resume" if args.resume else "detection",
            target_base_url=args.base_url or data["target"]["base_url"],
            case_file=args.cases,
            entries=ordered,
            resumed_from=args.resume,
        )
        out_path = report_mod.unique_report_path(report_dir, "detection_report")
        report_mod.write_json(out_path, payload)
        logger.info("检测报告已写入：%s", out_path)

    completed_count = (
        payload["summary"]["completed"]
        if "completed" in payload["summary"]
        else payload["summary"]["total"] - payload["summary"]["incomplete"]
    )
    logger.info(
        "================ 安全检测完成 ================"
        "（完成 %d / 未完成 %d / 报告 %s）",
        completed_count,
        payload["summary"]["incomplete"],
        out_path.name,
    )
    logger.info("日志文件：%s（凭据已脱敏，不含明文）", log_path)
    print(f"\n报告文件：{out_path}")
    print(f"日志文件：{log_path}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
