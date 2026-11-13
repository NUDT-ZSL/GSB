# -*- coding: utf-8 -*-
"""播客双轨单声道剪辑合成工具（仅使用 Python 标准库）。

功能概述
--------
1. 读取主机位、嘉宾位两段单声道 16-bit PCM WAV（同一时间轴）。
2. 读取 CSV 剪辑清单，逐行校验；非法行（空行跳过）记录行号与原因。
3. 同一轨道上的重叠/相邻裁剪区间先合并，重叠部分只处理一次。
4. 两轨裁剪结果按时间轴对齐，逐帧合成为单轨；同一时刻两轨均有内容时
   默认取主机位（主机位优先），两轨都被裁掉的帧不进入成片。
5. 输出单声道 16-bit PCM WAV 成片与 UTF-8 处理报告，全程不修改源文件。

清单语义：CSV 中的区间是“要裁掉（静音/口误）”的区间，单位秒，
区间为左闭右开 [start, end)；清单之外的内容予以保留并拼接。

退出码约定
----------
0  正常结束（含“全部无效、仅输出报告”的空结果情况）
1  输入不满足硬性约束（采样率不一致、WAV 格式不支持、文件无法读取等）
2  命令行参数缺失或用法错误（argparse 标准行为，不创建任何文件）
"""

from __future__ import annotations

import argparse
import array
import csv
import io
import math
import os
import sys
import wave
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 常量与数据结构
# ---------------------------------------------------------------------------

# 轨道标识约定取值：中文（主机位/嘉宾位）与英文别名（host/guest）等价。
TRACK_ALIASES: Dict[str, str] = {
    "主机位": "host",
    "host": "host",
    "嘉宾位": "guest",
    "guest": "guest",
}
TRACK_CN = {"host": "主机位", "guest": "嘉宾位"}
CUT_INTERVAL = Tuple[int, int]  # 帧级裁剪区间（左闭右开）

EXIT_OK = 0
EXIT_INPUT_ERROR = 1
EXIT_USAGE = 2


class InputError(Exception):
    """输入违反硬性约束（采样率不匹配、WAV 格式不支持等）。"""


@dataclass(frozen=True)
class WavTrack:
    """已加载的一条单声道 16-bit PCM 音轨。"""

    label: str                      # host / guest
    path: str
    frame_rate: int
    n_frames: int
    samples: array.array            # 有符号 16-bit 采样数组（主机字节序）


@dataclass(frozen=True)
class EditRow:
    """通过全部校验的一行清单。"""

    line_no: int
    track: str                      # host / guest
    start_frame: int
    end_frame: int


@dataclass(frozen=True)
class RejectedRow:
    """被拒绝的一行清单：物理行号 + 中文原因。"""

    line_no: int
    reason: str


@dataclass
class ParseResult:
    accepted: List[EditRow]
    rejected: List[RejectedRow]
    skipped_blank: int


# ---------------------------------------------------------------------------
# WAV 读取与校验
# ---------------------------------------------------------------------------

def load_wav_track(path: str, label: str) -> WavTrack:
    """读取并校验一条 WAV 音轨。

    仅接受：单声道（nchannels == 1）、16-bit（sampwidth == 2）、
    未压缩 PCM（comptype == 'NONE'）。任一条件不满足即整体拒绝。
    """
    try:
        with wave.open(path, "rb") as wf:
            n_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            frame_rate = wf.getframerate()
            comp_type = wf.getcomptype()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
    except (wave.Error, EOFError, OSError) as exc:
        raise InputError(
            f"{TRACK_CN[label]} WAV 无法读取（{path}）：{exc}"
        ) from exc

    problems = []
    if n_channels != 1:
        problems.append(f"声道数为 {n_channels}，要求单声道（1）")
    if sample_width != 2:
        problems.append(
            f"采样宽度为 {sample_width * 8} 位，要求 16 位 PCM"
        )
    if comp_type != "NONE":
        problems.append(f"存在压缩（{comp_type}），要求未压缩 PCM")
    if problems:
        raise InputError(
            f"{TRACK_CN[label]} WAV 输入格式不受支持（{path}）：" + "；".join(problems)
        )

    samples = array.array("h")
    samples.frombytes(raw)
    # wave 模块按小端返回；在大端机器上转换为本机有符号 short。
    if sys.byteorder == "big":
        samples.byteswap()

    return WavTrack(
        label=label,
        path=path,
        frame_rate=frame_rate,
        n_frames=n_frames,
        samples=samples,
    )


