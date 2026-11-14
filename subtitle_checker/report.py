"""Report assembly: group entries by severity, attach overlaps and summary.

Outputs are fully deterministic: no wall-clock timestamps are written. The
provenance record contains the manifest's SHA-256 instead, so two runs over
unchanged inputs produce byte-identical report and preview files.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .check import (
    GROUP_ORDER,
    GROUP_TITLES,
    OverlapPair,
    SubtitleResult,
    sort_by_id,
)
from .manifest import Manifest


def manifest_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class Report:
    manifest: Manifest
    results: list[SubtitleResult]
    overlaps: list[OverlapPair]
    manifest_hash: str
    frames_dir: Path

    def grouped(self) -> dict[str, list[SubtitleResult]]:
        groups: dict[str, list[SubtitleResult]] = {name: [] for name in GROUP_ORDER}
        for result in self.results:
            groups[result.group].append(result)
        for name in groups:
            groups[name] = sort_by_id(groups[name])
        return groups

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {"total": len(self.results), "overlap_pairs": len(self.overlaps)}
        for name in GROUP_ORDER:
            counts[name] = 0
        for result in self.results:
            counts[result.group] += 1
        return counts

    def to_json_dict(self) -> dict[str, object]:
        summary = self.summary()
        groups = self.grouped()
        return {
            "schema_version": 1,
            "manifest_sha256": self.manifest_hash,
            "source": {
                "manifest": str(self.manifest.source),
                "frames_dir": str(self.frames_dir),
                "canvas": {"width": self.manifest.canvas.width, "height": self.manifest.canvas.height},
                "safe_margin": {
                    "top": self.manifest.margin.top,
                    "right": self.manifest.margin.right,
                    "bottom": self.manifest.margin.bottom,
                    "left": self.manifest.margin.left,
                },
                "fx_region": self.manifest.fx_region.as_dict(),
            },
            "summary": summary,
            "groups": [
                {
                    "key": name,
                    "title": GROUP_TITLES[name],
                    "count": len(groups[name]),
                    "ids": [r.id for r in groups[name]],
                    "items": [r.as_dict() for r in groups[name]],
                }
                for name in GROUP_ORDER
            ],
            "overlaps": {
                "count": len(self.overlaps),
                "pairs": [p.as_dict() for p in self.overlaps],
            },
        }

    def to_markdown(self) -> str:
        summary = self.summary()
        groups = self.grouped()
        lines: list[str] = []
        lines.append("# 字幕核对报告")
        lines.append("")
        lines.append(f"- 清单文件：`{self.manifest.source}`（sha256 `{self.manifest_hash[:16]}…`）")
        lines.append(
            f"- 画布：{self.manifest.canvas.width} × {self.manifest.canvas.height}"
            f"；安全区内边距 上 {self.manifest.margin.top} / 右 {self.manifest.margin.right}"
            f" / 下 {self.manifest.margin.bottom} / 左 {self.manifest.margin.left}"
        )
        fx = self.manifest.fx_region
        lines.append(f"- 特效区：x={fx.x}, y={fx.y}, w={fx.width}, h={fx.height}")
        lines.append("")
        lines.append("## 汇总")
        lines.append("")
        lines.append("| 指标 | 数量 |")
        lines.append("| --- | ---: |")
        lines.append(f"| 总条目数 | {summary['total']} |")
        lines.append(f"| 通过数 | {summary['pass']} |")
        lines.append(f"| 越界数 | {summary['overflow']} |")
        lines.append(f"| 遮挡数 | {summary['occlusion']} |")
        lines.append(f"| 未核对数 | {summary['unverified']} |")
        lines.append(f"| 时间异常数 | {summary['timing']} |")
        lines.append(f"| 时间重叠对数 | {summary['overlap_pairs']} |")
        lines.append("")
        lines.append(
            f"计数校验：通过 {summary['pass']} + 越界 {summary['overflow']} + 遮挡 {summary['occlusion']} + "
            f"未核对 {summary['unverified']} + 时间异常 {summary['timing']} = "
            f"{summary['pass'] + summary['overflow'] + summary['occlusion'] + summary['unverified'] + summary['timing']}"
            f"，总条目数 {summary['total']}"
        )
        lines.append("")

        for name in GROUP_ORDER:
            items = groups[name]
            lines.append(f"## {GROUP_TITLES[name]}组（{len(items)} 条，按 id 升序）")
            lines.append("")
            if not items:
                lines.append("（无）")
                lines.append("")
                continue
            lines.append("| id | 起止(ms) | 文字 | 说明 |")
            lines.append("| --- | --- | --- | --- |")
            for item in items:
                sub = item.subtitle
                text = sub.text.replace("\n", "⏎").replace("|", "\\|")
                if len(text) > 40:
                    text = text[:40] + "…"
                lines.append(
                    f"| {item.id} | {sub.start_ms}–{sub.end_ms} | {text} | {'；'.join(item.reasons)} |"
                )
            lines.append("")

        lines.append(f"## 时间重叠对（{len(self.overlaps)} 对，按 id 升序）")
        lines.append("")
        if self.overlaps:
            lines.append("| # | 条目对 | 重叠时长(ms) |")
            lines.append("| --- | --- | ---: |")
            for index, pair in enumerate(self.overlaps, start=1):
                lines.append(f"| {index} | {pair.first_id} ↔ {pair.second_id} | {pair.overlap_ms} |")
        else:
            lines.append("（无）")
        lines.append("")
        return "\n".join(lines)


def build_report(
    manifest: Manifest,
    results: list[SubtitleResult],
    overlaps: list[OverlapPair],
    frames_dir: Path,
) -> Report:
    return Report(
        manifest=manifest,
        results=results,
        overlaps=overlaps,
        manifest_hash=manifest_sha256(Path(manifest.source)),
        frames_dir=frames_dir,
    )


def write_reports(report: Report, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "report.json"
    md_path = output_dir / "report.md"
    json_text = json.dumps(report.to_json_dict(), ensure_ascii=False, indent=2, sort_keys=False)
    json_path.write_text(json_text + "\n", encoding="utf-8", newline="\n")
    md_path.write_text(report.to_markdown(), encoding="utf-8", newline="\n")
    return json_path, md_path
