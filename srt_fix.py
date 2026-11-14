#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SRT 批量去重叠与规范化工具。

读取输入目录下全部 .srt 文件，消除相邻条目时间轴重叠，移除空文本与
时间颠倒的条目，统一时间格式与序号，并将结果按原文件名写入输出目录。
原文件只读，不会被修改。

用法:
    python srt_fix.py <输入目录> <输出目录>
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPORT_FILENAME = "修正记录.txt"

# 时间轴允许逗号或点作为毫秒分隔符；毫秒允许 1~3 位（按整数值补零）。
_TIME_TOKEN = r"\d+:\d{1,2}:\d{1,2}[,.]\d{1,3}"
_TIME_LINE_RE = re.compile(
    rf"^(?P<start>{_TIME_TOKEN})\s*-->\s*(?P<end>{_TIME_TOKEN})"
    r"(?:\s+(?P<tail>.*))?$"
)
_TIME_TOKEN_RE = re.compile(
    r"^(?P<h>\d+):(?P<m>\d{1,2}):(?P<s>\d{1,2})[,.](?P<ms>\d{1,3})$"
)
_INDEX_RE = re.compile(r"^[ \t]*\d+[ \t]*$")
_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n")


class SrtParseError(ValueError):
    """文件无法按 SRT 解析。"""


@dataclass
class Entry:
    """一条字幕（保留原始位置与原始时间，便于生成修正记录）。"""

    position: int                 # 在输入文件中的块顺序，从 1 开始
    declared_index: int | None    # 文件里写的序号；缺失时为 None
    raw_time_line: str            # 原始时间轴整行（已去掉首尾空白）
    tail: str                     # 结束时间之后的附加信息（如 X1:...），可为空
    text_lines: list[str]         # 原样保留的多行文本
    orig_start_ms: int
    orig_end_ms: int
    start_ms: int
    end_ms: int
    removed: bool = False
    removal_reasons: list[str] = field(default_factory=list)
    change_types: list[str] = field(default_factory=list)
    adjust_targets: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.text_lines)


@dataclass
class FileResult:
    """单个文件的处理结果，用于汇总与修正记录。"""

    filename: str
    ok: bool
    reason: str = ""
    input_count: int = 0
    output_count: int = 0
    corrected_entries: int = 0
    has_correction: bool = False
    detail_lines: list[str] = field(default_factory=list)
    extra_notes: list[str] = field(default_factory=list)
    output_text: str = ""


def format_ms(ms: int) -> str:
    """把毫秒数格式化为 HH:MM:SS,mmm（毫秒固定三位）。"""
    if ms < 0:
        ms = 0
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def parse_time_token(token: str) -> int:
    """解析单个时间标记为毫秒数。

    “00:00:01,50” 按 50 毫秒处理，输出时补成 “00:00:01,050”。
    """
    match = _TIME_TOKEN_RE.fullmatch(token)
    if not match:
        raise SrtParseError(f"无法解析时间: {token}")
    hours = int(match.group("h"))
    minutes = int(match.group("m"))
    seconds = int(match.group("s"))
    millis = int(match.group("ms"))  # 1~3 位都按整数值解释，输出补足三位
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def canonical_time_line(start_ms: int, end_ms: int, tail: str = "") -> str:
    line = f"{format_ms(start_ms)} --> {format_ms(end_ms)}"
    if tail:
        line += " " + tail
    return line


def decode_srt(data: bytes) -> tuple[str, str | None]:
    """解码字幕文件。返回 (文本, 实际使用的非默认编码)。

    优先 UTF-8（自动去除 BOM），失败后兼容 GB18030。
    """
    try:
        return data.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gb18030"), "GB18030"
    except UnicodeDecodeError as exc:
        raise SrtParseError(
            "文本编码无法识别（仅支持 UTF-8/UTF-8 BOM，兼容 GB18030）"
        ) from exc


def detect_newline(text: str) -> str:
    """按原文件的行尾风格选择输出行尾，尽量保持合法文件原样。"""
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text:
        return "\r"
    return "\n"