def check_common_rate(tracks: Sequence[WavTrack]) -> int:
    """两轨采样率必须完全一致，否则明确拒绝。"""
    rates = {t.frame_rate for t in tracks}
    if len(rates) != 1:
        detail = "；".join(
            f"{TRACK_CN[t.label]} {t.frame_rate} Hz（{os.path.basename(t.path)}）"
            for t in tracks
        )
        raise InputError(
            "两轨采样率不匹配，无法按同一时间轴对齐：" + detail
            + "。请先重采样为一致采样率后再处理。"
        )
    return tracks[0].frame_rate


# ---------------------------------------------------------------------------
# CSV 清单解析与逐行校验
# ---------------------------------------------------------------------------

def parse_edit_csv(
    csv_path: str, available_tracks: Sequence[str], frame_rate: int
) -> ParseResult:
    """逐行解析剪辑清单。

    - 空行（含仅空白字符）直接跳过，不计入拒绝列表；
    - 首行若为表头（字段名为 track,start,end，大小写不敏感）则跳过；
    - 每行必须恰好 3 个字段，数值合法、区间合法、轨道合法且该轨存在；
    - 被拒行保留物理行号（从 1 开始）与中文原因；
    - 秒 → 帧：起点 floor(秒×采样率)、终点 ceil(秒×采样率)，
      保证请求覆盖的所有采样帧都被裁掉。
    """
    try:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
            text = fh.read()
    except OSError as exc:
        raise InputError(f"剪辑清单无法读取（{csv_path}）：{exc}") from exc

    accepted: List[EditRow] = []
    rejected: List[RejectedRow] = []
    skipped_blank = 0
    available = set(available_tracks)

    # 必须按物理行逐行读取：csv.reader 会吞掉空行，导致行号错位。
    header_seen = False
    for line_no, raw_line in enumerate(io.StringIO(text), start=1):
        if not raw_line.strip():
            skipped_blank += 1
            continue

        # 单行解析（每行一条记录），保留完整字段与引号处理能力。
        row = next(csv.reader(io.StringIO(raw_line)))

        # 仅把第一个非空行识别为表头。
        if not header_seen:
            header_seen = True
            lowered = [cell.strip().lower() for cell in row]
            if lowered[:3] == ["track", "start", "end"]:
                continue

        parsed = _validate_row(row, available)
        if isinstance(parsed, str):
            rejected.append(RejectedRow(line_no=line_no, reason=parsed))
            continue

        track, start_sec, end_sec = parsed
        start_frame = math.floor(start_sec * frame_rate)
        end_frame = math.ceil(end_sec * frame_rate)
        accepted.append(
            EditRow(
                line_no=line_no,
                track=track,
                start_frame=start_frame,
                end_frame=end_frame,
            )
        )

    return ParseResult(
        accepted=accepted, rejected=rejected, skipped_blank=skipped_blank
    )


def _validate_row(
    row: Sequence[str],
    available: set,
):
    """校验单行。

    合法时返回 (track, start_sec, end_sec)；非法时返回中文拒绝原因字符串。
    """
    if len(row) != 3:
        return f"字段数为 {len(row)}，要求恰好 3 个字段（track,start,end）"

    track_text, start_text, end_text = (cell.strip() for cell in row)

    track = TRACK_ALIASES.get(track_text)
    if track is None:
        return (
            f"未知轨道名“{track_text}”，仅允许约定取值："
            "主机位/host、嘉宾位/guest"
        )
    if track not in available:
        return f"轨道“{track_text}”在输入 WAV 中缺失，无法裁剪该轨"

    try:
        start_sec = float(start_text)
    except ValueError:
        return f"起点“{start_text}”不是合法数值"
    try:
        end_sec = float(end_text)
    except ValueError:
        return f"终点“{end_text}”不是合法数值"

    if not math.isfinite(start_sec) or not math.isfinite(end_sec):
        return "起止时间必须为有限数值"
    if start_sec < 0 or end_sec < 0:
        return "起止时间不允许为负数"
    if start_sec >= end_sec:
        return (
            f"区间倒置或为空：起点 {start_text}s 不小于终点 {end_text}s"
        )

    # 时长越界在知道各轨帧数后由 validate_bounds 补充判定。
    return track, start_sec, end_sec


