"""响应指纹提取与注入判定逻辑。

核心原则（任务第 4 条）：页面正常回显差异不算注入。
判定只看“数据子树”的结构变化（订单行数、订单号集合、哨兵行），
不看订单号输入本身在页面上的回显。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .config import TargetConfig
from .payloads import UNION_SENTINEL_ID


@dataclass(frozen=True)
class Fingerprint:
    reachable: bool
    status: int | None
    order_ids: tuple[str, ...]          # 排序后的订单号集合
    row_count: int
    data_sha256: str                    # 数据子树规范化哈希
    json_ok: bool
    server_error: bool
    transport_error: str | None

    def to_dict(self) -> dict:
        return {
            "reachable": self.reachable,
            "status": self.status,
            "row_count": self.row_count,
            "order_ids": list(self.order_ids),
            "data_sha256": self.data_sha256,
            "json_ok": self.json_ok,
            "server_error": self.server_error,
            "transport_error": self.transport_error,
        }


def _walk(obj: object, path: list[str]) -> object:
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _canonical(obj: object) -> str:
    """确定性 JSON 序列化：固定键序、无空白、确保哈希跨运行一致。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(cfg: TargetConfig, resp) -> Fingerprint:
    """从探测响应中提取数据指纹。"""
    if not resp.reached:
        return Fingerprint(
            reachable=False,
            status=None,
            order_ids=(),
            row_count=0,
            data_sha256="",
            json_ok=False,
            server_error=False,
            transport_error=resp.transport_error,
        )

    order_ids: list[str] = []
    data_subtree: object = None
    json_ok = resp.json_ok
    if json_ok:
        body = json.loads(resp.body_text)
        data_subtree = _walk(body, cfg.data_path)
        if isinstance(data_subtree, list):
            for row in data_subtree:
                if isinstance(row, dict):
                    oid = row.get(cfg.order_id_field)
                    if isinstance(oid, (str, int)):
                        order_ids.append(str(oid))
    order_ids.sort()

    data_hash = ""
    if data_subtree is not None:
        data_hash = hashlib.sha256(_canonical(data_subtree).encode("utf-8")).hexdigest()

    return Fingerprint(
        reachable=True,
        status=resp.status,
        order_ids=tuple(order_ids),
        row_count=len(order_ids),
        data_sha256=data_hash,
        json_ok=json_ok,
        server_error=(resp.status is not None and resp.status >= 500),
        transport_error=None,
    )


@dataclass(frozen=True)
class ProbeVerdict:
    verdict: str          # suspicious / informational / no_effect / probe_error
    reason: str


