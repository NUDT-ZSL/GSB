#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""过场动画音画同步核对工具（仅使用 Python 标准库，要求 Python 3.10+）。

输入：
  1. 音频提示点表 JSON（标记数组，每项含 name / time，time 为整数毫秒）
  2. 引擎事件表 JSON（结构同上）
  3. 配置 JSON（tolerance_ms 容差，缺省 80；total_duration_ms 总时长）

输出（写入 --outdir）：
  - corrections.json  校正清单（中文键，按偏移绝对值降序、名称升序）
  - result.json       完整结构化结果（含未匹配、不可核对、顺序异常等）
  - sync_report.txt   可读同步报告

任何情况下退出码均为 0（含两份输入都无法读取的场景）。
本工具只读输入文件，不会修改它们；输出不含时间戳，重复运行逐字节一致。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TOLERANCE_MS = 80

# 输出中的来源标签
SRC_AUDIO = "音频"
SRC_ENGINE = "引擎"


@dataclass
class Record:
    """通过结构校验、参与后续规则判断的一条标记。"""

    index: int  # 在原文件数组中的下标（0 起）
    name: str
    time: int


@dataclass
class FileResult:
    source: str
    path: str
    readable: bool = False
    error: str | None = None
    records: list[Record] = None  # 结构合法的全部标记（含之后被判越界/重复者）
    survivors: list[Record] = None  # 实际参与名称比对的标记
    uncheckable: list[dict[str, Any]] = None  # 不可核对条目
    order_anomalies: list[dict[str, Any]] = None  # 顺序异常条目

    def __post_init__(self) -> None:
        self.records = self.records or []
        self.survivors = self.survivors or []
        self.uncheckable = self.uncheckable or []
        self.order_anomalies = self.order_anomalies or []


def _is_plain_int(value: Any) -> bool:
    """time 只接受真正的 int；bool 是 int 的子类，必须排除。"""
    return isinstance(value, int) and not isinstance(value, bool)


def load_config(path: Path) -> tuple[int, int | None, str | None]:
    """读取配置。返回 (容差毫秒, 总时长毫秒或None, 备注)。

    文件缺失或字段非法时回退缺省值，并把原因放入备注供报告说明。
    """
    tolerance = DEFAULT_TOLERANCE_MS
    total: int | None = None
    note: str | None = None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return tolerance, total, f"配置文件不存在，使用缺省容差 {DEFAULT_TOLERANCE_MS} 毫秒"
    except (OSError, json.JSONDecodeError) as exc:
        return tolerance, total, f"配置文件读取失败（{exc}），使用缺省容差 {DEFAULT_TOLERANCE_MS} 毫秒"
    if not isinstance(raw, dict):
        return tolerance, total, "配置文件顶层不是对象，全部使用缺省值"

    tol = raw.get("tolerance_ms", DEFAULT_TOLERANCE_MS)
    if _is_plain_int(tol) and tol >= 0:
        tolerance = tol
    else:
        note = f"tolerance_ms 非法（{tol!r}），使用缺省值 {DEFAULT_TOLERANCE_MS} 毫秒"

    if "total_duration_ms" in raw:
        dur = raw["total_duration_ms"]
        if _is_plain_int(dur) and dur >= 0:
            total = dur
        else:
            note = (note + "；" if note else "") + f"total_duration_ms 非法（{dur!r}），不做时间上限检查"
    return tolerance, total, note