def validate_bounds(
    parse_result: ParseResult, tracks: Sequence[WavTrack]
) -> Tuple[List[EditRow], List[RejectedRow]]:
    """补充校验：区间终点不得超出所引用轨道的实际时长。"""
    limits = {t.label: t.n_frames for t in tracks}
    accepted: List[EditRow] = []
    rejected = list(parse_result.rejected)
    for row in parse_result.accepted:
        limit = limits[row.track]
        if row.end_frame > limit:
            rejected.append(
                RejectedRow(
                    line_no=row.line_no,
                    reason=(
                        f"区间终点越界：第 {row.end_frame} 帧超出"
                        f"{TRACK_CN[row.track]}总时长 {limit} 帧"
                    ),
                )
            )
        else:
            accepted.append(row)
    rejected.sort(key=lambda item: item.line_no)
    return accepted, rejected


# ---------------------------------------------------------------------------
# 区间合并与双轨对齐合成
# ---------------------------------------------------------------------------

def merge_intervals(intervals: Sequence[CUT_INTERVAL]) -> List[CUT_INTERVAL]:
    """合并同一轨道上重叠或首尾相接的裁剪区间；重叠部分只保留一次。"""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: List[CUT_INTERVAL] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:  # 重叠或首尾相接 → 合并
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def cut_intervals_to_kept(
    cut_intervals: Sequence[CUT_INTERVAL], n_frames: int
) -> List[CUT_INTERVAL]:
    """把“要裁掉”的区间取补集，得到该轨保留的连续区间。"""
    kept: List[CUT_INTERVAL] = []
    cursor = 0
    for start, end in cut_intervals:
        if start > cursor:
            kept.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < n_frames:
        kept.append((cursor, n_frames))
    return kept


def compose_track(
    host: WavTrack,
    guest: Optional[WavTrack],
    kept_host: Sequence[CUT_INTERVAL],
    kept_guest: Sequence[CUT_INTERVAL],
) -> array.array:
    """按时间轴对齐合成单轨（双指针扫描，主机位优先）。

    不变量：kept_host / kept_guest 各自按起点升序、互不重叠。
    每轮取两条指针当前段中起点靠前的一条作为“先开始段”，再看另一条
    是否在其内部开始：
      - 先开始的是主机位 → 主机位整段优先，嘉宾同刻部分被覆盖；
      - 先开始的是嘉宾位 → 嘉宾只输出到主机位段起点（若有），主机位
        同刻优先；两轨同刻都被裁掉的帧不属于任何保留段，自然跳过。
    仅搬运已有样本、不做算术运算，16-bit 样本不可能溢出或回绕。
    """
    out = array.array("h")
    guest_samples = guest.samples if guest is not None else array.array("h")
    guest_limit = guest.n_frames if guest is not None else 0

    i = j = 0
    while i < len(kept_host) or j < len(kept_guest):
        h = kept_host[i] if i < len(kept_host) else None
        g = kept_guest[j] if j < len(kept_guest) else None

        if h is not None and (g is None or h[0] <= g[0]):
            # 主机位段先开始（或同起点）→ 主机位优先，整段输出。
            h_start, h_end = h
            out.extend(host.samples[h_start:h_end])
            i += 1
            # 嘉宾段若在主机位段结束前开始：同刻部分被主机位覆盖。
            while j < len(kept_guest) and kept_guest[j][0] < h_end:
                gs, ge = kept_guest[j]
                if ge > h_end:
                    # 嘉宾段延伸到主机位段之后：截断，保留后半截，
                    # 起点前移到 h_end（指针 j 不前进）。
                    kept_guest[j] = (h_end, ge)
                    break
                j += 1
        else:
            # 嘉宾位段先开始。
            g_start, g_end = g
            if h is not None and h[0] < g_end:
                # 主机位段在嘉宾段内部开始：嘉宾只输出到主机位起点，
                # 其余部分下一轮交给主机位优先逻辑处理。
                end_now = h[0]
                out.extend(guest_samples[g_start:min(end_now, guest_limit)])
                kept_guest[j] = (end_now, g_end)
            else:
                # 主机位在此段范围内无内容（被裁或已结束）→ 整段嘉宾。
                out.extend(guest_samples[g_start:min(g_end, guest_limit)])
                j += 1
    return out


# ---------------------------------------------------------------------------
# WAV 写出
# ---------------------------------------------------------------------------

def write_wav(path: str, samples: array.array, frame_rate: int) -> None:
    """写出单声道 16-bit PCM WAV（独立新文件，不触碰源文件）。"""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    payload = samples
    if sys.byteorder == "big":
        payload = array.array("h", samples)
        payload.byteswap()  # WAV 要求小端
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(frame_rate)
        wf.writeframes(payload.tobytes())


