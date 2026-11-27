"""Command-line orchestration for generation, verification, and reporting."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from .engine import RebalanceEngine
from .generator import generate_bundle, load_generated
from .model import KeyedEvent, MemberEvent, canonical_sha256
from .normalize import normalize_project, normalization_report


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _semantic_run_payload(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "settings": report["settings"],
        "rounds": report["rounds"],
        "verification": report["verification"],
    }


def _run_version_equivalence(
    reports: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    hashes = {
        version: canonical_sha256(_semantic_run_payload(report))
        for version, report in sorted(reports.items())
    }
    reference = hashes[sorted(hashes)[0]]
    differences = [
        {
            "schema": version,
            "runtime_sha256": digest,
            "reference_sha256": reference,
        }
        for version, digest in sorted(hashes.items())
        if digest != reference
    ]
    return {
        "equal": not differences,
        "runtime_sha256": hashes,
        "differences": differences,
        "verdict": (
            "三版本归一后逐轮运行结果一致"
            if not differences
            else "三版本逐轮运行结果不一致"
        ),
    }


def _edge_summary(
    case: dict[str, Any],
    report: dict[str, Any],
) -> dict[str, Any]:
    rounds = report["rounds"]
    final = rounds[-1] if rounds else {}
    round_summaries = []
    for item in rounds:
        round_summaries.append(
            {
                "round": item["round"],
                "at_ms": item["at_ms"],
                "trigger_types": item["trigger"]["types"],
                "expired_members": item["trigger"]["expired_members"],
                "participants": item["participants"],
                "liveness_before": item["liveness"]["before_events"],
                "liveness_after": item["liveness"]["after_events"],
                "assignment_by_member": item["assignment"]["by_member"],
                "redelivery_count": item["redelivery"][
                    "reclaimed_event_count"
                ],
                "single_owner_passed": item["single_owner_check"]["passed"],
                "local_consistent": (
                    item["local_rebalance"][
                        "consistent_with_full_rebalance"
                    ]
                    if item["local_rebalance"]
                    else None
                ),
            }
        )
    key_distribution: dict[str, int] = {}
    for event in case["keyed_events"]:
        pid = f"{event['topic']}:{event['partition']}"
        key_distribution[pid] = key_distribution.get(pid, 0) + 1
    return {
        "name": case["name"],
        "description": case["description"],
        "rounds": round_summaries,
        "final_assignment": (
            final.get("assignment", {}).get("by_partition", {})
            if final
            else {}
        ),
        "final_liveness": (
            final.get("liveness", {}).get("after_events", {}) if final else {}
        ),
        "over_threshold_members": (
            final.get("load_balance", {}).get("over_threshold_members", [])
            if final
            else []
        ),
        "key_distribution": key_distribution,
        "verification": report["verification"],
    }


def execute(
    *,
    seed: int,
    output_dir: Path,
    imbalance_threshold: float,
    inflight_limit: int,
) -> dict[str, Any]:
    generated_dir = output_dir / "generated"
    paths = generate_bundle(seed, generated_dir)
    generated = load_generated(paths)

    project_raw = {
        "v1": generated["project_v1"],
        "v2": generated["project_v2"],
        "v3": generated["project_v3"],
    }
    projects = {
        version: normalize_project(raw)
        for version, raw in sorted(project_raw.items())
    }
    norm_report = normalization_report(projects)
    if not norm_report["canonical_equal"]:
        raise RuntimeError("schema normalization did not converge")

    member_events = [
        MemberEvent.from_dict(item, index)
        for index, item in enumerate(generated["member_events"])
    ]
    keyed_events = [
        KeyedEvent.from_dict(item) for item in generated["keyed_events"]
    ]

    reports: dict[str, dict[str, Any]] = {}
    for version, project in sorted(projects.items()):
        engine = RebalanceEngine(
            project,
            member_events,
            keyed_events,
            inflight_limit=inflight_limit,
            imbalance_threshold=imbalance_threshold,
        )
        reports[version] = engine.run()
    version_equivalence = _run_version_equivalence(reports)
    if not version_equivalence["equal"]:
        raise RuntimeError("normalized schemas produced different runtime results")

    edge_reports = []
    for case in generated["edge_cases"]:
        project = normalize_project(case["project"])
        engine = RebalanceEngine(
            project,
            [
                MemberEvent.from_dict(item, index)
                for index, item in enumerate(case["member_events"])
            ],
            [KeyedEvent.from_dict(item) for item in case["keyed_events"]],
            inflight_limit=inflight_limit,
            imbalance_threshold=imbalance_threshold,
        )
        edge_reports.append(_edge_summary(case, engine.run()))

    primary = reports["v1"]
    all_checks = (
        norm_report["canonical_equal"]
        and version_equivalence["equal"]
        and primary["verification"]["passed"]
        and all(
            report["verification"]["passed"] for report in reports.values()
        )
        and all(item["verification"]["passed"] for item in edge_reports)
    )

    _write_json(output_dir / "normalization_report.json", norm_report)
    _write_json(output_dir / "rebalance_report.json", primary)
    _write_json(
        output_dir / "version_equivalence.json", version_equivalence
    )
    _write_json(output_dir / "edge_cases_report.json", edge_reports)

    summary = {
        "schema": "rebalance-kernel-summary/v1",
        "seed": seed,
        "passed": all_checks,
        "group_id": projects["v1"].group_id,
        "canonical_sha256": projects["v1"].content_sha256(),
        "normalization_equal": norm_report["canonical_equal"],
        "version_runtime_equal": version_equivalence["equal"],
        "round_count": len(primary["rounds"]),
        "local_rebalance_round_count": primary["verification"][
            "local_rebalance_round_count"
        ],
        "redelivered_event_count": primary["verification"][
            "unique_redelivered_event_count"
        ],
        "expiration_rounds": primary["verification"]["expiration_rounds"],
        "edge_case_count": len(edge_reports),
        "edge_cases_equal": version_equivalence["equal"],
        "artifacts": {
            "generated": str(paths["root"]),
            "normalization": str(output_dir / "normalization_report.json"),
            "rebalance": str(output_dir / "rebalance_report.json"),
            "version_equivalence": str(
                output_dir / "version_equivalence.json"
            ),
            "edge_cases": str(output_dir / "edge_cases_report.json"),
        },
    }
    _write_json(output_dir / "summary.json", summary)
    return {
        "summary": summary,
        "normalization_report": norm_report,
        "runtime": primary,
        "all_runtime_reports": reports,
        "version_equivalence": version_equivalence,
        "edge_cases": edge_reports,
    }


def _print_text(result: dict[str, Any]) -> None:
    summary = result["summary"]
    print("房产交易消费者重平衡内核")
    print("=" * 32)
    print(f"seed: {summary['seed']}")
    print(f"group: {summary['group_id']}")
    print(
        "三版本归一: "
        + ("一致" if summary["normalization_equal"] else "不一致")
        + f" | canonical sha256={summary['canonical_sha256']}"
    )
    print(
        "三版本逐轮运行: "
        + ("一致" if summary["version_runtime_equal"] else "不一致")
    )
    print()
    for item in result["runtime"]["rounds"]:
        assignment = item["assignment"]
        by_member = ", ".join(
            f"{member_id}=[{', '.join(pids) if pids else '-'}]"
            for member_id, pids in assignment["by_member"].items()
            if pids
        )
        if not by_member:
            by_member = "-"
        redelivery = item["redelivery"]["reclaimed_event_count"]
        local = item["local_rebalance"]
        local_text = ""
        if local:
            local_text = (
                " | 局部=一致"
                if local["consistent_with_full_rebalance"]
                else " | 局部=有差异"
            )
        expired = item["trigger"]["expired_members"]
        expired_text = f" | 失效={','.join(expired)}" if expired else ""
        print(
            f"轮次 {item['round']:02d} @ {item['at_ms']}ms "
            f"| 触发={','.join(item['trigger']['types'])} "
            f"| 参与={','.join(item['participants']) or '-'} "
            f"| 归属={by_member} "
            f"| 重投递={redelivery}{local_text}{expired_text}"
        )
    print()
    print("边界场景:")
    for item in result["edge_cases"]:
        print(
            f"- {item['name']}: "
            + ("通过" if item["verification"]["passed"] else "失败")
            + f" | {item['description']}"
        )
    print()
    print(
        "总体校验: "
        + ("PASS" if summary["passed"] else "FAIL")
        + " | 单归属/重投递/局部一致性详见 rebalance_report.json"
    )
    print("产物:")
    for label, path in summary["artifacts"].items():
        print(f"- {label}: {path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic consumer-group rebalance kernel"
    )
    parser.add_argument("--seed", type=int, default=20261128)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts"),
        help="directory for generated inputs and reports",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="console format; artifacts are always written",
    )
    parser.add_argument("--imbalance-threshold", type=float, default=0.25)
    parser.add_argument("--inflight-limit", type=int, default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = execute(
            seed=args.seed,
            output_dir=args.output_dir,
            imbalance_threshold=args.imbalance_threshold,
            inflight_limit=args.inflight_limit,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        payload = {
            "summary": result["summary"],
            "normalization_report": result["normalization_report"],
            "runtime": result["runtime"],
            "version_equivalence": result["version_equivalence"],
            "edge_cases": result["edge_cases"],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_text(result)
    return 0 if result["summary"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