def process_file(source: str, path: Path, total_duration: int | None) -> FileResult:
    """读取并按规则 2/3/7/8 处理单份标记表。"""
    result = FileResult(source=source, path=str(path))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        result.readable = False
        result.error = "文件不存在"
        return result
    except OSError as exc:
        result.readable = False
        result.error = f"文件读取失败：{exc}"
        return result
    except json.JSONDecodeError as exc:
        result.readable = False
        result.error = f"JSON 解析失败：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）"
        return result

    result.readable = True
    if not isinstance(raw, list):
        result.readable = False
        result.error = "顶层结构不是标记数组"
        return result

    # ---- 规则 8：逐条结构校验，坏记录跳过且不影响其他记录 ----
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            result.uncheckable.append(
                {"source": source, "index": index, "name": None, "reason": "记录不是 JSON 对象"}
            )
            continue

        if "name" not in item:
            result.uncheckable.append(
                {"source": source, "index": index, "name": None, "reason": "缺少名称字段 name"}
            )
            continue
        name = item["name"]
        if not isinstance(name, str) or not name.strip():
            result.uncheckable.append(
                {"source": source, "index": index, "name": None,
                 "reason": f"名称字段缺失或类型错误（{name!r}），要求非空字符串"}
            )
            continue

        if "time" not in item:
            result.uncheckable.append(
                {"source": source, "index": index, "name": name, "reason": "缺少时间字段 time"}
            )
            continue
        time_value = item["time"]
        if not _is_plain_int(time_value):
            result.uncheckable.append(
                {"source": source, "index": index, "name": name,
                 "reason": f"时间字段类型错误（{time_value!r}），要求整数毫秒"}
            )
            continue

        result.records.append(Record(index=index, name=name, time=time_value))

    # ---- 规则 2：结构合法标记按原顺序检查单调不减 ----
    previous: Record | None = None
    for record in result.records:
        if previous is not None and record.time < previous.time:
            result.order_anomalies.append(
                {
                    "source": source,
                    "index": record.index,
                    "name": record.name,
                    "time": record.time,
                    "previous_time": previous.time,
                }
            )
        previous = record

    # ---- 规则 7：越界标记列入不可核对，且不影响其他标记 ----
    in_bounds: list[Record] = []
    for record in result.records:
        if record.time < 0 or (total_duration is not None and record.time > total_duration):
            limit = "0 ~ " + str(total_duration) if total_duration is not None else ">= 0"
            result.uncheckable.append(
                {
                    "source": source,
                    "index": record.index,
                    "name": record.name,
                    "reason": f"时间越界（time={record.time}，允许范围 {limit}）",
                }
            )
        else:
            in_bounds.append(record)

    # ---- 规则 3：同名保留首个出现记录，其余列为不可核对 ----
    seen: set[str] = set()
    for record in in_bounds:
        if record.name in seen:
            result.uncheckable.append(
                {
                    "source": source,
                    "index": record.index,
                    "name": record.name,
                    "reason": "名称重复，仅保留首个出现记录参与比对",
                }
            )
        else:
            seen.add(record.name)
            result.survivors.append(record)

    # 不可核对列表保持稳定顺序：来源内按原文件下标
    result.uncheckable.sort(key=lambda x: x["index"])
    return result


