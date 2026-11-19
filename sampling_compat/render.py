"""Plain-text rendering helpers for the local CLI."""

from __future__ import annotations

import json
from typing import Any

from .errors import BusinessError


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def print_error(error: BusinessError) -> None:
    print_json(error.to_dict())


def render_request_list(app: Any) -> None:
    print("\n请求列表")
    print("-" * 78)
    print(f"{'request_id':<14} {'tokenizer':<14} {'steps':>5} {'seed':>10}  views")
    for request_id, request in app.requests.items():
        steps = len(request.logits) if request.logits is not None else 0
        views = ",".join(request.profile_versions())
        print(
            f"{request_id:<14} {request.tokenizer_version:<14} "
            f"{steps:>5} {request.sampling_seed:>10}  {views}"
        )


def render_inspection(inspection: dict[str, Any]) -> None:
    print(f"\n请求 {inspection['request_id']} | 视图 {inspection['view_version']}")
    print("=" * 78)
    print("原始配置:")
    print(inspection["raw_config"].rstrip())
    print("\n解析字段:")
    parsed = inspection["parsed_config"]
    print(
        f"  version={parsed['version']} temperature={parsed['temperature']} "
        f"literal={parsed['temperature_literal']!r} top_k={parsed['top_k']}"
    )
    print(
        f"  top_p={parsed['top_p']} repetition_penalty={parsed['repetition_penalty']} "
        f"vocab_version={parsed['vocab_version']}"
    )
    print(f"  units={json.dumps(parsed['units'], ensure_ascii=False, sort_keys=True)}")
    print("\n归一化参数:")
    normalized = inspection["normalized"]
    print(
        f"  temperature={normalized['temperature']} top_k={normalized['top_k']} "
        f"top_p={normalized['top_p']} "
        f"repetition_penalty={normalized['repetition_penalty']}"
    )
    print(f"  checksum={inspection['normalized_checksum']}")
    print(f"  range_policy={json.dumps(normalized['range_policy'], ensure_ascii=False)}")
    if normalized["defaulted_fields"]:
        print("  显式补全:")
        for item in normalized["defaulted_fields"]:
            print(f"    - {json.dumps(item, ensure_ascii=False, sort_keys=True)}")
    if normalized["clamped_fields"]:
        print("  受控截断:")
        for item in normalized["clamped_fields"]:
            print(f"    - {json.dumps(item, ensure_ascii=False, sort_keys=True)}")


def render_decision(decision: dict[str, Any], *, trace: bool = True) -> None:
    print(
        f"\n采样决策 {decision['request_id']} | {decision['config_version']} | "
        f"{decision['sampling_status']}"
    )
    print("=" * 78)
    print(f"token_ids     : {decision['token_ids']}")
    print(f"token_sha256  : {decision['token_sha256']}")
    print(f"artifact_hash : {decision['artifact_checksum']}")
    normalized = decision["normalized_params"]
    print(
        f"normalized    : temp={normalized['temperature']} k={normalized['top_k']} "
        f"p={normalized['top_p']} rep={normalized['repetition_penalty']}"
    )
    print(f"seed          : {decision['sampling_seed']} ({decision['seed_source']})")
    if not trace:
        return
    for step in decision["step_traces"]:
        print(
            f"\nstep {step['step']}  uniform={step['uniform']:.15f}  "
            f"chosen={step['chosen_token']}  "
            f"top_k_n={len(step['top_k_candidate_ids'])} "
            f"top_p_n={step['top_p_candidate_count']}"
        )
        print("  token  scaled_logit        weight    cumulative")
        for candidate in step["candidates"]:
            marker = "*" if candidate["token_id"] == step["chosen_token"] else " "
            print(
                f"{marker} {candidate['token_id']:>5} "
                f"{candidate['scaled_logit']:>16.10f} "
                f"{candidate['weight']:>13.10f} "
                f"{candidate['cumulative_weight']:>13.10f}"
            )


def render_adjudication(conclusion: dict[str, Any]) -> None:
    print(f"\n跨版本裁决 {conclusion['request_id']}")
    print("=" * 78)
    print(f"status          : {conclusion['status']}")
    print(f"versions        : {', '.join(conclusion['versions'])}")
    print(f"reference       : {conclusion['reference_version']}")
    print(f"token_ids       : {conclusion['token_ids']}")
    print(f"token_sha256    : {conclusion['token_sha256']}")
    print(f"semantic_hash   : {conclusion['semantic_fingerprint']}")


