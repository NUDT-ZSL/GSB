from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import generator
from .classify import classify_blocks
from .compare import compare_versions
from .imageops import extract_foreground, load_rgb
from .metrics import evaluate_analyses, load_truth
from .model import VersionAnalysis
from .reading_order import build_reading_order
from .segment import segment_page
from .verifier import verify


ANALYSIS_SCHEMA = "gsb-layout-run-results/v1"
FIXED_OUTPUTS = [
    "annotations/page_*.json",
    "features/page_*.json",
    "reports/page_*.json",
    "reports/page_*.md",
    "cross_version/page_*.json",
    "cross_version_diffs.json",
    "verifier/page_*.json",
    "metrics.json",
    "summary.json",
    "run_results.json",
]


def analyze_version(
    page_id: str,
    format_name: str,
    image_path: str | Path,
    reference_size: tuple[int, int],
) -> VersionAnalysis:
    rgb = load_rgb(image_path)
    native_height, native_width = rgb.shape[:2]
    scale = native_width / reference_size[0]
    foreground = extract_foreground(rgb, scale)
    merged_blocks, projection = segment_page(foreground, scale)
    blocks = classify_blocks(merged_blocks, rgb, foreground, scale, reference_size)
    order = build_reading_order(blocks, reference_size[0])
    return VersionAnalysis(
        page_id=page_id,
        format_name=format_name,
        image_path=str(image_path),
        image_size=(native_width, native_height),
        reference_size=reference_size,
        blocks=blocks,
        order=order,
        projection=projection,
    )


