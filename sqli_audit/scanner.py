"""扫描编排：基线可达性 → 逐参数逐载荷探测 → 可疑参数二次确认。

关键性质：
- 参数顺序 (name, location)、载荷顺序均为固定常量，扫描行为与工作目录无关；
- 单个探测失败被捕获并标记 incomplete，不中断后续参数；
- 通过 ScanState 复用已完成结论，恢复重跑只补未完成项、不重复计数。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import analysis, client
from .config import TargetConfig
from .payloads import CONFIRMATIONS, PROBES, Payload
from .state import ScanState


class BaselineUnreachable(RuntimeError):
    """基线请求不可达，扫描无法开始。"""


@dataclass
class ScanOutcome:
    baseline_fingerprint: analysis.Fingerprint
    param_records: list[dict]
    summary: dict


def _baseline_fields(cfg: TargetConfig) -> dict[str, str]:
    return dict(cfg.baseline)


def _probe_fields(cfg: TargetConfig, param_name: str, payload_value: str) -> dict[str, str]:
    fields = _baseline_fields(cfg)
    fields[param_name] = payload_value
    return fields


def _result_from_response(cfg: TargetConfig, resp, kind: str, baseline_fp) -> dict:
    fp = analysis.fingerprint(cfg, resp)
    if not fp.reachable:
        return {
            "request_outcome": "incomplete",
            "verdict": "probe_error",
            "reason": fp.transport_error or "未连通",
            "response": fp.to_dict(),
        }
    verdict = analysis.classify_probe(kind, baseline_fp, fp)
    return {
        "request_outcome": "completed",
        "verdict": verdict.verdict,
        "reason": verdict.reason,
        "response": fp.to_dict(),
    }


def _run_with_state(cfg: TargetConfig, state: ScanState, key: str,
                    fields: dict[str, str], kind: str, baseline_fp) -> dict:
    cached = state.get(key)
    if cached is not None:
        # 状态文件只保存成功结论，命中即复用、不重复发请求、不重复计数
        return cached
    resp = client.send(cfg, fields)
    result = _result_from_response(cfg, resp, kind, baseline_fp)
    if result["request_outcome"] == "completed":
        # 仅缓存已完成项；未完成项留待恢复后重跑
        state.put(key, result)
    return result


def run_scan(cfg: TargetConfig, state: ScanState) -> ScanOutcome:
    # ---- 1. 基线可达性 ----
    if state.baseline is None:
        resp = client.send(cfg, _baseline_fields(cfg))
        fp = analysis.fingerprint(cfg, resp)
        if not fp.reachable:
            raise BaselineUnreachable(
                f"基线请求失败，目标不可达: {fp.transport_error} "
                f"(url={cfg.url}, method={cfg.method})"
            )
        state.baseline = fp.to_dict()
    baseline_fp = analysis.Fingerprint(
        reachable=state.baseline["reachable"],
        status=state.baseline["status"],
        order_ids=tuple(state.baseline["order_ids"]),
        row_count=state.baseline["row_count"],
        data_sha256=state.baseline["data_sha256"],
        json_ok=state.baseline["json_ok"],
        server_error=state.baseline["server_error"],
        transport_error=state.baseline["transport_error"],
    )

    param_records: list[dict] = []
    probes_total = probes_completed = probes_incomplete = 0
    confirms_total = confirms_completed = confirms_incomplete = 0
    params_suspicious = params_confirmed = params_incomplete = 0

    # ---- 2. 逐参数、逐载荷（顺序固定） ----
    for param in cfg.params:
        probe_results: list[dict] = []
        incomplete_reasons: list[str] = []
        suspicious_hits: list[dict] = []

        for payload in PROBES:
            probes_total += 1
            key = ScanState.probe_key(param.name, payload.payload_id)
            result = _run_with_state(
                cfg, state, key,
                _probe_fields(cfg, param.name, payload.value),
                payload.kind, baseline_fp,
            )
            row = {
                "payload_id": payload.payload_id,
                "kind": payload.kind,
                "title": payload.title,
                "injected_value": payload.value,
                **result,
            }
            probe_results.append(row)
            if result["request_outcome"] == "incomplete":
                probes_incomplete += 1
                incomplete_reasons.append(
                    f"{payload.payload_id} 未完成: {result['reason']}"
                )
            else:
                probes_completed += 1
            if result["verdict"] == "suspicious":
                suspicious_hits.append(row)

        # ---- 3. 可疑参数二次确认 ----
        confirmation_results: list[dict] = []
        confirmation_status = "not_applicable"
        evidence_summary = ""
        is_suspicious = bool(suspicious_hits)

        if is_suspicious:
            params_suspicious += 1
            fps: dict[str, analysis.Fingerprint] = {}
            for c_payload in CONFIRMATIONS:
                confirms_total += 1
                key = ScanState.confirm_key(param.name, c_payload.payload_id)
                result = _run_with_state(
                    cfg, state, key,
                    _probe_fields(cfg, param.name, c_payload.value),
                    c_payload.kind, baseline_fp,
                )
                confirmation_results.append({
                    "payload_id": c_payload.payload_id,
                    "kind": c_payload.kind,
                    "title": c_payload.title,
                    "injected_value": c_payload.value,
                    **result,
                })
                if result["request_outcome"] == "incomplete":
                    confirms_incomplete += 1
                    incomplete_reasons.append(
                        f"{c_payload.payload_id} 确认未完成: {result['reason']}"
                    )
                else:
                    confirms_completed += 1
                fps[c_payload.payload_id] = analysis.Fingerprint(
                    reachable=result["response"]["reachable"],
                    status=result["response"]["status"],
                    order_ids=tuple(result["response"]["order_ids"]),
                    row_count=result["response"]["row_count"],
                    data_sha256=result["response"]["data_sha256"],
                    json_ok=result["response"]["json_ok"],
                    server_error=result["response"]["server_error"],
                    transport_error=result["response"]["transport_error"],
                )

            confirmation_status, evidence_summary, _ = analysis.evaluate_confirmation(
                baseline_fp,
                fps["C01_BOOL_TRUE"],
                fps["C02_BOOL_FALSE"],
                fps["C03_UNION_SENTINEL"],
            )
            if confirmation_status == "confirmed":
                params_confirmed += 1
        else:
            info = [r["reason"] for r in probe_results if r["verdict"] == "informational"]
            evidence_summary = "；".join(info) if info else "全部载荷下数据子树与基线一致，未发现查询语义改变"

        if confirmation_status == "confirmed":
            status = "vulnerable_confirmed"
        elif is_suspicious:
            status = "suspicious_unconfirmed"
        elif incomplete_reasons:
            status = "incomplete"
        else:
            status = "not_suspicious"

        if incomplete_reasons:
            params_incomplete += 1

        param_records.append({
            "name": param.name,
            "location": param.location,
            "label": param.label,
            "status": status,
            "suspicious": is_suspicious,
            "confirmation_status": confirmation_status,
            "evidence_summary": evidence_summary,
            "suspicious_probe_ids": [r["payload_id"] for r in suspicious_hits],
            "incomplete_reasons": incomplete_reasons,
            "probe_results": probe_results,
            "confirmation_results": confirmation_results,
        })

    # 固定排序：参数名 + 位置
    param_records.sort(key=lambda r: (r["name"], r["location"]))

    summary = {
        "params_total": len(cfg.params),
        "params_suspicious": params_suspicious,
        "params_confirmed": params_confirmed,
        "params_incomplete": params_incomplete,
        "probes_total": probes_total,
        "probes_completed": probes_completed,
        "probes_incomplete": probes_incomplete,
        "confirmations_total": confirms_total,
        "confirmations_completed": confirms_completed,
        "confirmations_incomplete": confirms_incomplete,
        "coverage_complete": probes_incomplete == 0 and confirms_incomplete == 0,
    }
    return ScanOutcome(baseline_fp, param_records, summary)
