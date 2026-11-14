"""报告渲染（Markdown / JSON / HTML）。"""

from __future__ import annotations

import hashlib
import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import Config

STATUS_TEXT = {
    "selected": "已输出",
    "merged": "已合并",
    "skipped": "未产出",
    "error": "未产出",
}


def _fmt_time(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return f"{text}s"


def _fmt_score(value: float | None) -> str:
    return "—" if value is None else f"{value:.4f}"


def _output_text(record: dict[str, Any]) -> str:
    output = record.get("output")
    if not output:
        return "—"
    if record["status"] == "merged":
        return f"合并至 {output}"
    return output


def _merged_text(merged: Sequence[dict[str, Any]]) -> str:
    return (
        "、".join(f"{_fmt_time(m['time'])}（差 {m['difference']:.4f}）" for m in merged)
        or "—"
    )


REPORT_STEM = "covers_report"


def report_filename(iteration: str | None, extension: str) -> str:
    """报告文件名；非默认迭代名会作为后缀，避免多次运行互相覆盖。"""

    suffix = "" if not iteration or iteration == "default" else f"__{iteration}"
    return f"{REPORT_STEM}{suffix}.{extension}"


def write_report_files(
    report: "Report", directory: Path, iteration: str | None
) -> dict[str, Path]:
    """把 Markdown / HTML / JSON 三种报告写入 ``directory``。"""

    directory.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for extension, renderer in (
        ("json", report.to_json),
        ("md", report.to_markdown),
        ("html", report.to_html),
    ):
        target = directory / report_filename(iteration, extension)
        target.write_text(renderer(), encoding="utf-8")
        written[extension] = target
    return written


def hash_files(paths: Sequence[Path]) -> dict[str, str]:
    """计算文件的 sha256 摘要。"""

    digest: dict[str, str] = {}
    for path in paths:
        sha = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                sha.update(chunk)
        digest[str(path)] = sha.hexdigest()
    return digest


def digest_directory(directory: Path) -> tuple[str, int]:
    """目录内图片文件的整体摘要（按文件名排序后拼接各自 sha256），返回 (摘要, 文件数)。"""

    files = sorted(p for p in directory.iterdir() if p.is_file())
    names = [
        p for p in files
        if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
    ]
    outer = hashlib.sha256()
    for path in names:
        outer.update(path.name.encode("utf-8"))
        outer.update(hashlib.sha256(path.read_bytes()).digest())
    return outer.hexdigest(), len(names)


class Report:
    """汇总一次运行，并渲染为 Markdown、HTML 与 JSON。"""

    def __init__(
        self,
        *,
        entry_records: Sequence[dict[str, Any]],
        groups: Sequence[dict[str, Any]],
        notes: Sequence[str],
        hashes: dict[str, Any] | None,
        config: Config,
        started_at: datetime,
        finished_at: datetime,
        iteration: str,
        output_dir: Path,
        frame_dir: Path,
        manifest_path: Path,
        failures: Sequence[dict[str, Any]] = (),
    ) -> None:
        self.entry_records = list(entry_records)
        self.groups = list(groups)
        self.notes = list(notes)
        self.hashes = hashes or {}
        self.config = config
        self.started_at = started_at
        self.finished_at = finished_at
        self.iteration = iteration
        self.output_dir = output_dir
        self.frame_dir = frame_dir
        self.manifest_path = manifest_path
        self.failures = list(failures)

    @property
    def status_counts(self) -> dict[str, int]:
        counts = {key: 0 for key in STATUS_TEXT}
        for record in self.entry_records:
            counts[record["status"]] = counts.get(record["status"], 0) + 1
        return counts

    @property
    def unified_frames(self) -> list[dict[str, Any]]:
        return [entry for entry in self.entry_records if entry.get("size_note")]

    def to_dict(self) -> dict[str, Any]:
        """结构化报告，供 JSON 输出与自动检查使用。"""

        return {
            "tool": {"name": "cover-selector", "version": __version__},
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": self.finished_at.isoformat(timespec="seconds"),
            "paths": {
                "frames": str(self.frame_dir),
                "manifest": str(self.manifest_path),
                "output": str(self.output_dir),
            },
            "config": self.config.to_dict(),
            "status_counts": self.status_counts,
            "entries": [
                {
                    "position": r["position"],
                    "label": r.get("label"),
                    "time": r.get("time"),
                    "status": r["status"],
                    "output": r.get("output"),
                    "frame_index": r.get("frame_index"),
                    "frame_file": r.get("frame_file"),
                    "merged_into": r.get("merged_into"),
                    "score": r.get("score"),
                    "reasons": list(r.get("reasons") or []),
                }
                for r in self.entry_records
            ],
            "covers": list(self.groups),
            "input_hashes": self.hashes,
            "write_failures": list(self.failures),
            "notes": list(self.notes),
        }

    # ------------------------------------------------------------------
    def to_markdown(self) -> str:
        cfg = self.config
        counts = self.status_counts
        lines: list[str] = [
            "# 封面候选选取报告",
            "",
            "| 项目 | 内容 |",
            "| --- | --- |",
            f"| 生成时间 | {self.finished_at:%Y-%m-%d %H:%M:%S} |",
            f"| 工具版本 | cover-selector {__version__} |",
            f"| 帧序列目录 | `{self.frame_dir}` |",
            f"| 候选清单 | `{self.manifest_path}` |",
            f"| 输出目录 | `{self.output_dir}` |",
            f"| 清单条目 | 共 {len(self.entry_records)} 条：已输出 {counts['selected']}，"
            f"已合并 {counts['merged']}，未产出 {counts['skipped'] + counts['error']} |",
            "",
            "## 本次运行配置",
            "",
            "| 参数 | 值 |",
            "| --- | --- |",
            f"| 帧率 fps | {cfg.fps:g} |",
            f"| 基准尺寸 | {cfg.base_width}x{cfg.base_height} |",
            f"| 尺寸统一方式 | {cfg.resize_mode} |",
            f"| 近似重复阈值 | {cfg.dedupe_threshold:g}（帧间平均像素差 ≤ 阈值判定为重复） |",
            f"| 帧号容差 | ±{cfg.tolerance_frames:g} 帧 |",
            f"| 起始帧号 | {cfg.frame_start} |",
            "",
            "## 清单条目处理结果",
            "",
            "| 序号 | 名称 | 时间点 | 状态 | 输出文件 | 命中帧号 | 原因 / 备注 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for record in self.entry_records:
            lines.append(
                "| {} | {} | {} | {} | {} | {} | {} |".format(
                    record["position"],
                    record.get("label") or "—",
                    _fmt_time(record.get("time")),
                    STATUS_TEXT[record["status"]],
                    _output_text(record),
                    record.get("frame_index")
                    if record.get("frame_index") is not None
                    else "—",
                    "；".join(record.get("reasons") or []) or "—",
                )
            )

        lines += [
            "",
            "## 输出候选与画面差异强度排序",
            "",
            "排序依据：画面差异强度评分由梯度清晰度（权重 0.75）与灰度对比度（权重 0.25）加权；"
            "梯度越强、灰度分布越分散，评分越高，排序越靠前。评分相同时保留时间点更早、清单序号更靠前者。"
            f"两帧平均像素差 ≤ {cfg.dedupe_threshold:g} 时视为近似重复，仅保留组内评分最高者。",
            "",
            "| 排名 | 时间点 | 输出文件 | 评分 | 清晰度 | 对比度 | 平均梯度 | 被合并的时间点 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for group in self.groups:
            lines.append(
                "| {} | {} | {} | {} | {} | {} | {:.3f} | {} |".format(
                    group["rank"],
                    _fmt_time(group["time"]),
                    group["output"],
                    _fmt_score(group["score"]),
                    _fmt_score(group["clarity"]),
                    _fmt_score(group["contrast"]),
                    group["gradient"],
                    _merged_text(group["merged"]),
                )
            )

        lines += ["", "## 尺寸统一记录", ""]
        handled = self.unified_frames
        if handled:
            lines.append("| 帧号 | 文件 | 处理 |")
            lines.append("| --- | --- | --- |")
            for entry in handled:
                lines.append(
                    f"| {entry['frame_index']} | {entry['frame_file']} | {entry['size_note']} |"
                )
        else:
            lines.append("所有候选帧均为基准尺寸，无需统一。")

        if self.failures:
            lines += ["", "## 写出失败", ""]
            lines.append("| 目标文件 | 原因 |")
            lines.append("| --- | --- |")
            for failure in self.failures:
                lines.append(f"| {failure['output']} | {failure['reason']} |")

        lines += ["", "## 输入文件校验", ""]
        if self.hashes:
            lines.append("运行结束时的输入摘要；与运行前一致即说明源帧与清单未被修改：")
            lines.append("")
            lines.append("| 输入 | 路径 | 文件数 | sha256 |")
            lines.append("| --- | --- | --- | --- |")
            manifest_hash = self.hashes.get("manifest")
            if manifest_hash:
                lines.append(
                    f"| 候选清单 | `{manifest_hash['path']}` | 1 | `{manifest_hash['sha256']}` |"
                )
            frames_hash = self.hashes.get("frames")
            if frames_hash:
                lines.append(
                    f"| 帧序列 | `{frames_hash['dir']}` | {frames_hash['file_count']} | "
                    f"`{frames_hash['sha256']}` |"
                )
        else:
            lines.append("未计算输入文件摘要。")

        if self.notes:
            lines += ["", "## 运行说明", ""]
            lines.extend(f"- {note}" for note in self.notes)

        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------
    def to_html(self) -> str:
        def esc(value: Any) -> str:
            return html.escape("" if value is None else str(value))

        cfg = self.config
        parts = [
            '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
            "<title>封面候选选取报告</title>",
            "<style>body{font-family:system-ui,'Segoe UI','Microsoft YaHei',sans-serif;"
            "margin:24px;color:#222;background:#fafbfc}"
            "table{border-collapse:collapse;margin:12px 0;background:#fff}"
            "th,td{border:1px solid #d5d9e0;padding:6px 10px;vertical-align:top;font-size:14px}"
            "th{background:#eef1f6}h2{margin-top:28px}code{background:#eef1f6;padding:1px 4px}"
            "</style></head><body>",
            "<h1>封面候选选取报告</h1>",
            "<table>",
            f"<tr><th>生成时间</th><td>{self.finished_at:%Y-%m-%d %H:%M:%S}</td></tr>",
            f"<tr><th>工具版本</th><td>cover-selector {esc(__version__)}</td></tr>",
            f"<tr><th>帧序列目录</th><td><code>{esc(self.frame_dir)}</code></td></tr>",
            f"<tr><th>候选清单</th><td><code>{esc(self.manifest_path)}</code></td></tr>",
            f"<tr><th>输出目录</th><td><code>{esc(self.output_dir)}</code></td></tr>",
            f"<tr><th>配置</th><td>{cfg.fps:g} fps；基准 {cfg.base_width}x{cfg.base_height}；"
            f"重复阈值 {cfg.dedupe_threshold:g}；容差 ±{cfg.tolerance_frames:g} 帧；"
            f"起始帧号 {cfg.frame_start}</td></tr>",
            "</table>",
            "<h2>清单条目处理结果</h2><table><tr><th>序号</th><th>名称</th><th>时间点</th>"
            "<th>状态</th><th>输出文件</th><th>命中帧号</th><th>原因 / 备注</th></tr>",
        ]
        for record in self.entry_records:
            parts.append(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
                "<td>{}</td><td>{}</td></tr>".format(
                    record["position"],
                    esc(record.get("label") or "—"),
                    _fmt_time(record.get("time")),
                    STATUS_TEXT[record["status"]],
                    esc(_output_text(record)),
                    esc(record.get("frame_index")
                        if record.get("frame_index") is not None else "—"),
                    esc("；".join(record.get("reasons") or []) or "—"),
                )
            )

        parts += [
            "</table><h2>输出候选与画面差异强度排序</h2>",
            "<p>排序依据：评分 = 0.75 × 梯度清晰度 + 0.25 × 灰度对比度；"
            f"平均像素差 ≤ {cfg.dedupe_threshold:g} 的帧视为近似重复并合并。</p>",
            "<table><tr><th>排名</th><th>时间点</th><th>输出文件</th><th>评分</th>"
            "<th>清晰度</th><th>对比度</th><th>平均梯度</th><th>被合并的时间点</th></tr>",
        ]
        for group in self.groups:
            parts.append(
                "<tr><td>{}</td><td>{}</td><td><code>{}</code></td><td>{}</td><td>{}</td>"
                "<td>{}</td><td>{:.3f}</td><td>{}</td></tr>".format(
                    group["rank"],
                    _fmt_time(group["time"]),
                    esc(group["output"]),
                    _fmt_score(group["score"]),
                    _fmt_score(group["clarity"]),
                    _fmt_score(group["contrast"]),
                    group["gradient"],
                    esc(_merged_text(group["merged"])),
                )
            )
        parts.append("</table>")

        parts.append("<h2>尺寸统一记录</h2>")
        unified = self.unified_frames
        if unified:
            parts.append("<table><tr><th>帧号</th><th>文件</th><th>处理</th></tr>")
            for entry in unified:
                parts.append(
                    f"<tr><td>{entry['frame_index']}</td><td>{esc(entry['frame_file'])}</td>"
                    f"<td>{esc(entry['size_note'])}</td></tr>"
                )
            parts.append("</table>")
        else:
            parts.append("<p>所有候选帧均为基准尺寸，无需统一。</p>")

        if self.failures:
            parts.append("<h2>写出失败</h2><table><tr><th>目标文件</th><th>原因</th></tr>")
            for failure in self.failures:
                parts.append(
                    f"<tr><td><code>{esc(failure['output'])}</code></td>"
                    f"<td>{esc(failure['reason'])}</td></tr>"
                )
            parts.append("</table>")

        parts.append("<h2>输入文件校验</h2>")
        if self.hashes:
            parts.append("<table><tr><th>输入</th><th>路径</th><th>文件数</th><th>sha256</th></tr>")
            manifest_hash = self.hashes.get("manifest")
            if manifest_hash:
                parts.append(
                    f"<tr><td>候选清单</td><td><code>{esc(manifest_hash['path'])}</code></td>"
                    f"<td>1</td><td><code>{manifest_hash['sha256']}</code></td></tr>"
                )
            frames_hash = self.hashes.get("frames")
            if frames_hash:
                parts.append(
                    f"<tr><td>帧序列</td><td><code>{esc(frames_hash['dir'])}</code></td>"
                    f"<td>{frames_hash['file_count']}</td>"
                    f"<td><code>{frames_hash['sha256']}</code></td></tr>"
                )
            parts.append("</table>")
        else:
            parts.append("<p>未计算输入文件摘要。</p>")

        if self.notes:
            parts.append("<h2>运行说明</h2><ul>")
            parts.extend(f"<li>{esc(note)}</li>" for note in self.notes)
            parts.append("</ul>")
        parts.append("</body></html>")
        return "\n".join(parts)

    # ------------------------------------------------------------------
    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
