"""越权漏洞修复对比判定工具（仅使用 Python 标准库）。

工作流程：
  1. 加载并严格校验 scenarios.json（字段缺失 / 类型不符时列出全部错误，退出码 2，
     不执行任何场景、不写报告；空数组退出码 3）。
  2. 对每个场景分别向“未修复形态”和“已修复形态”发起相同请求，
     暂时性故障（连接失败 / 超时 / 502 / 503 / 504）按 --retries 自动重试。
  3. 以资源归属为准判定：非所有者访问他人资源时，修复后响应不得包含该资源的
     敏感字段，否则判定漏洞仍存在。
  4. 输出三类逐场景结论（fixed / vulnerable / cannot_determine），每条附依据，
     并汇总整体结论到 JSON 报告（默认 report.json）。

退出码：
  0 全部场景判定为已修复
  2 清单校验失败（缺字段 / 类型不符 / 重复 id / JSON 非法）
  3 清单为空数组
  4 未完全修复（存在漏洞仍存在或无法判定的场景）
  5 全部场景均无法判定
  1 其他运行期错误
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from urllib.parse import urlparse  # noqa: F401  (保留以便扩展)

try:  # 项目根在 sys.path 时
    from range.data import RangeData, load_data
except ImportError:  # 以绝对脚本路径启动时的兜底
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from range.data import RangeData, load_data  # type: ignore[no-redef]

TOOL_VERSION = "1.0.0"

# 三类逐场景结论
VERDICT_FIXED = "fixed"
VERDICT_VULNERABLE = "vulnerable"
VERDICT_CANNOT = "cannot_determine"
VERDICT_LABELS = {
    VERDICT_FIXED: "漏洞已修复",
    VERDICT_VULNERABLE: "漏洞仍存在",
    VERDICT_CANNOT: "无法判定",
}

# 整体结论
OVERALL_FIXED = "fully_fixed"
OVERALL_NOT_FIXED = "not_fully_fixed"
OVERALL_CANNOT = "cannot_determine"
OVERALL_LABELS = {
    OVERALL_FIXED: "完全修复",
    OVERALL_NOT_FIXED: "未完全修复",
    OVERALL_CANNOT: "无法形成整体判定",
}

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_INVALID_MANIFEST = 2
EXIT_EMPTY_MANIFEST = 3
EXIT_NOT_FULLY_FIXED = 4
EXIT_ALL_CANNOT = 5

RETRIABLE_STATUS = {502, 503, 504}
BODY_EXCERPT_LIMIT = 200


# --------------------------------------------------------------------------- #
# 控制台编码（Windows 中文环境兼容）
# --------------------------------------------------------------------------- #
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass


def _print(message: str) -> None:
    print(message)


# --------------------------------------------------------------------------- #
# 清单校验
# --------------------------------------------------------------------------- #
def validate_manifest(raw: Any) -> list[str]:
    """返回错误信息列表；空列表表示校验通过。"""
    errors: list[str] = []
    if not isinstance(raw, list):
        return [f"清单顶层必须是 JSON 数组，实际类型为 {type(raw).__name__}"]

    seen_ids: set[str] = set()
    required = ("id", "actor", "path", "query")
    for index, item in enumerate(raw):
        prefix = f"第 {index + 1} 条场景"
        if not isinstance(item, dict):
            errors.append(f"{prefix}：必须是 JSON 对象，实际类型为 {type(item).__name__}")
            continue
        for field in required:
            if field not in item:
                errors.append(f"{prefix}（id={item.get('id', '<未知>')}）：缺少字段 '{field}'")
        # 类型校验（字段存在才检查，避免重复报错）
        if "id" in item and not isinstance(item["id"], str):
            errors.append(f"{prefix}：字段 'id' 必须是字符串，实际为 {type(item['id']).__name__}")
        elif isinstance(item.get("id"), str) and not item["id"].strip():
            errors.append(f"{prefix}：字段 'id' 不能为空字符串")
        if "actor" in item and not isinstance(item["actor"], str):
            errors.append(
                f"{prefix}（id={item.get('id', '<未知>')}）：字段 'actor' 必须是字符串，"
                f"实际为 {type(item['actor']).__name__}"
            )
        elif isinstance(item.get("actor"), str) and not item["actor"].strip():
            errors.append(f"{prefix}（id={item.get('id', '<未知>')}）：字段 'actor' 不能为空字符串")
        if "path" in item:
            if not isinstance(item["path"], str):
                errors.append(
                    f"{prefix}（id={item.get('id', '<未知>')}）：字段 'path' 必须是字符串，"
                    f"实际为 {type(item['path']).__name__}"
                )
            elif not item["path"].startswith("/"):
                errors.append(
                    f"{prefix}（id={item.get('id', '<未知>')}）：字段 'path' 必须以 '/' 开头，"
                    f"实际值为 {item['path']!r}"
                )
        if "query" in item:
            q = item["query"]
            if not isinstance(q, dict):
                errors.append(
                    f"{prefix}（id={item.get('id', '<未知>')}）：字段 'query' 必须是对象，"
                    f"实际为 {type(q).__name__}"
                )
            else:
                for key, value in q.items():
                    if not isinstance(key, str):
                        errors.append(
                            f"{prefix}（id={item.get('id', '<未知>')}）：query 的键必须是字符串"
                        )
                    if not isinstance(value, str):
                        errors.append(
                            f"{prefix}（id={item.get('id', '<未知>')}）：query 参数 {key!r} 的值"
                            f"必须是字符串，实际为 {type(value).__name__}"
                        )
        sid = item.get("id")
        if isinstance(sid, str) and sid.strip():
            if sid in seen_ids:
                errors.append(f"{prefix}：场景 id {sid!r} 重复，id 必须唯一")
            seen_ids.add(sid)
    return errors


def load_manifest(path: Path) -> tuple[list[dict[str, Any]] | None, int | None]:
    """加载清单。成功返回 (清单, None)，失败返回 (None, 退出码)。"""
    if not path.exists():
        _print(f"[错误] 清单文件不存在: {path}")
        return None, EXIT_RUNTIME_ERROR
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        _print(f"[错误] 清单不是合法 JSON: {path}（第 {exc.lineno} 行第 {exc.colno} 列: {exc.msg}）")
        return None, EXIT_INVALID_MANIFEST
    except OSError as exc:
        _print(f"[错误] 读取清单失败: {path}（{exc}）")
        return None, EXIT_RUNTIME_ERROR

    if isinstance(raw, list) and len(raw) == 0:
        _print(f"[错误] 清单 {path} 为空数组：无场景可判定，不生成报告。")
        return None, EXIT_EMPTY_MANIFEST

    errors = validate_manifest(raw)
    if errors:
        _print(f"[错误] 清单校验失败，共 {len(errors)} 个问题，未执行任何场景：")
        for err in errors:
            _print(f"  - {err}")
        return None, EXIT_INVALID_MANIFEST
    return raw, None


# --------------------------------------------------------------------------- #
# HTTP 执行（含暂时性故障重试）
# --------------------------------------------------------------------------- #
def build_url(base_url: str, path: str, query: dict[str, str]) -> str:
    return base_url.rstrip("/") + path + "?" + urllib.parse.urlencode(query)


def invoke_once(
    url: str, token: str | None, timeout: float
) -> tuple[int | None, str, str | None]:
    """单次 HTTP 调用，返回 (状态码, 响应体文本, 错误信息)。"""
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Auth-Token"] = token
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.status, resp.read().decode(charset, errors="replace"), None
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 同样携带响应体，读取后交给判定逻辑
        try:
            charset = exc.headers.get_content_charset() or "utf-8"
            body = exc.read().decode(charset, errors="replace")
        except Exception:
            body = ""
        return exc.code, body, None
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        return None, "", f"连接/传输故障: {exc}"


def run_observation(
    form_name: str,
    base_url: str,
    scenario: dict[str, Any],
    token: str | None,
    retries: int,
    retry_delay: float,
    timeout: float,
) -> dict[str, Any]:
    """在一种形态上执行场景，暂时性故障自动重试，返回结构化观测结果。"""
    url = build_url(base_url, scenario["path"], scenario["query"])
    attempts_log: list[dict[str, Any]] = []
    last_status: int | None = None
    last_body = ""
    last_error: str | None = None

    total_attempts = retries + 1
    for attempt in range(1, total_attempts + 1):
        status, body, error = invoke_once(url, token, timeout)
        retriable = False
        if error is not None:
            retriable = True  # 连接失败 / 超时等网络层暂时性故障
            last_error = error
        elif status in RETRIABLE_STATUS:
            retriable = True  # 服务端暂时性状态码
            last_status, last_body, last_error = status, body, None
        else:
            last_status, last_body, last_error = status, body, None

        attempts_log.append(
            {
                "attempt": attempt,
                "status_code": status,
                "error": error,
                "retriable": retriable,
            }
        )
        if not retriable or attempt == total_attempts:
            break
        # 短暂退避后给该场景再次执行的机会（不丢弃场景）
        time.sleep(retry_delay)

    # 重试用尽后仍是可重试状态码（502/503/504）或网络故障，视为该形态不可用，
    # 绝不能把 503 错误体当作成功响应参与越权判定
    exhausted_retriable_status = (
        last_error is None and last_status is not None and last_status in RETRIABLE_STATUS
    )
    available = last_error is None and not exhausted_retriable_status
    return {
        "form": form_name,
        "base_url": base_url,
        "url": url,
        "available": available,
        "status_code": last_status,
        "body_excerpt": last_body[:BODY_EXCERPT_LIMIT],
        "attempts": len(attempts_log),
        "attempts_detail": attempts_log,
        "errors": (
            [last_error] if last_error
            else [f"重试用尽后仍返回可重试状态码 {last_status}"]
            if exhausted_retriable_status
            else []
        ),
    }


# --------------------------------------------------------------------------- #
# 敏感字段泄露检测
# --------------------------------------------------------------------------- #
def _walk_json(node: Any, keys: list[str], values: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            keys.append(str(key))
            _walk_json(value, keys, values)
    elif isinstance(node, list):
        for item in node:
            _walk_json(item, keys, values)
    elif node is not None:
        values.append(str(node))


def detect_leaks(body: str, sensitive_pairs: list[tuple[str, str]]) -> list[str]:
    """检测响应体中出现的敏感字段（按“字段名 + 字段值”证据判定）。"""
    if not body or not sensitive_pairs:
        return []
    found: list[str] = []
    try:
        parsed = json.loads(body)
        all_keys: list[str] = []
        all_values: list[str] = []
        _walk_json(parsed, all_keys, all_values)
        key_set = set(all_keys)
        value_set = set(all_values)
        for field, value in sensitive_pairs:
            if field in key_set and value in value_set:
                found.append(field)
    except (json.JSONDecodeError, TypeError):
        # 非 JSON 响应：退化为敏感值子串匹配
        for field, value in sensitive_pairs:
            if value and value in body:
                found.append(field)
    return found


# --------------------------------------------------------------------------- #
# 场景判定
# --------------------------------------------------------------------------- #
def resolve_target(
    scenario: dict[str, Any], data: RangeData
) -> tuple[str | None, dict[str, Any] | None, str | None, str | None]:
    """解析场景访问的目标对象，返回 (kind, target, owner_id, 解析错误)。"""
    path = scenario["path"]
    query = scenario["query"]
    if path == "/api/resource":
        resource_id = query.get("resource_id")
        if not resource_id:
            return None, None, None, "缺少 resource_id 查询参数，无法定位资源"
        target = data.resource_by_id(resource_id)
        if target is None:
            return None, None, None, f"资源 {resource_id} 不存在于靶场数据中，无法确认归属"
        return "resource", target, target["owner_id"], None
    if path == "/api/account":
        user_id = query.get("user_id")
        if not user_id:
            return None, None, None, "缺少 user_id 查询参数，无法定位账号"
        target = data.user_by_id(user_id)
        if target is None:
            return None, None, None, f"账号 {user_id} 不存在于靶场数据中，无法确认归属"
        return "account", target, target["id"], None
    return None, None, None, f"未知接口路径 {path}，判定工具仅识别 /api/resource 与 /api/account"


def judge(
    scenario: dict[str, Any],
    obs_vuln: dict[str, Any],
    obs_fixed: dict[str, Any],
    data: RangeData,
) -> dict[str, Any]:
    """以资源归属为准输出三类结论之一，并附人类可读依据。"""
    actor = scenario["actor"]
    kind, target, owner_id, resolve_error = resolve_target(scenario, data)

    ownership = {
        "actor": actor,
        "target_kind": kind,
        "target_id": target.get("id") if target else None,
        "target_owner": owner_id,
        "is_cross_owner_access": (owner_id is not None and owner_id != actor),
    }

    def cannot(reason: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "code": VERDICT_CANNOT,
            "label": VERDICT_LABELS[VERDICT_CANNOT],
            "reason": reason,
            "evidence": evidence or {},
        }

    # 目标无法解析：归属关系无法建立，禁止以空结果冒充成功
    if resolve_error:
        return cannot(f"无法判定：{resolve_error}。", {
            "vulnerable_available": obs_vuln["available"],
            "fixed_available": obs_fixed["available"],
        })

    assert target is not None and kind is not None and owner_id is not None
    # account 目标取账号敏感字段，resource 目标取资源敏感字段
    sensitive_pairs = data.sensitive_pairs(kind, target)
    # 两种形态均不可用
    if not obs_vuln["available"] and not obs_fixed["available"]:
        return cannot(
            "无法判定：未修复与已修复两种形态均未取得有效响应，"
            f"未修复形态错误={obs_vuln['errors'] or '状态码缺失'}，"
            f"已修复形态错误={obs_fixed['errors'] or '状态码缺失'}。"
        )

    if ownership["is_cross_owner_access"]:
        # ---- 核心越权场景：非所有者访问他人资源 ----
        # 判定以“已修复形态”的响应为准，未修复形态仅作为漏洞存在与否的对照证据
        if not obs_fixed["available"]:
            return cannot(
                "无法判定：已修复形态持续不可用，无法验证修复后是否仍泄露 "
                f"{target['id']} 的敏感字段；未修复形态 "
                f"{'已取得响应' if obs_vuln['available'] else '同样不可用'}。",
                {"fixed_form_errors": obs_fixed["errors"]},
            )
        fixed_leaks = detect_leaks(obs_fixed["body_excerpt"], sensitive_pairs)
        vuln_leaks = (
            detect_leaks(obs_vuln["body_excerpt"], sensitive_pairs)
            if obs_vuln["available"]
            else []
        )
        if fixed_leaks:
            # 已修复形态仍向非所有者泄露 -> 修复无效，漏洞仍存在
            reason = (
                f"漏洞仍存在：用户 {actor} 非资源 {target['id']} 的所有者（所有者为 "
                f"{owner_id}），已修复形态响应（HTTP {obs_fixed['status_code']}）"
                f"仍包含敏感字段 {fixed_leaks}。"
            )
            return {
                "code": VERDICT_VULNERABLE,
                "label": VERDICT_LABELS[VERDICT_VULNERABLE],
                "reason": reason,
                "evidence": {
                    "leaked_fields_in_fixed": fixed_leaks,
                    "leaked_fields_in_vulnerable": vuln_leaks,
                },
            }
        # 已修复形态未泄露 -> 修复有效
        if vuln_leaks:
            reason = (
                f"漏洞已修复：用户 {actor} 越权访问 {owner_id} 的资源 {target['id']} 时，"
                f"未修复形态（HTTP {obs_vuln['status_code']}）泄露敏感字段 {vuln_leaks}，"
                f"已修复形态（HTTP {obs_fixed['status_code']}）未检出任何敏感字段，"
                "前后对比证明归属控制已生效。"
            )
        elif obs_vuln["available"]:
            reason = (
                f"漏洞已修复：已修复形态对非所有者 {actor} 返回 HTTP "
                f"{obs_fixed['status_code']} 且响应不含 {target['id']} 的敏感字段；"
                "未修复形态本次也未观察到泄露，修复后归属保护到位。"
            )
        else:
            reason = (
                f"漏洞已修复：已修复形态对非所有者 {actor} 返回 HTTP "
                f"{obs_fixed['status_code']} 且响应不含 {target['id']} 的敏感字段；"
                "未修复形态暂不可用，以修复后归属保护到位作为判定依据。"
            )
        return {
            "code": VERDICT_FIXED,
            "label": VERDICT_LABELS[VERDICT_FIXED],
            "reason": reason,
            "evidence": {
                "leaked_fields_in_vulnerable": vuln_leaks,
                "leaked_fields_in_fixed": fixed_leaks,
            },
        }

    # ---- 属主访问自身资源：归属控制不应误伤合法访问 ----
    usable = [o for o in (obs_vuln, obs_fixed) if o["available"]]
    healthy = [o for o in usable if o["status_code"] == 200]
    if healthy:
        names = "、".join(o["form"] for o in healthy)
        return {
            "code": VERDICT_FIXED,
            "label": VERDICT_LABELS[VERDICT_FIXED],
            "reason": (
                f"漏洞已修复：用户 {actor} 访问自身资源 {target['id']}，"
                f"{names} 形态正常返回 HTTP 200 与属主数据，归属控制未阻断合法访问；"
                "该场景不构成越权。"
            ),
            "evidence": {
                "status_codes": {o["form"]: o["status_code"] for o in usable},
            },
        }
    return cannot(
        f"无法判定：属主 {actor} 访问自身资源 {target['id']} 时可用形态均未返回 200，"
        f"观测状态码={ {o['form']: o['status_code'] for o in usable} }，行为异常。"
    )


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def build_report(
    scenarios: list[dict[str, Any]],
    results: list[dict[str, Any]],
    manifest_path: Path,
    vuln_url: str,
    fixed_url: str,
    unavailable_forms: list[dict[str, str]],
) -> dict[str, Any]:
    counts = {VERDICT_FIXED: 0, VERDICT_VULNERABLE: 0, VERDICT_CANNOT: 0}
    cannot_ids: list[str] = []
    vuln_ids: list[str] = []
    for item in results:
        code = item["verdict"]["code"]
        counts[code] += 1
        if code == VERDICT_CANNOT:
            cannot_ids.append(item["id"])
        elif code == VERDICT_VULNERABLE:
            vuln_ids.append(item["id"])

    if counts[VERDICT_VULNERABLE] > 0 or counts[VERDICT_CANNOT] > 0:
        if counts[VERDICT_FIXED] == 0:
            overall_code = OVERALL_CANNOT
        else:
            overall_code = OVERALL_NOT_FIXED
    else:
        overall_code = OVERALL_FIXED

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tool_version": TOOL_VERSION,
        "manifest_path": str(manifest_path),
        "target_forms": {
            "vulnerable": vuln_url,
            "fixed": fixed_url,
        },
        "scenario_count": len(scenarios),
        "unavailable_forms": unavailable_forms,
        "scenarios": results,
        "overall_conclusion": {
            "code": overall_code,
            "label": OVERALL_LABELS[overall_code],
            "summary": (
                f"共 {len(scenarios)} 条场景：已修复 {counts[VERDICT_FIXED]} 条，"
                f"漏洞仍存在 {counts[VERDICT_VULNERABLE]} 条，"
                f"无法判定 {counts[VERDICT_CANNOT]} 条。"
            ),
            "vulnerable_scenario_ids": vuln_ids,
            "cannot_determine_scenario_ids": cannot_ids,
        },
    }


def write_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def execute_one(
    index: int,
    scenario: dict[str, Any],
    data: RangeData,
    vuln_url: str,
    fixed_url: str,
    retries: int,
    retry_delay: float,
    timeout: float,
) -> dict[str, Any]:
    """执行单条场景的双形态请求与判定（线程安全，仅使用局部变量与只读数据）。"""
    actor = data.user_by_id(scenario["actor"])
    token = actor["token"] if actor else None

    obs_vuln = run_observation(
        "vulnerable", vuln_url, scenario, token, retries, retry_delay, timeout
    )
    obs_fixed = run_observation(
        "fixed", fixed_url, scenario, token, retries, retry_delay, timeout
    )
    verdict = judge(scenario, obs_vuln, obs_fixed, data)
    return {
        "order": index + 1,
        "id": scenario["id"],
        "actor": scenario["actor"],
        "path": scenario["path"],
        "query": scenario["query"],
        "ownership": _ownership_snapshot(scenario, data),
        "vulnerable_response": _strip_attempt_detail(obs_vuln),
        "fixed_response": _strip_attempt_detail(obs_fixed),
        "verdict": verdict,
        "_form_available": {
            "vulnerable": obs_vuln["available"],
            "fixed": obs_fixed["available"],
        },
    }


def run(
    manifest_path: Path,
    report_path: Path,
    vuln_url: str,
    fixed_url: str,
    data_path: Path | None,
    retries: int,
    retry_delay: float,
    timeout: float,
    workers: int = 1,
    quiet: bool = False,
) -> int:
    scenarios, error_code = load_manifest(manifest_path)
    if error_code is not None:
        return error_code
    assert scenarios is not None

    data = load_data(data_path)
    _print(f"[信息] 清单校验通过，共 {len(scenarios)} 条场景；开始双形态对比执行 ...")
    _print(f"       未修复形态: {vuln_url}")
    _print(f"       已修复形态: {fixed_url}")

    # 每种形态的失败计数，用于识别“持续不可用的执行形态”
    form_failures = {"vulnerable": 0, "fixed": 0}
    total = len(scenarios)

    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor

        _print(f"       并发工作线程: {workers}")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    execute_one, index, scenario, data, vuln_url, fixed_url,
                    retries, retry_delay, timeout,
                )
                for index, scenario in enumerate(scenarios)
            ]
            raw_results = [future.result() for future in futures]
        # 线程池按提交顺序收集，天然保持清单顺序；再显式按 order 排序兜底
        raw_results.sort(key=lambda item: item["order"])
    else:
        raw_results = [
            execute_one(index, scenario, data, vuln_url, fixed_url,
                        retries, retry_delay, timeout)
            for index, scenario in enumerate(scenarios)
        ]

    results: list[dict[str, Any]] = []
    # 大清单下节流进度输出，避免万行级控制台输出在管道/CI 环境阻塞子进程
    progress_step = max(1, total // 20)
    for item in raw_results:
        if not item["_form_available"]["vulnerable"]:
            form_failures["vulnerable"] += 1
        if not item["_form_available"]["fixed"]:
            form_failures["fixed"] += 1
        item.pop("_form_available")
        results.append(item)
        if item["order"] == 1 or item["order"] % progress_step == 0 or item["order"] == total:
            if not quiet:
                _print(
                    f"  [{item['order']}/{total}] {item['id']:<24} "
                    f"{item['verdict']['label']}（{item['verdict']['code']}）"
                )

    unavailable_forms = [
        {
            "form": form,
            "base_url": vuln_url if form == "vulnerable" else fixed_url,
            "failure_count": count,
            "note": f"该形态在全部 {len(scenarios)} 条场景中均未取得有效响应，判定为持续不可用",
        }
        for form, count in form_failures.items()
        if count == len(scenarios)
    ]

    report = build_report(
        scenarios, results, manifest_path, vuln_url, fixed_url, unavailable_forms
    )
    write_report(report, report_path)

    overall = report["overall_conclusion"]
    _print("")
    _print(f"[信息] 报告已写入: {report_path.resolve()}")
    _print(f"[结论] {overall['label']}（{overall['code']}）— {overall['summary']}")
    if unavailable_forms:
        for item in unavailable_forms:
            _print(f"[警告] 执行形态持续不可用: {item['form']} ({item['base_url']})")
    if overall["cannot_determine_scenario_ids"]:
        _print(f"       无法判定项: {overall['cannot_determine_scenario_ids']}")

    if overall["code"] == OVERALL_FIXED:
        return EXIT_OK
    if overall["code"] == OVERALL_CANNOT:
        return EXIT_ALL_CANNOT
    return EXIT_NOT_FULLY_FIXED


def _ownership_snapshot(scenario: dict[str, Any], data: RangeData) -> dict[str, Any]:
    kind, target, owner_id, _ = resolve_target(scenario, data)
    return {
        "actor": scenario["actor"],
        "target_kind": kind,
        "target_id": target.get("id") if target else None,
        "target_owner": owner_id,
        "is_cross_owner_access": (owner_id is not None and owner_id != scenario["actor"]),
    }


def _strip_attempt_detail(obs: dict[str, Any]) -> dict[str, Any]:
    """报告中保留尝试次数与每次状态码，但精简冗长字段。"""
    return {
        "available": obs["available"],
        "status_code": obs["status_code"],
        "body_excerpt": obs["body_excerpt"],
        "attempts": obs["attempts"],
        "attempts_detail": obs["attempts_detail"],
        "errors": obs["errors"],
        "url": obs["url"],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="越权漏洞修复对比判定工具（未修复形态 vs 已修复形态）"
    )
    parser.add_argument(
        "scenario_file",
        nargs="?",
        default="scenarios.json",
        help="场景清单路径，默认当前目录 scenarios.json",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="report.json",
        help="判定报告输出路径，默认当前目录 report.json",
    )
    parser.add_argument("--vuln-url", default="http://127.0.0.1:8081")
    parser.add_argument("--fixed-url", default="http://127.0.0.1:8082")
    parser.add_argument("--data", default=None, help="靶场数据文件，默认项目根 data.json")
    parser.add_argument("--retries", type=int, default=2, help="暂时性故障重试次数，默认 2")
    parser.add_argument("--retry-delay", type=float, default=0.3, help="重试间隔秒数，默认 0.3")
    parser.add_argument("--timeout", type=float, default=5.0, help="单次请求超时秒数，默认 5")
    parser.add_argument(
        "--workers", type=int, default=1,
        help="并发工作线程数，默认 1（串行）；大规模清单可调大，结果仍按清单顺序输出",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="安静模式：仅输出错误与最终结论，适合大规模清单或管道/CI 环境",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(
            manifest_path=Path(args.scenario_file),
            report_path=Path(args.output),
            vuln_url=args.vuln_url,
            fixed_url=args.fixed_url,
            data_path=Path(args.data) if args.data else None,
            retries=max(0, args.retries),
            retry_delay=args.retry_delay,
            timeout=args.timeout,
            workers=max(1, args.workers),
            quiet=args.quiet,
        )
    except KeyboardInterrupt:
        _print("[中断] 用户取消执行。")
        return EXIT_RUNTIME_ERROR
    except Exception as exc:  # noqa: BLE001
        _print(f"[错误] 运行期异常: {type(exc).__name__}: {exc}")
        return EXIT_RUNTIME_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