def analyze_dataset(
    samples_dir: str | Path,
    out_dir: str | Path,
) -> dict[str, Any]:
    samples_path = Path(samples_dir)
    out_path = Path(out_dir)
    manifest = json.loads((samples_path / "manifest.json").read_text(encoding="utf-8"))
    reference_size = tuple(int(value) for value in manifest["reference_size"])
    page_analyses: dict[str, dict[str, VersionAnalysis]] = {}
    truth_by_page: dict[str, dict[str, Any]] = {}
    cross_versions: dict[str, dict[str, Any]] = {}
    verifier_results: dict[str, dict[str, Any]] = {}
    annotations: dict[str, dict[str, Any]] = {}

    for page in manifest["pages"]:
        page_id = page["page_id"]
        page_versions: dict[str, VersionAnalysis] = {}
        for format_name, variant in page["variants"].items():
            image_path = samples_path / variant["path"]
            page_versions[format_name] = analyze_version(
                page_id,
                format_name,
                image_path,
                reference_size,
            )
        page_analyses[page_id] = page_versions
        truth_path = samples_path / page["truth"]
        truth_by_page[page_id] = load_truth(truth_path)
        comparison = compare_versions(page_id, page_versions)
        cross_versions[page_id] = comparison

    out_path.mkdir(parents=True, exist_ok=True)
    for directory in ("annotations", "features", "reports", "cross_version", "verifier"):
        (out_path / directory).mkdir(parents=True, exist_ok=True)

    for page_id, versions in page_analyses.items():
        base = versions["lossless_png"]
        canonical_report = base.report_dict()
        canonical_report["cross_version_consistency"] = cross_versions[page_id]
        canonical_report["format_versions"] = {
            name: _canonical_version_entry(name, versions, cross_versions[page_id])
            for name in versions
        }
        annotation = {
            "schema": "gsb-layout-annotation/v1",
            "page_id": page_id,
            "reference_size": list(reference_size),
            "canonical_format": "lossless_png",
            "blocks": [block.to_dict() for block in base.blocks],
            "reading_order": list(base.order.order),
            "conflict_section": canonical_report["conflict_section"],
            "format_consensus": cross_versions[page_id]["consensus"],
        }

        _write_json(out_path / "reports" / f"{page_id}.json", canonical_report)
        _write_json(out_path / "annotations" / f"{page_id}.json", annotation)
        _write_json(out_path / "features" / f"{page_id}.json", base.features_dict())
        for format_name, analysis in versions.items():
            _write_json(
                out_path / "features" / f"{page_id}.{format_name}.json",
                analysis.features_dict(),
            )
        _write_json(out_path / "cross_version" / f"{page_id}.json", cross_versions[page_id])
        markdown = render_page_markdown(canonical_report)
        (out_path / "reports" / f"{page_id}.md").write_text(markdown, encoding="utf-8")

        verifier = verify(canonical_report, base.features_dict())
        verifier["cross_version_consistent"] = cross_versions[page_id]["consensus"][
            "all_versions_consistent"
        ]
        verifier_results[page_id] = verifier
        _write_json(out_path / "verifier" / f"{page_id}.json", verifier)

    metrics = evaluate_analyses(page_analyses, truth_by_page)
    _write_json(out_path / "metrics.json", metrics)

    aggregate = metrics["aggregate"]
    primary_format_summary = metrics["by_format"]["lossless_png"]
    all_consistent = all(
        comparison["consensus"]["all_versions_consistent"]
        for comparison in cross_versions.values()
    )
    verifier_ok = all(result["ok"] for result in verifier_results.values())
    conflict_records = {
        page_id: {
            "unresolved_blocks": cross_versions[page_id]["reference_unresolved_blocks"],
            "detected_conflicts": [
                conflict.to_dict()
                for conflict in page_analyses[page_id]["lossless_png"].order.conflicts
            ],
            "expected_conflicts": truth_by_page[page_id].get("expected_conflicts", []),
        }
        for page_id in page_analyses
    }
    summary = {
        "schema": "gsb-layout-summary/v1",
        "seed": manifest["seed"],
        "page_count": len(manifest["pages"]),
        "formats": sorted({name for page in manifest["pages"] for name in page["variants"]}),
        "block_segmentation_iou": round(aggregate["block_segmentation_iou"], 6),
        "type_classification_accuracy": round(aggregate["type_classification_accuracy"], 6),
        "kendall_rank_correlation": round(aggregate["kendall_rank_correlation"], 6),
        "all_versions_consistent": all_consistent,
        "independent_verifier_ok": verifier_ok,
        "expected_conflicts": sum(
            len(truth.get("expected_conflicts", []))
            for truth in truth_by_page.values()
        ),
        "detected_conflicts": primary_format_summary["detected_conflicts"],
        "matched_conflicts": primary_format_summary["matched_conflicts"],
        "conflict_counts_by_format": {
            name: {
                "expected": values["expected_conflicts"],
                "detected": values["detected_conflicts"],
                "matched": values["matched_conflicts"],
            }
            for name, values in metrics["by_format"].items()
        },
        "conflict_records": conflict_records,
        "pages": [
            {
                "page_id": page["page_id"],
                "cross_version_consistent": cross_versions[page["page_id"]]["consensus"][
                    "all_versions_consistent"
                ],
                "verifier_ok": verifier_results[page["page_id"]]["ok"],
                "conflicts": len(
                    page_analyses[page["page_id"]]["lossless_png"].order.conflicts
                ),
            }
            for page in manifest["pages"]
        ],
    }
    _write_json(out_path / "summary.json", summary)

    cross_version_aggregate = {
        "schema": "gsb-layout-cross-version-diffs/v1",
        "seed": manifest["seed"],
        "reference_format": "lossless_png",
        "boundary_tolerance_ref_px": cross_versions[next(iter(cross_versions))][
            "versions"
        ][0]["boundary_tolerance_ref_px"],
        "all_versions_consistent": all_consistent,
        "pages": cross_versions,
    }
    _write_json(out_path / "cross_version_diffs.json", cross_version_aggregate)

    run_results = {
        "schema": ANALYSIS_SCHEMA,
        "seed": manifest["seed"],
        "samples_dir": str(samples_path),
        "results_dir": str(out_path),
        "fixed_outputs": FIXED_OUTPUTS,
        "summary": summary,
        "metrics": metrics,
        "verifier": {
            "ok": verifier_ok,
            "pages": verifier_results,
        },
        "cross_version": cross_version_aggregate,
    }
    _write_json(out_path / "run_results.json", run_results)
    return run_results


def generate_dataset(
    out_dir: str | Path,
    seed: int = generator.DEFAULT_SEED,
    page_count: int = 3,
) -> dict[str, Any]:
    return generator.generate_dataset(out_dir, seed=seed, page_count=page_count)