# ---------------------------------------------------------------------------
# 处理报告
# ---------------------------------------------------------------------------

def format_duration(n_frames: int, frame_rate: int) -> str:
    return f"{n_frames / frame_rate:.3f} 秒"


def build_report(
    tracks: Sequence[WavTrack],
    frame_rate: Optional[int],
    parse_result: ParseResult,
    rejected: Sequence[RejectedRow],
    merged_cuts: Dict[str, List[CUT_INTERVAL]],
    kept: Dict[str, List[CUT_INTERVAL]],
    output_frames: Optional[int],
    output_path: Optional[str],
    blank_skipped: int,
    no_valid_request: bool = False,
) -> str:
    """生成 UTF-8 文本报告；被拒请求按行号升序。"""
    lines: List[str] = []
    lines.append("播客双轨剪辑处理报告")
    lines.append("=" * 40)
    lines.append("")
    lines.append("【输入文件】")
    for t in tracks:
        lines.append(
            f"- {TRACK_CN[t.label]}：{t.path}"
            f"（{t.frame_rate} Hz，{t.n_frames} 帧，"
            f"时长 {format_duration(t.n_frames, t.frame_rate)}）"
        )
    if frame_rate is not None:
        lines.append(f"- 共同采样率：{frame_rate} Hz")
    lines.append("")

    lines.append("【清单校验】")
    lines.append(f"- 跳过空行：{blank_skipped} 行")
    lines.append(f"- 合法请求：{len(parse_result.accepted)} 行")
    lines.append(f"- 被拒请求：{len(rejected)} 行")
    lines.append("")

    lines.append("【被拒请求（按行号升序）】")
    if rejected:
        for item in rejected:
            lines.append(f"- 第 {item.line_no} 行：{item.reason}")
    else:
        lines.append("- 无")
    lines.append("")

    lines.append("【各轨裁剪情况（重叠区间已合并）】")
    for label in ("host", "guest"):
        if label not in merged_cuts:
            continue
        cuts = merged_cuts[label]
        kept_intervals = kept[label]
        cut_frames = sum(e - s for s, e in cuts)
        kept_frames = sum(e - s for s, e in kept_intervals)
        lines.append(
            f"- {TRACK_CN[label]}：合并后裁剪区间 {len(cuts)} 段，"
            f"裁掉 {cut_frames} 帧，保留 {kept_frames} 帧"
        )
        for idx, (s, e) in enumerate(cuts, start=1):
            lines.append(
                f"    裁剪 {idx}：第 {s} 帧 - 第 {e} 帧（左闭右开）"
            )
    lines.append("")

    lines.append("【成片统计】")
    if output_frames is None:
        if no_valid_request:
            lines.append(
                "- 未生成成片：无有效片段——剪辑清单全部请求被拒或清单为空。"
            )
        else:
            lines.append(
                "- 未生成成片：无有效片段——有效裁剪覆盖了全部音频，合成结果为空。"
            )
    else:
        # 注意：报告不写入输出路径，保证同输入重复运行时报告逐字节一致；
        # 成片路径由命令行 --output 决定并在控制台回显。
        lines.append(f"- 有效片段数：{len(kept.get('host', [])) + len(kept.get('guest', []))} 段")
        lines.append(
            f"- 成片总时长：{format_duration(output_frames, frame_rate)}"
            f"（{output_frames} 帧）"
        )
        lines.append("- 封装格式：单声道 16-bit PCM WAV")
    lines.append("")
    return "\n".join(lines)


def build_failure_report(reason: str, argv: Sequence[str]) -> str:
    """硬性输入约束失败时的报告（此时不产出成片）。"""
    lines = [
        "播客双轨剪辑处理报告",
        "=" * 40,
        "",
        "【处理结果】整体拒绝，未生成成片。",
        f"【原因】{reason}",
        "",
        "【命令行参数】" + (" ".join(argv) if argv else "（无）"),
        "",
    ]
    return "\n".join(lines)