def render_migration(record: dict[str, Any]) -> None:
    print(f"\n迁移演示 {record['request_id']}")
    print("=" * 78)
    upgrade = record.get("upgrade")
    if upgrade:
        print("v1 -> v2 升级补全:")
        for item in upgrade["added_fields"]:
            print(
                f"  + {item['field']}={item['value']!r} "
                f"[{item['rule']}] {item['reason']}"
            )
        print("  升级结果:")
        print(upgrade["output_text"].rstrip())
    downgrade = record.get("downgrade")
    if downgrade:
        print("\nv2 -> v1 降级丢弃:")
        for item in downgrade["dropped_fields"]:
            loss = "SEMANTIC_LOSS" if item.get("semantic_loss") else "metadata-only"
            print(
                f"  - {item['field']} value={item['source_value']!r} "
                f"[{item['rule']}] ({loss})"
            )
        print("v2 -> v1 量化:")
        for item in downgrade["quantized_fields"]:
            print(
                f"  ~ {item['field']} {item['before']!r} -> {item['after']!r} "
                f"[{item['rule']}]"
            )
        print("  降级结果:")
        print(downgrade["output_text"].rstrip())
        if downgrade["losses"]:
            print("  语义损失:")
            for item in downgrade["losses"]:
                print(f"    ! {json.dumps(item, ensure_ascii=False, sort_keys=True)}")


def render_error_outcomes(outcomes: list[dict[str, Any]]) -> None:
    print("\n错误码演示")
    print("-" * 78)
    print(f"{'case':<28} {'expected':<28} {'actual':<28} result")
    for outcome in outcomes:
        print(
            f"{outcome['case']:<28} {outcome['expected_code']:<28} "
            f"{outcome['actual_code']:<28} {'PASS' if outcome['passed'] else 'FAIL'}"
        )
    print("\n错误归因摘要:")
    for outcome in outcomes:
        error = outcome.get("error") or {}
        attribution = []
        for key in ("request_id", "field", "step", "entry", "version"):
            if key in error:
                attribution.append(f"{key}={error[key]}")
        print(f"  {outcome['actual_code']:<28} {' '.join(attribution)}")


def render_self_check(report: dict[str, Any]) -> None:
    print("\n跨模块一致性自检")
    print("=" * 78)
    print(f"ok                      : {report['ok']}")
    print(f"decision recompute      : {report['decision_checks']}")
    print(f"float64 reference       : {report['reference_checks']}")
    print(f"migration traceability  : {report['migration_checks']}")
    print(f"adjudication            : {report['adjudication_checks']}")
    print(f"max reference weight Δ  : {report['max_reference_weight_delta']:.3e}")
    print(f"snapshot versions       : {report['snapshot_versions']}")
    if report["expected_semantic_gaps"]:
        print("expected semantic gaps  :")
        for gap in report["expected_semantic_gaps"]:
            print(
                f"  - {gap['request_id']} fields={gap['fields']} "
                f"affected_steps={gap['affected_steps']}"
            )


def render_snapshots(summary: dict[str, Any]) -> None:
    print("\n版本化快照")
    print("=" * 78)
    print(f"path         : {summary['path']}")
    print(f"reload_count : {summary['reload_count']}")
    print(f"last_reload  : {json.dumps(summary['last_reload'], ensure_ascii=False, sort_keys=True)}")
    if not summary["entries"]:
        print("entries      : <empty; run the demo or persist a request>")
        return
    print(f"{'entry':<24} {'version':>8} checksum")
    for kind, entry in summary["entries"].items():
        print(f"{kind:<24} {entry['version']:>8} {entry['checksum']}")


def render_recent_errors(errors: list[dict[str, Any]]) -> None:
    print("\n最近业务错误")
    print("=" * 78)
    if not errors:
        print("<none>")
        return
    for entry in errors[-10:]:
        error = entry["error"]
        attribution = " ".join(
            f"{key}={error[key]}"
            for key in ("request_id", "field", "step", "entry", "version")
            if key in error
        )
        print(f"{error['code']:<28} {entry['context']:<34} {attribution}")