def render_page_markdown(report: dict[str, Any]) -> str:
    page_id = report["page_id"]
    consensus = report.get("cross_version_consistency", {}).get("consensus", {})
    lines = [
        f"# 版面标注报告 {page_id}",
        "",
        f"- 参考尺寸：{report['reference_size'][0]} × {report['reference_size'][1]}",
        f"- 标准格式：{report.get('format', 'lossless_png')}",
        f"- 跨版本结论一致：{str(consensus.get('all_versions_consistent', 'n/a')).lower()}",
        "",
        "## 阅读顺序",
        "",
        "| 顺序 | 块编号 | 类型 | 边界框 (x0,y0,x1,y1) | 规则依据 |",
        "|---:|---|---|---|---|",
    ]
    for item in report["reading_order"]:
        bbox = ",".join(f"{value:.1f}" for value in item["bbox"])
        rule = item["rule_basis"].get("rule", "")
        lines.append(
            f"| {item['index']} | {item['uid']} | {item['type_zh']} | ({bbox}) | `{rule}` |"
        )
    if not report["reading_order"]:
        lines.append("| - | - | - | - | - |")

    lines.extend(
        [
            "",
            "## 块与量化判定依据",
            "",
            "| 块编号 | 类型 | 顺序 | 置信度 | 边界框 | 触发规则 | 关键特征值 |",
            "|---|---|---:|---:|---|---|---|",
        ]
    )
    for block in report["blocks"]:
        bbox = ",".join(f"{value:.1f}" for value in block["bbox"])
        features = block["features"]
        key_values = (
            f"width={features.get('width_ref')}, height={features.get('height_ref')}, "
            f"aspect={features.get('aspect_ratio')}, density={features.get('ink_density')}, "
            f"red={features.get('red_ratio')}, "
            f"internal_columns={features.get('internal_column_count')}, "
            f"top_margin={features.get('top_margin_ratio')}, "
            f"main_width={features.get('main_column_width_ref')}, "
            f"column_pitch={features.get('column_pitch_ref')}"
        )
        order = block["order_index"] if block["order_index"] is not None else "未定"
        lines.append(
            f"| {block['uid']} | {block['type_zh']} | {order} | {block['confidence']:.3f} | "
            f"({bbox}) | `{block['evidence'].get('rule', '')}` | {key_values} |"
        )

    lines.extend(
        [
            "",
            "## 邻接图与版式规则",
            "",
            "| 起点 | 终点 | 规则 | 证据 |",
            "|---|---|---|---|",
        ]
    )
    for edge in report["adjacency_graph"]["edges"]:
        evidence = json.dumps(edge["evidence"], ensure_ascii=False, sort_keys=True)
        lines.append(f"| {edge['from']} | {edge['to']} | `{edge['rule']}` | {evidence} |")
    if not report["adjacency_graph"]["edges"]:
        lines.append("| - | - | - | - |")

    lines.extend(
        [
            "",
            "## 顺序冲突处置",
            "",
            f"- 无法唯一确定顺序的块：{report['conflict_section']['unresolved_blocks']}",
        ]
    )
    conflicts = report["conflict_section"]["conflicts"]
    if not conflicts:
        lines.append("- 本页无规则冲突。")
    for conflict in conflicts:
        lines.append(
            f"- `{conflict['kind']}`：涉及 {conflict['involved']}；{conflict['message']}"
        )
        for edge in conflict["candidate_edges"]:
            evidence = json.dumps(edge["evidence"], ensure_ascii=False, sort_keys=True)
            lines.append(
                f"  - 冲突候选边 {edge['from']} → {edge['to']} "
                f"（`{edge['rule']}`，{evidence}）"
            )

    variant_table = report.get("format_versions", {})
    lines.extend(
        [
            "",
            "## 格式版本一致性",
            "",
            "| 格式 | 图像尺寸 | 块数 | 顺序序列 | 未定块 |",
            "|---|---|---:|---|---|",
        ]
    )
    for name, version in variant_table.items():
        lines.append(
            f"| {name} | {version['image_size'][0]}×{version['image_size'][1]} | "
            f"{version['block_count']} | {', '.join(version['order'])} | "
            f"{', '.join(version['unresolved_blocks']) or '无'} |"
        )
    differences = report.get("cross_version_consistency", {}).get("differences", [])
    lines.extend(["", f"- 超限/翻转/位次差异数：{len(differences)}"])
    for difference in differences:
        lines.append(
            f"- {difference.get('block_uid')} [{difference.get('kind')}]: "
            f"{difference.get('flags', [])}"
        )
    lines.append("")
    return "\n".join(lines)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def _canonical_version_entry(
    format_name: str,
    versions: dict[str, VersionAnalysis],
    comparison: dict[str, Any],
) -> dict[str, Any]:
    analysis = versions[format_name]
    if format_name == comparison["reference_format"]:
        order = list(analysis.order.order)
        unresolved = list(analysis.order.unresolved)
    else:
        entry = next(
            item for item in comparison["versions"] if item["format"] == format_name
        )
        order = entry["canonical_order"]
        unresolved = entry["canonical_unresolved_blocks"]
    return {
        "image_size": list(analysis.image_size),
        "block_count": len(analysis.blocks),
        "order": order,
        "unresolved_blocks": unresolved,
    }