def parse_srt(text: str) -> list[Entry]:
    """解析 SRT 文本。任何结构错误都会使整个文件判为无法处理。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    if not text.strip():
        raise SrtParseError("文件为空或仅含空白字符")

    blocks = [
        block.strip("\n")
        for block in _BLANK_LINE_RE.split(text)
        if block.strip()
    ]
    if not blocks:
        raise SrtParseError("文件为空或仅含空白字符")

    entries: list[Entry] = []
    for position, block in enumerate(blocks, 1):
        lines = block.split("\n")
        declared_index: int | None = None
        time_index = 0

        if _INDEX_RE.fullmatch(lines[0]):
            declared_index = int(lines[0].strip())
            time_index = 1

        if time_index >= len(lines):
            raise SrtParseError(f"第 {position} 个条目缺少时间轴行")

        raw_time_line = lines[time_index].strip()
        match = _TIME_LINE_RE.fullmatch(raw_time_line)
        if not match:
            raise SrtParseError(
                f"第 {position} 个条目的时间轴无法解析: {raw_time_line[:80]}"
            )

        start_ms = parse_time_token(match.group("start"))
        end_ms = parse_time_token(match.group("end"))
        entries.append(
            Entry(
                position=position,
                declared_index=declared_index,
                raw_time_line=raw_time_line,
                tail=match.group("tail") or "",
                text_lines=lines[time_index + 1 :],
                orig_start_ms=start_ms,
                orig_end_ms=end_ms,
                start_ms=start_ms,
                end_ms=end_ms,
            )
        )
    return entries


def _add_once(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _add_change(entry: Entry, change_type: str, target: str | None = None) -> None:
    _add_once(entry.change_types, change_type)
    if target:
        _add_once(entry.adjust_targets, target)


def has_overlap(entries: list[Entry]) -> bool:
    """判断条目序列中是否存在相邻时间重叠（供测试与自检）。"""
    return any(
        entries[index].start_ms < entries[index - 1].end_ms
        for index in range(1, len(entries))
    )


def resolve_overlaps(entries: list[Entry]) -> None:
    """消除相邻条目的时间重叠，原地修改并记录修改类型。

    优先把后一条的起点推到前一条的终点；若这样会让后一条起点超过
    自身终点，则改为把前一条的终点收缩到后一条的起点。
    """
    index = 1
    steps = 0
    # 每次迭代要么消解一处重叠、要么前进一步；交换顺序时会回退，
    # 回退次数受条目数的平方约束，这里给出足够宽松的上限。
    max_steps = max(1000, len(entries) * len(entries) * 2)

    while index < len(entries) and steps < max_steps:
        steps += 1
        previous = entries[index - 1]
        current = entries[index]

        if current.start_ms >= previous.end_ms:
            index += 1
            continue

        # 起点逆序（或起点相同但后一条更短）：交换顺序后再按同一套规则处理，
        # 避免产生倒序输出或零时长条目。
        starts_out_of_order = current.start_ms < previous.start_ms
        same_start_shorter = (
            current.start_ms == previous.start_ms
            and current.end_ms < previous.end_ms
        )
        if starts_out_of_order or same_start_shorter:
            entries[index - 1], entries[index] = current, previous
            change = "时序倒置（相邻条目起点逆序），已交换顺序"
            _add_change(current, change, "相邻条目顺序")
            _add_change(previous, change, "相邻条目顺序")
            index = max(1, index - 1)
            continue

        if previous.end_ms < current.end_ms:
            # 常规重叠：后一条起点不早于前一条终点。
            current.start_ms = previous.end_ms
            _add_change(current, "时间重叠（调整后一条起点）", "后一条起点")
            index += 1
        else:
            # 推后条起点会达到或超过其终点（后一条整段位于前一条范围内，
            # 或两条终点相同）；改为收缩前一条终点。
            previous.end_ms = current.start_ms
            _add_change(previous, "时间重叠（调整前一条终点）", "前一条终点")
            index = max(1, index - 1)

    # 兜底：确保任何情况下相邻区间都不再重叠。正常流程已处理完毕；
    # 若因极端输入触及迭代上限，这里保证输出文件仍然无重叠。
    for position in range(1, len(entries)):
        previous = entries[position - 1]
        current = entries[position]
        if current.start_ms < previous.end_ms:
            current.start_ms = previous.end_ms
            _add_change(current, "时间重叠（兜底顺延起点）", "后一条起点")
        if current.end_ms < current.start_ms:
            current.end_ms = current.start_ms


def _entry_label(entry: Entry) -> str:
    if entry.declared_index is None:
        return f"条目（文件位置 {entry.position}）"
    return f"条目 #{entry.declared_index}（文件位置 {entry.position}）"


def process_srt_file(source: Path) -> FileResult:
    """处理单个 SRT 文件，返回结果（不写文件）。"""
    filename = source.name
    try:
        data = source.read_bytes()
    except OSError as exc:
        return FileResult(filename=filename, ok=False, reason=f"读取失败: {exc}")

    if not data:
        return FileResult(filename=filename, ok=False, reason="文件为空")

    try:
        text, actual_encoding = decode_srt(data)
        original_newline = detect_newline(text)
        had_bom = data.startswith(b"\xef\xbb\xbf")
        entries = parse_srt(text)
    except SrtParseError as exc:
        return FileResult(filename=filename, ok=False, reason=str(exc))

    # 1) 标记需要移除的条目（空文本 / 时间颠倒）。
    #    零时长条目（开始与结束相同）属于合法区间，不在此处移除，避免丢文本。
    for entry in entries:
        if not any(line.strip() for line in entry.text_lines):
            entry.removed = True
            entry.removal_reasons.append("空文本")
        if entry.end_ms < entry.start_ms:
            entry.removed = True
            entry.removal_reasons.append("时间颠倒（结束早于开始）")

    kept = [entry for entry in entries if not entry.removed]

    # 2) 消除剩余条目的重叠。
    resolve_overlaps(kept)

    # 3) 检查时间书写格式是否规范（例如毫秒不足三位）。
    for entry in kept:
        expected = canonical_time_line(
            entry.orig_start_ms, entry.orig_end_ms, entry.tail
        )
        if entry.raw_time_line != expected:
            _add_once(entry.change_types, "时间格式规范化")

    # 4) 序号从 1 起连续重排。
    numbering_changed = any(
        entry.declared_index != new_index
        for new_index, entry in enumerate(kept, 1)
    )

    result = FileResult(
        filename=filename,
        ok=True,
        input_count=len(entries),
        output_count=len(kept),
        has_correction=bool(
            numbering_changed
            or had_bom
            or actual_encoding is not None
            or any(entry.removed or entry.change_types for entry in entries)
        ),
        corrected_entries=sum(
            1 for entry in entries if entry.removed or entry.change_types
        ),
    )

    if numbering_changed:
        original_seq = [
            "无" if entry.declared_index is None else str(entry.declared_index)
            for entry in kept
        ]
        new_seq = [str(index) for index in range(1, len(kept) + 1)]
        result.extra_notes.append(
            "  序号规范化: 原序号 [{}] -> 输出序号 [{}]".format(
                ", ".join(original_seq) if original_seq else "",
                ", ".join(new_seq) if new_seq else "",
            )
        )

    if actual_encoding is not None:
        result.extra_notes.append(
            f"  编码转换: {actual_encoding} -> UTF-8（输出不带 BOM）"
        )
    if had_bom:
        result.extra_notes.append("  BOM 处理: 已移除 UTF-8 BOM（输出首行无 BOM）")

    # 5) 生成修正明细（按原文件位置排序）。
    for entry in sorted(entries, key=lambda item: item.position):
        if entry.removed:
            result.detail_lines.append(f"  - {_entry_label(entry)}: 已移除")
            result.detail_lines.append(f"      原时间: {entry.raw_time_line}")
            result.detail_lines.append(
                "      修改类型: " + "；".join(entry.removal_reasons)
            )
            continue

        time_changed = (
            entry.start_ms != entry.orig_start_ms
            or entry.end_ms != entry.orig_end_ms
        )
        if not time_changed and not entry.change_types:
            continue

        result.detail_lines.append(f"  - {_entry_label(entry)}: 时间已修正")
        result.detail_lines.append(f"      原时间: {entry.raw_time_line}")
        result.detail_lines.append(
            f"      新时间: {format_ms(entry.start_ms)} --> {format_ms(entry.end_ms)}"
        )
        result.detail_lines.append(
            "      修改类型: " + "；".join(entry.change_types)
        )
        if entry.adjust_targets:
            result.detail_lines.append(
                "      调整对象: " + "；".join(entry.adjust_targets)
            )

    result.output_text = render_srt(kept, newline=original_newline)
    return result


def render_srt(entries: list[Entry], newline: str = "\n") -> str:
    """把条目渲染为可再次导入剪辑软件的 SRT 文本（UTF-8、无 BOM）。

    多行文本按原样使用换行符连接；行尾风格跟随原文件（默认 LF）。
    """
    blocks: list[str] = []
    for index, entry in enumerate(entries, 1):
        time_line = canonical_time_line(entry.start_ms, entry.end_ms, entry.tail)
        blocks.append(newline.join([str(index), time_line, entry.text]))
    if not blocks:
        return ""
    return (newline + newline).join(blocks) + newline


def build_report(
    input_dir: Path,
    output_dir: Path,
    results: list[FileResult],
) -> str:
    success = [result for result in results if result.ok]
    corrected = [result for result in success if result.has_correction]
    failed = [result for result in results if not result.ok]

    lines: list[str] = [
        "SRT 批量修正记录",
        "=" * 64,
        f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"输入目录: {input_dir}",
        f"输出目录: {output_dir}",
        "",
    ]

    for result in results:
        lines.append(f"===== 文件: {result.filename} =====")
        if not result.ok:
            lines.append("状态: 未能处理（无法解析为 SRT）")
            lines.append(f"原因: {result.reason}")
            lines.append("")
            continue

        if result.has_correction:
            lines.append("状态: 已输出（含修正）")
        else:
            lines.append("状态: 已输出（无需修正）")
        lines.append(f"输入条目数: {result.input_count}")
        lines.append(f"输出条目数: {result.output_count}")

        if not result.has_correction:
            lines.append("修改明细: 无（该文件无需修正，内容保持不变）")
        else:
            if result.corrected_entries:
                lines.append(f"修正条目数: {result.corrected_entries}")
            else:
                lines.append("修正条目数: 0（仅文件级修正）")
            lines.append("修改明细:")
            for note in result.extra_notes:
                lines.append(note)
            if result.detail_lines:
                lines.extend(result.detail_lines)
            elif not result.extra_notes:
                lines.append("  （仅序号规范化）")
        lines.append("")

    lines.append("=" * 64)
    lines.append(
        f"汇总: 成功文件数: {len(success)}, "
        f"含修正文件数: {len(corrected)}, "
        f"未能处理文件数: {len(failed)}"
    )
    return "\n".join(lines) + "\n"


def process_directory(input_dir: Path, output_dir: Path) -> int:
    """处理目录并写报告。仅在参数合法时调用。"""
    output_dir.mkdir(parents=True, exist_ok=True)

    sources = sorted(
        (
            path
            for path in input_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".srt"
        ),
        key=lambda path: path.name.lower(),
    )

    results: list[FileResult] = []
    for source in sources:
        try:
            result = process_srt_file(source)
        except Exception as exc:  # noqa: BLE001 - 单个文件异常不得中断整批
            result = FileResult(
                filename=source.name,
                ok=False,
                reason=f"处理时发生异常: {exc}",
            )
        if result.ok:
            try:
                with open(
                    output_dir / source.name,
                    "w",
                    encoding="utf-8",
                    newline="\n",
                ) as handle:
                    handle.write(result.output_text)
            except OSError as exc:
                result = FileResult(
                    filename=source.name,
                    ok=False,
                    reason=f"写入输出文件失败: {exc}",
                )
        results.append(result)

    report = build_report(input_dir, output_dir, results)
    report_path = output_dir / REPORT_FILENAME
    with open(report_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(report)

    success_count = sum(1 for result in results if result.ok)
    corrected_count = sum(
        1 for result in results if result.ok and result.has_correction
    )
    failed_count = len(results) - success_count

    print(
        f"处理完成: 成功 {success_count} 个, "
        f"含修正 {corrected_count} 个, 未能处理 {failed_count} 个"
    )
    print(f"修正记录: {report_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "批量读取 SRT 字幕，消除时间轴重叠、清理空文本/颠倒条目，"
            "统一时间格式与序号，并生成修正记录。原文件保持不变。"
        )
    )
    parser.add_argument("input_dir", help="输入目录（处理其中全部 .srt 文件）")
    parser.add_argument("output_dir", help="输出目录（不存在时自动创建）")
    args = parser.parse_args(argv)

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)

    # 先校验输入，再创建输出目录；输入无效时不得留下输出目录。
    if not input_dir.exists():
        print(f"错误: 输入路径不存在: {input_dir}", file=sys.stderr)
        return 1
    if not input_dir.is_dir():
        print(f"错误: 输入路径不是目录: {input_dir}", file=sys.stderr)
        return 1
    if output_dir.exists() and not output_dir.is_dir():
        print(f"错误: 输出路径已存在且不是目录: {output_dir}", file=sys.stderr)
        return 1
    try:
        if input_dir.resolve() == output_dir.resolve():
            print(
                "错误: 输入目录与输出目录不能相同（必须保留原文件）",
                file=sys.stderr,
            )
            return 1
    except OSError:
        pass

    try:
        return process_directory(input_dir, output_dir)
    except OSError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