def merge_and_compare(
    audio: FileResult, engine: FileResult, tolerance: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """规则 4/5/6：按名称合并、算偏移、划分校正清单与未匹配列表。"""
    audio_map = {r.name: r for r in audio.survivors} if audio.readable else {}
    engine_map = {r.name: r for r in engine.survivors} if engine.readable else {}

    corrections: list[dict[str, Any]] = []
    for name in sorted(set(audio_map) & set(engine_map)):
        a = audio_map[name]
        e = engine_map[name]
        offset = e.time - a.time  # 规则 4：引擎时间减音频时间
        if abs(offset) > tolerance:  # 规则 5：绝对值“超过”阈值才校正
            corrections.append(
                {
                    "名称": name,
                    "音频时间": a.time,
                    "引擎时间": e.time,
                    "偏移量": offset,
                }
            )

    # 规则 9：偏移绝对值降序，同值按名称升序
    corrections.sort(key=lambda item: (-abs(item["偏移量"]), item["名称"]))

    unmatched: list[dict[str, Any]] = []
    for name in sorted(set(audio_map) - set(engine_map)):
        unmatched.append({"source": SRC_AUDIO, "name": name, "time": audio_map[name].time})
    for name in sorted(set(engine_map) - set(audio_map)):
        unmatched.append({"source": SRC_ENGINE, "name": name, "time": engine_map[name].time})
    return corrections, unmatched


def build_report(
    audio: FileResult,
    engine: FileResult,
    tolerance: int,
    total_duration: int | None,
    config_note: str | None,
    corrections: list[dict[str, Any]],
    unmatched: list[dict[str, Any]],
) -> str:
    """规则 10：生成可读报告，空分类明确写“无”。"""
    lines: list[str] = []
    lines.append("音画同步核对报告")
    lines.append("=" * 64)
    lines.append("")
    lines.append("配置")
    lines.append(f"  容差阈值：{tolerance} 毫秒（偏移绝对值超过该值才进入校正清单）")
    if total_duration is not None:
        lines.append(f"  总时长：{total_duration} 毫秒（允许时间范围 0 ~ {total_duration}）")
    else:
        lines.append("  总时长：未配置（不做时间上限检查）")
    if config_note:
        lines.append(f"  配置备注：{config_note}")
    lines.append("")

    lines.append("输入文件")
    for result in (audio, engine):
        if result.readable:
            lines.append(
                f"  [{result.source}] {result.path}：读取成功（结构有效记录 {len(result.records)} 条，"
                f"参与比对 {len(result.survivors)} 条）"
            )
        else:
            lines.append(f"  [{result.source}] {result.path}：读取失败——{result.error}")
    lines.append("")

    read_errors = [r for r in (audio, engine) if not r.readable]
    lines.append("读取失败说明")
    if read_errors:
        for result in read_errors:
            lines.append(f"  - [{result.source}] {result.path} 无法读取，原因：{result.error}")
    else:
        lines.append("  无")
    lines.append("")

    lines.append("一、校正清单（按偏移绝对值降序，同值按名称升序）")
    if corrections:
        for pos, item in enumerate(corrections, start=1):
            lines.append(
                f"  {pos}. 名称={item['名称']}，音频时间={item['音频时间']} 毫秒，"
                f"引擎时间={item['引擎时间']} 毫秒，偏移={item['偏移量']:+d} 毫秒"
            )
    else:
        lines.append("  无")
    lines.append("")

    lines.append("二、未匹配标记（仅单侧出现，不参与偏移计算）")
    if unmatched:
        for item in unmatched:
            lines.append(f"  - [{item['source']}] 名称={item['name']}，时间={item['time']} 毫秒")
    else:
        lines.append("  无")
    lines.append("")

    lines.append("三、不可核对标记（重复、越界或字段错误，不参与比对）")
    uncheckable = audio.uncheckable + engine.uncheckable
    if uncheckable:
        for item in uncheckable:
            label = f"名称={item['name']}" if item["name"] is not None else "名称未知"
            lines.append(
                f"  - [{item['source']}] 第 {item['index'] + 1} 条（{label}）：{item['reason']}"
            )
    else:
        lines.append("  无")
    lines.append("")

    lines.append("四、顺序异常标记（文件内时间非单调不减，仅提示不影响比对资格）")
    anomalies = audio.order_anomalies + engine.order_anomalies
    if anomalies:
        for item in anomalies:
            lines.append(
                f"  - [{item['source']}] 第 {item['index'] + 1} 条 名称={item['name']}："
                f"时间 {item['time']} 小于前一条时间 {item['previous_time']}"
            )
    else:
        lines.append("  无")
    lines.append("")
    return "\n".join(lines)


def write_text(path: Path, content: str) -> None:
    """统一 UTF-8、LF 行尾、末尾单个换行，保证跨平台逐字节一致。"""
    if not content.endswith("\n"):
        content += "\n"
    path.write_text(content, encoding="utf-8", newline="\n")


def run(audio_path: Path, engine_path: Path, config_path: Path, outdir: Path) -> int:
    tolerance, total_duration, config_note = load_config(config_path)
    audio = process_file(SRC_AUDIO, audio_path, total_duration)
    engine = process_file(SRC_ENGINE, engine_path, total_duration)
    corrections, unmatched = merge_and_compare(audio, engine, tolerance)
    report = build_report(audio, engine, tolerance, total_duration, config_note, corrections, unmatched)

    full_result = {
        "config": {
            "tolerance_ms": tolerance,
            "total_duration_ms": total_duration,
            "config_note": config_note,
        },
        "files": {
            "audio": {
                "path": str(audio.path),
                "readable": audio.readable,
                "error": audio.error,
                "valid_records": len(audio.records),
                "compared_records": len(audio.survivors),
            },
            "engine": {
                "path": str(engine.path),
                "readable": engine.readable,
                "error": engine.error,
                "valid_records": len(engine.records),
                "compared_records": len(engine.survivors),
            },
        },
        "order_anomalies": audio.order_anomalies + engine.order_anomalies,
        "corrections": corrections,
        "unmatched": unmatched,
        "uncheckable": audio.uncheckable + engine.uncheckable,
    }

    outdir.mkdir(parents=True, exist_ok=True)
    write_text(
        outdir / "corrections.json",
        json.dumps(corrections, ensure_ascii=False, indent=2) + "\n",
    )
    write_text(
        outdir / "result.json",
        json.dumps(full_result, ensure_ascii=False, indent=2) + "\n",
    )
    write_text(outdir / "sync_report.txt", report)
    return 0


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="过场动画音画同步核对工具")
    parser.add_argument("--audio", type=Path, default=root / "samples" / "main" / "audio_cues.json",
                        help="音频提示点表 JSON 路径")
    parser.add_argument("--engine", type=Path, default=root / "samples" / "main" / "engine_events.json",
                        help="引擎事件表 JSON 路径")
    parser.add_argument("--config", type=Path, default=root / "config" / "config.sample.json",
                        help="配置 JSON 路径（容差/总时长）")
    parser.add_argument("--outdir", type=Path, default=root / "output",
                        help="输出目录（自动创建）")
    args = parser.parse_args(argv)
    return run(args.audio, args.engine, args.config, args.outdir)


if __name__ == "__main__":
    sys.exit(main())
