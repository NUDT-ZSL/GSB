"""End-to-end local acceptance demo with machine-readable evidence."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np

from .cache import cache_diagnose
from .engine import execute
from .generate_data import generate_dataset
from .injection import inject_diagnose
from .selfcheck import equivalence_report
from .util import atomic_write_json


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_demo(output_dir: str | Path = "artifacts") -> dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    data_dir = Path("data")
    runtime = out / "runtime"
    cache_dir = runtime / "cache"
    snapshot_dir = runtime / "snapshots"
    shutil.rmtree(runtime, ignore_errors=True)
    runtime.mkdir(parents=True, exist_ok=True)

    dataset_meta = generate_dataset(data_dir)
    manifest = Path(dataset_meta["manifest"])

    cold = execute(
        manifest,
        data_dir=data_dir,
        cache_dir=cache_dir,
        snapshot_dir=snapshot_dir,
    )
    atomic_write_json(out / "execution_cold.json", cold.to_dict())

    warm = execute(
        manifest,
        data_dir=data_dir,
        cache_dir=cache_dir,
        snapshot_dir=snapshot_dir,
    )
    atomic_write_json(out / "execution_warm.json", warm.to_dict())

    equivalence = equivalence_report(
        manifest,
        data_dir=data_dir,
        cache_dir=cache_dir,
        snapshot_dir=snapshot_dir,
    )
    atomic_write_json(out / "equivalence.json", equivalence)

    injections = inject_diagnose(
        manifest,
        data_dir=data_dir,
        cache_dir=cache_dir,
        snapshot_dir=snapshot_dir,
    )
    atomic_write_json(out / "injections.json", injections)

    cache_result = cache_diagnose(
        manifest,
        changes={
            "case_silu_eps_axis": {"eps": 1e-3},
            "case_gelu_perm": {"axis_order": [1, 2, 0]},
            "case_downstream_layernorm": {"axis_order": [1, 2, 0]},
        },
        data_dir=data_dir,
        cache_dir=cache_dir,
        snapshot_dir=snapshot_dir,
    )
    atomic_write_json(out / "cache_diagnosis.json", cache_result)

    strict = execute(
        manifest,
        data_dir=data_dir,
        cache_dir=runtime / "resource_strict_cache",
        snapshot_dir=runtime / "resource_strict_snapshots",
        limits={"max_resident_bytes": 1, "strict": True},
        case_ids=["case_gelu_perm"],
        run_selfcheck=False,
    )
    atomic_write_json(out / "resource_strict.json", strict.to_dict())
    fallback = execute(
        manifest,
        data_dir=data_dir,
        cache_dir=runtime / "resource_fallback_cache",
        snapshot_dir=runtime / "resource_fallback_snapshots",
        limits={"max_blocks": 1, "allow_block_expand": False},
        case_ids=["case_gelu_perm"],
    )
    atomic_write_json(out / "resource_fallback.json", fallback.to_dict())

    corrupt_root = runtime / "corrupt_snapshots"
    shutil.copytree(snapshot_dir, corrupt_root)
    corrupt_index = json.loads((corrupt_root / "index.json").read_text(encoding="utf-8"))
    corrupt_case = "case_gelu_perm"
    entry = corrupt_index["cases"][corrupt_case]
    snapshot_file = corrupt_root / entry["snapshot"]
    snapshot_file.write_bytes(snapshot_file.read_bytes()[:-5] + b"BROKEN")
    corrupt = execute(
        manifest,
        data_dir=data_dir,
        cache_dir=runtime / "corrupt_cache",
        snapshot_dir=corrupt_root,
        case_ids=[corrupt_case],
        run_selfcheck=False,
    )
    atomic_write_json(out / "snapshot_corruption.json", corrupt.to_dict())

    fused_cases = [case for case in cold.cases if case.plan and case.plan.path == "fused"]
    cold_hits = sum(1 for case in cold.cases if case.cache.get("hit"))
    warm_hits = sum(1 for case in warm.cases if case.cache.get("hit"))
    max_abs = max(
        (case.equivalence.get("max_abs_deviation") or 0.0) for case in cold.cases
    )
    injection_records = {
        record["kind"]: {
            "case_id": record["case_id"],
            "stage": record["stage"],
            "business_code": record["business_code"],
            "unaffected_cases_unchanged": record["unaffected_cases_unchanged"],
            "record": record.get("record"),
        }
        for record in injections["records"]
    }
    snapshot_versions = {
        case.case_id: {
            "cold": case.snapshot.get("version"),
            "warm": warm.case_map[case.case_id].snapshot.get("version"),
            "replacement": case.snapshot.get("replacement"),
        }
        for case in cold.cases
        if case.snapshot
    }
    strict_codes = sorted({d.code for d in strict.diagnostics})
    fallback_case = fallback.cases[0]
    corrupt_codes = sorted({d.code for d in corrupt.diagnostics})

    assertions = {
        "all_base_cases_ok": cold.ok and warm.ok,
        "all_fused_outputs_byte_identical": all(
            case.equivalence.get("fused_vs_separate_byte_identical")
            for case in cold.cases
            if case.plan and case.plan.path == "fused"
        ),
        "all_intermediate_digests_equal": all(
            case.intermediate_digest == case.separate_intermediate_digest
            for case in cold.cases
            if case.pair and case.pair.feasible
        ),
        "all_tolerances_ok": all(case.equivalence.get("ok") for case in cold.cases),
        "cache_cold_to_warm_hit": cold_hits < warm_hits and warm_hits >= len(fused_cases),
        "injections_all_codes_observed": injections["all_expected_codes_observed"],
        "injections_all_unaffected_isolated": injections["all_unaffected_cases_unchanged"],
        "invalid_reused_disjoint": cache_result["no_intersection"],
        "invalid_reused_union_covers": cache_result["union_covers_all_affected_nodes"],
        "eps_reuses_axis_mapping": "axis_mapping"
        in cache_result["reused_nodes"]["case_silu_eps_axis"],
        "axis_reuses_activation": "activation"
        in cache_result["reused_nodes"]["case_gelu_perm"],
        "axis_invalidates_downstream": "downstream:2:layernorm"
        in cache_result["invalidated_nodes"]["case_downstream_layernorm"],
        "strict_resource_returns_code": "E_RESOURCE_LIMIT" in strict_codes,
        "resource_fallback_keeps_equivalence": (
            fallback.ok
            and fallback_case.plan.path == "separate"
            and fallback_case.plan.fallback_category == "block_count"
            and fallback_case.equivalence["ok"]
        ),
        "snapshot_versions_monotonic": all(
            versions["warm"] > versions["cold"] for versions in snapshot_versions.values()
        ),
        "snapshot_full_replacement": all(
            versions["replacement"] == "full" for versions in snapshot_versions.values()
        ),
        "corruption_returns_code": "E_SNAPSHOT_CORRUPT" in corrupt_codes,
        "selfcheck_ok": equivalence["ok"] and equivalence["selfcheck"]["ok"],
    }
    acceptance = {
        "ok": all(assertions.values()),
        "dataset": dataset_meta,
        "execution": {
            "case_count": len(cold.cases),
            "fused_path_count": len(fused_cases),
            "separate_path_count": len(cold.cases) - len(fused_cases),
            "cold_cache_hits": cold_hits,
            "warm_cache_hits": warm_hits,
            "max_abs_deviation_vs_float64": max_abs,
            "paths": {
                case.case_id: {
                    "path": case.plan.path if case.plan else None,
                    "fallback_category": case.plan.fallback_category if case.plan else None,
                    "upstream_code": case.plan.upstream_code if case.plan else None,
                    "tolerance_ok": case.equivalence.get("ok"),
                    "byte_identical": case.equivalence.get(
                        "fused_vs_separate_byte_identical"
                    ),
                }
                for case in cold.cases
            },
        },
        "injections": injection_records,
        "cache_invalidation": {
            "affected_cases": cache_result["affected_cases"],
            "unchanged_cases": cache_result["unchanged_cases"],
            "changed_fields": cache_result["changed_fields"],
            "invalidated_nodes": cache_result["invalidated_nodes"],
            "reused_nodes": cache_result["reused_nodes"],
            "recompute_suffix": cache_result["recompute_suffix"],
            "cache_hit_cases": cache_result["cache_hit_cases"],
            "cache_miss_cases": cache_result["cache_miss_cases"],
            "no_intersection": cache_result["no_intersection"],
            "union_covers_all_affected_nodes": cache_result[
                "union_covers_all_affected_nodes"
            ],
        },
        "resources": {
            "strict_codes": strict_codes,
            "fallback": {
                "path": fallback_case.plan.path,
                "fallback_category": fallback_case.plan.fallback_category,
                "upstream_code": fallback_case.plan.upstream_code,
                "equivalence_ok": fallback_case.equivalence["ok"],
            },
        },
        "snapshots": {
            "versions": snapshot_versions,
            "corruption_codes": corrupt_codes,
        },
        "assertions": assertions,
        "artifacts": {
            "execution_cold": str(out / "execution_cold.json"),
            "execution_warm": str(out / "execution_warm.json"),
            "equivalence": str(out / "equivalence.json"),
            "injections": str(out / "injections.json"),
            "cache_diagnosis": str(out / "cache_diagnosis.json"),
            "resource_strict": str(out / "resource_strict.json"),
            "resource_fallback": str(out / "resource_fallback.json"),
            "snapshot_corruption": str(out / "snapshot_corruption.json"),
        },
    }
    atomic_write_json(out / "acceptance_report.json", acceptance)
    summary = {
        "ok": acceptance["ok"],
        "case_count": len(cold.cases),
        "fused_path_count": len(fused_cases),
        "warm_cache_hits": warm_hits,
        "max_abs_deviation_vs_float64": max_abs,
        "injection_codes": {
            kind: record["business_code"] for kind, record in injection_records.items()
        },
        "partial_invalidation_cases": cache_result["affected_cases"],
        "resource_strict_codes": strict_codes,
        "fallback_category": fallback_case.plan.fallback_category,
        "snapshot_corruption_codes": corrupt_codes,
        "assertions": assertions,
        "acceptance_report": str(out / "acceptance_report.json"),
    }
    _write_text(out / "acceptance_summary.md", _markdown_summary(acceptance))
    return {"ok": acceptance["ok"], "summary": summary, "acceptance": acceptance}


def _markdown_summary(acceptance: dict[str, Any]) -> str:
    execution = acceptance["execution"]
    invalidation = acceptance["cache_invalidation"]
    resources = acceptance["resources"]
    snapshots = acceptance["snapshots"]
    lines = [
        "# Transformer 激活+转置融合验收结果",
        "",
        f"- 总体验收：`{'PASS' if acceptance['ok'] else 'FAIL'}`",
        f"- 样例数：{execution['case_count']}，融合路径：{execution['fused_path_count']}",
        f"- 冷缓存命中：{execution['cold_cache_hits']}，热缓存命中：{execution['warm_cache_hits']}",
        f"- 相对 float64 逐算子参考最大绝对偏差：{execution['max_abs_deviation_vs_float64']}",
        "",
        "## 逐 case 路径",
        "",
        "| case_id | path | fallback | upstream | tolerance | byte-identical |",
        "|---|---|---|---|---|---|",
    ]
    for case_id, value in execution["paths"].items():
        lines.append(
            f"| {case_id} | {value['path']} | {value['fallback_category'] or '-'} | "
            f"{value['upstream_code'] or '-'} | {value['tolerance_ok']} | "
            f"{value['byte_identical'] if value['byte_identical'] is not None else '-'} |"
        )
    lines += [
        "",
        "## 异常注入",
        "",
        "| kind | stage | business code | 未注入 case 不变 |",
        "|---|---|---|---|",
    ]
    for kind, record in acceptance["injections"].items():
        lines.append(
            f"| {kind} | {record['stage']} | {record['business_code']} | "
            f"{record['unaffected_cases_unchanged']} |"
        )
    lines += [
        "",
        "## 部分失效",
        "",
        f"- 受影响 case：`{invalidation['affected_cases']}`",
        f"- 失效/复用无交集：{invalidation['no_intersection']}",
        f"- 并集覆盖全部节点：{invalidation['union_covers_all_affected_nodes']}",
    ]
    for case_id in invalidation["affected_cases"]:
        lines.append(
            f"- `{case_id}` 失效 `{invalidation['invalidated_nodes'][case_id]}`；"
            f"复用 `{invalidation['reused_nodes'][case_id]}`"
        )
    lines += [
        "",
        "## 资源与快照",
        "",
        f"- 严格资源上限业务码：`{resources['strict_codes']}`",
        f"- 非严格资源退回：`{resources['fallback']}`",
        f"- 快照损坏业务码：`{snapshots['corruption_codes']}`",
        f"- 快照版本单调递增：{acceptance['assertions']['snapshot_versions_monotonic']}",
        f"- 快照整体替换：{acceptance['assertions']['snapshot_full_replacement']}",
        "",
        "详细机器可读证据见 `artifacts/acceptance_report.json`。",
        "",
    ]
    return "\n".join(lines)