def write_report(path: str, content: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="podcast_editor.py",
        description=(
            "播客双轨单声道 16-bit PCM WAV 剪辑合成工具：按 CSV 清单裁掉"
            "静音/口误区间，按时间轴对齐合成单轨成片并输出处理报告。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "清单 CSV 每行格式：track,start,end（区间单位秒，左闭右开）；"
            "track 仅允许 主机位/host、嘉宾位/guest；空行自动跳过。"
        ),
    )
    parser.add_argument("--host", required=True, help="主机位 WAV 路径（必填）")
    parser.add_argument("--guest", help="嘉宾位 WAV 路径（可选；缺失时嘉宾位清单行会被拒）")
    parser.add_argument("--csv", dest="csv_path", required=True, help="剪辑清单 CSV 路径（必填）")
    parser.add_argument("--output", required=True, help="成片 WAV 输出路径（必填）")
    parser.add_argument("--report", required=True, help="处理报告输出路径（必填，UTF-8 文本）")
    return parser


def ensure_distinct_outputs(host_path: str, guest_path: Optional[str],
                            output_path: str, report_path: str) -> None:
    """禁止输出路径覆盖任何源文件。"""
    sources = {os.path.abspath(host_path)}
    if guest_path:
        sources.add(os.path.abspath(guest_path))
    for out in (output_path, report_path):
        if os.path.abspath(out) in sources:
            raise InputError(f"输出路径与源文件相同，拒绝写入：{out}")


def run(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)  # 参数缺失时 argparse 自动退出码 2 并打印用法

    ensure_distinct_outputs(args.host, args.guest, args.output, args.report)

    try:
        host = load_wav_track(args.host, "host")
        tracks: List[WavTrack] = [host]
        if args.guest:
            guest = load_wav_track(args.guest, "guest")
            tracks.append(guest)
        else:
            guest = None
        frame_rate = check_common_rate(tracks)
        parse_result = parse_edit_csv(
            args.csv_path,
            available_tracks=[t.label for t in tracks],
            frame_rate=frame_rate,
        )
        accepted, rejected = validate_bounds(parse_result, tracks)
    except InputError as exc:
        report = build_failure_report(str(exc), argv if argv is not None else sys.argv[1:])
        write_report(args.report, report)
        print(f"[拒绝] {exc}", file=sys.stderr)
        print(f"[报告] {args.report}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    # 按轨汇总并合并裁剪区间（重叠只处理一次）。
    raw_cuts: Dict[str, List[CUT_INTERVAL]] = {"host": [], "guest": []}
    for row in accepted:
        raw_cuts[row.track].append((row.start_frame, row.end_frame))
    merged_cuts = {
        label: merge_intervals(intervals)
        for label, intervals in raw_cuts.items()
        if any(t.label == label for t in tracks)
    }
    kept: Dict[str, List[CUT_INTERVAL]] = {}
    for t in tracks:
        kept[t.label] = cut_intervals_to_kept(merged_cuts[t.label], t.n_frames)

    kept_guest = kept.get("guest", [])
    samples = compose_track(host, guest, kept["host"], kept_guest)

    # 空结果判定（满足任一即不出成片，仅输出报告）：
    #   1. 清单中没有任何通过校验的剪辑请求（全部无效/仅表头/仅空行）；
    #   2. 合法请求把全部音频内容裁掉，合成结果为零帧。
    no_valid_request = len(accepted) == 0
    if no_valid_request or len(samples) == 0:
        # 全部无效/全被裁掉：只输出报告，不生成成片。
        report = build_report(
            tracks=tracks,
            frame_rate=frame_rate,
            parse_result=parse_result,
            rejected=rejected,
            merged_cuts=merged_cuts,
            kept=kept,
            output_frames=None,
            output_path=None,
            blank_skipped=parse_result.skipped_blank,
            no_valid_request=no_valid_request,
        )
        write_report(args.report, report)
        print("[空结果] 无有效片段，未生成成片，仅输出报告。", file=sys.stderr)
        print(f"[报告] {args.report}", file=sys.stderr)
        return EXIT_OK

    write_wav(args.output, samples, frame_rate)
    report = build_report(
        tracks=tracks,
        frame_rate=frame_rate,
        parse_result=parse_result,
        rejected=rejected,
        merged_cuts=merged_cuts,
        kept=kept,
        output_frames=len(samples),
        output_path=args.output,
        blank_skipped=parse_result.skipped_blank,
    )
    write_report(args.report, report)
    print(
        f"[完成] 成片 {len(samples)} 帧（{format_duration(len(samples), frame_rate)}）"
        f" -> {args.output}"
    )
    print(f"[报告] {args.report}；被拒请求 {len(rejected)} 行。")
    return EXIT_OK


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(run())


if __name__ == "__main__":
    main()