def classify_probe(kind: str, baseline: Fingerprint, fp: Fingerprint) -> ProbeVerdict:
    """对单个初筛载荷做判定。

    - suspicious：越权数据扩张（行数变多/出现基线外订单/UNION 哨兵行）；
    - informational：仅抑制结果或仅触发服务端错误，属行为差异但不构成泄露证据；
    - no_effect：数据子树与基线一致（正常回显差异落在这里，不会误报）；
    - probe_error：传输失败，调用方应记录为未完成。
    """
    if not fp.reachable:
        return ProbeVerdict("probe_error", f"探测未连通: {fp.transport_error}")

    if fp.server_error:
        return ProbeVerdict(
            "informational",
            f"载荷触发服务端错误(HTTP {fp.status})，无数据返回，不构成越权证据",
        )

    baseline_ids = set(baseline.order_ids)
    extra_ids = [oid for oid in fp.order_ids if oid not in baseline_ids]

    if kind in ("tautology", "union"):
        if UNION_SENTINEL_ID in fp.order_ids:
            return ProbeVerdict(
                "suspicious",
                f"结果集出现 UNION 哨兵订单号 {UNION_SENTINEL_ID}，可向查询注入额外行",
            )
        if fp.row_count > baseline.row_count and extra_ids:
            return ProbeVerdict(
                "suspicious",
                f"返回行数由基线 {baseline.row_count} 增至 {fp.row_count}，"
                f"出现基线外订单 {extra_ids[:5]}，存在越权泄露",
            )
        if fp.data_sha256 and fp.data_sha256 != baseline.data_sha256:
            return ProbeVerdict(
                "informational",
                "数据子树内容变化但未出现行数扩张/越权订单，证据不足，仅记录",
            )
        return ProbeVerdict("no_effect", "数据子树与基线一致，未观察到查询语义改变")

    if kind == "false_condition":
        if fp.row_count < baseline.row_count:
            return ProbeVerdict(
                "informational",
                f"返回行数由基线 {baseline.row_count} 降至 {fp.row_count}，"
                "查询逻辑可被输入影响但无越权泄露，仅记录不判疑",
            )
        if fp.data_sha256 and fp.data_sha256 != baseline.data_sha256:
            return ProbeVerdict("informational", "数据子树变化但无越权泄露，仅记录")
        return ProbeVerdict("no_effect", "数据子树与基线一致")

    if kind == "syntax_error":
        if fp.data_sha256 and fp.data_sha256 != baseline.data_sha256:
            return ProbeVerdict("informational", "语法破坏载荷改变了响应，但无越权泄露，仅记录")
        return ProbeVerdict("no_effect", "数据子树与基线一致")

    return ProbeVerdict("no_effect", "未知载荷类型，按无效果处理")


def evaluate_confirmation(
    baseline: Fingerprint,
    true_fp: Fingerprint,
    false_fp: Fingerprint,
    union_fp: Fingerprint,
) -> tuple[str, str, list[dict]]:
    """汇总三个二次确认载荷的结果，返回 (状态, 证据摘要, 检查明细)。"""
    checks: list[dict] = []
    signals: list[str] = []

    # 布尔差异
    if not true_fp.reachable or not false_fp.reachable:
        checks.append({
            "check": "boolean_differential",
            "result": "incomplete",
            "detail": "确认载荷存在未连通项，布尔差异无法评估",
        })
    else:
        expanded = true_fp.row_count > baseline.row_count
        suppressed = false_fp.row_count == 0 and baseline.row_count > 0
        if expanded and suppressed:
            signals.append(
                f"布尔差异成立：真条件 {true_fp.row_count} 行 > 基线 {baseline.row_count} 行，"
                f"假条件 {false_fp.row_count} 行"
            )
            checks.append({
                "check": "boolean_differential",
                "result": "confirmed",
                "detail": f"true_rows={true_fp.row_count}, false_rows={false_fp.row_count}, "
                          f"baseline_rows={baseline.row_count}",
            })
        else:
            checks.append({
                "check": "boolean_differential",
                "result": "not_observed",
                "detail": f"true_rows={true_fp.row_count}, false_rows={false_fp.row_count}, "
                          f"baseline_rows={baseline.row_count}",
            })

    # UNION 哨兵
    if not union_fp.reachable:
        checks.append({
            "check": "union_sentinel",
            "result": "incomplete",
            "detail": "UNION 确认载荷未连通",
        })
    elif UNION_SENTINEL_ID in union_fp.order_ids:
        signals.append("UNION 确认成立：哨兵行 LEAK 出现在结果集中，可注入任意查询结果")
        checks.append({
            "check": "union_sentinel",
            "result": "confirmed",
            "detail": "sentinel_order_id=LEAK observed in result set",
        })
    else:
        checks.append({
            "check": "union_sentinel",
            "result": "not_observed",
            "detail": f"result_rows={union_fp.row_count}，未出现哨兵行",
        })

    if any(c["result"] == "confirmed" for c in checks):
        return "confirmed", "；".join(signals), checks
    if any(c["result"] == "incomplete" for c in checks):
        return "incomplete", "确认阶段存在未完成探测，证据不足以下结论", checks
    return "not_confirmed", "初筛可疑但二次确认未复现语义改变，保持存疑不作为确认结论", checks
