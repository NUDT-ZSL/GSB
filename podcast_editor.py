#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""播客后期命令行工具。

按 JSON 编辑脚本对单声道 16bit PCM WAV 做片段截取、顺序拼接、
首尾淡入淡出与整体统一音量调整，导出 WAV 与 JSON 处理报告。

用法:
    python podcast_editor.py <输入WAV> <编辑脚本JSON> <输出WAV> <报告JSON>

设计要点:
- 仅使用 Python 标准库 (wave / json / struct / array)，全程离线、确定性输出。
- 任何片段级错误 (越界、时长为零、淡变超标等) 只拒绝该片段，不影响其他片段。
- 输入 WAV / 编辑脚本本身不合法时，在写出任何文件之前失败，不留残缺输出。
"""

from __future__ import annotations

import array
import json
import math
import sys
import wave

# Windows 中文区域默认管道编码为 GBK，统一改为 UTF-8 保证输出可移植
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# 音量档位: 百分比整数，100 为默认 (原始幅度)，区间 [0, 200]
DEFAULT_VOLUME = 100
MIN_VOLUME = 0
MAX_VOLUME = 200

USAGE = (
    "用法: python podcast_editor.py <输入WAV> <编辑脚本JSON> "
    "<输出WAV> <报告JSON>\n"
    "参数 (均为必填):\n"
    "  输入WAV       单声道 16bit PCM WAV 文件路径\n"
    "  编辑脚本JSON  含 segments 片段列表与 volume 整体音量档位的 JSON\n"
    "  输出WAV       导出 WAV 的目标路径\n"
    "  报告JSON      处理报告的目标路径"
)


class EditorError(Exception):
    """可向用户展示的处理错误 (非法输入、格式不兼容等)。"""


# ----------------------------------------------------------------------
# 输入 WAV 读取与校验
# ----------------------------------------------------------------------
def read_mono_pcm16_wav(path: str) -> tuple[array.array, int]:
    """读取单声道 16bit PCM WAV，返回 (样本数组, 采样率)。

    任何不兼容情况抛出 EditorError，并在消息中说明具体原因。
    """
    try:
        wf = wave.open(path, "rb")
    except FileNotFoundError as exc:
        raise EditorError(f"输入 WAV 文件不存在或不可读取: {path}") from exc
    except wave.Error as exc:
        raise EditorError(f"输入 WAV 文件无法解析 ({path}): {exc}") from exc
    except OSError as exc:
        # 权限不足、路径是目录、文件被占用等
        raise EditorError(f"输入 WAV 文件不可读取 ({path}): {exc}") from exc

    try:
        nchannels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        comptype = wf.getcomptype()
        nframes = wf.getnframes()
        raw = wf.readframes(nframes)
    except (wave.Error, EOFError, OSError) as exc:
        raise EditorError(f"输入 WAV 文件内容损坏 ({path}): {exc}") from exc
    finally:
        wf.close()

    if comptype != "NONE":
        raise EditorError(
            f"输入 WAV 格式不兼容 ({path}): 使用了压缩格式 {comptype!r}，"
            "本工具仅支持未压缩 PCM (comptype=NONE)"
        )
    if nchannels != 1:
        raise EditorError(
            f"输入 WAV 声道不兼容 ({path}): 检测到 {nchannels} 声道，"
            "本工具仅支持单声道 (1 声道) WAV"
        )
    if sampwidth != 2:
        raise EditorError(
            f"输入 WAV 采样位宽不兼容 ({path}): 检测到 {sampwidth * 8}bit，"
            "本工具仅支持 16bit PCM WAV"
        )
    if framerate <= 0:
        raise EditorError(
            f"输入 WAV 采样率不兼容 ({path}): 采样率 {framerate} 非法"
        )

    try:
        samples = array.array("h")
        samples.frombytes(raw)
    except ValueError as exc:
        raise EditorError(
            f"输入 WAV 数据区长度不是 16bit 样本的整数倍 ({path})"
        ) from exc

    # 非小端平台上 array 是原生字节序，统一换算保证跨平台一致
    if sys.byteorder == "big":
        samples.byteswap()

    if len(samples) != nframes:
        raise EditorError(
            f"输入 WAV 文件头与数据不一致 ({path}): 头声明 {nframes} 帧，"
            f"实际解码 {len(samples)} 帧"
        )
    return samples, framerate


# ----------------------------------------------------------------------
# 编辑脚本读取与校验
# ----------------------------------------------------------------------
def load_script(path: str) -> dict:
    """读取并做结构性校验，返回解析后的 dict。

    片段级字段的语义问题由处理流程逐条拒绝；这里只保证顶层结构可用。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError as exc:
        raise EditorError(f"编辑脚本文件不存在或不可读取: {path}") from exc
    except OSError as exc:
        raise EditorError(f"编辑脚本文件不可读取 ({path}): {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EditorError(
            f"编辑脚本不是合法 JSON ({path}): 第 {exc.lineno} 行 "
            f"第 {exc.colno} 列附近解析失败 - {exc.msg}"
        ) from exc

    if not isinstance(data, dict):
        raise EditorError(
            f"编辑脚本结构非法 ({path}): 顶层必须是 JSON 对象，"
            f"实际为 {type(data).__name__}"
        )
    if "segments" not in data:
        raise EditorError(f"编辑脚本缺少必要字段: 'segments' ({path})")
    if not isinstance(data["segments"], list):
        raise EditorError(
            f"编辑脚本字段类型错误: 'segments' 必须是数组 ({path})"
        )

    volume = data.get("volume", DEFAULT_VOLUME)
    if isinstance(volume, bool) or not isinstance(volume, int):
        raise EditorError(
            f"编辑脚本字段类型错误: 'volume' 必须是整数 (百分比)，"
            f"实际为 {type(volume).__name__} ({path})"
        )
    return data


def validate_volume(volume: int) -> tuple[int, bool, str | None]:
    """返回 (实际采用档位, 是否合法, 非法原因)。非法时回退默认档位。"""
    if MIN_VOLUME <= volume <= MAX_VOLUME:
        return volume, True, None
    reason = (
        f"音量档位非法: {volume} 不在允许区间 [{MIN_VOLUME}, {MAX_VOLUME}]，"
        f"已按默认档位 {DEFAULT_VOLUME} 导出"
    )
    return DEFAULT_VOLUME, False, reason


def _is_finite_number(value) -> bool:
    """bool 不是合法数值；JSON 数字转 float 后必须有限。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


# ----------------------------------------------------------------------
# 片段处理
# ----------------------------------------------------------------------
def _ms_to_frames(ms: float, framerate: int) -> int:
    """毫秒换算为采样帧数，四舍五入 (Banker's Rounding)，结果确定。"""
    return int(round(ms * framerate / 1000.0))


def process_segments(
    source: array.array,
    framerate: int,
    segments: list,
) -> tuple[array.array, list[dict], list[dict]]:
    """按顺序处理片段，返回 (拼接后的样本, 成功列表, 拒绝列表)。

    成功/拒绝列表中的 index 均为片段在脚本中的原始序号 (从 0 开始)，
    两个列表最终都按该序号升序，与处理顺序一致。
    """
    total_frames = len(source)
    output = array.array("h")
    accepted: list[dict] = []
    rejected: list[dict] = []

    for index, seg in enumerate(segments):
        if not isinstance(seg, dict):
            rejected.append(
                {"index": index, "reason": "片段定义必须是 JSON 对象"}
            )
            continue

        start_ms = seg.get("start_ms")
        end_ms = seg.get("end_ms")
        if not _is_finite_number(start_ms) or not _is_finite_number(end_ms):
            rejected.append(
                {
                    "index": index,
                    "reason": "缺少或非法的 start_ms/end_ms 字段 "
                    "(必须是有限数字，单位毫秒)",
                }
            )
            continue
        if start_ms < 0 or end_ms < 0:
            rejected.append(
                {"index": index, "reason": "start_ms/end_ms 不得为负数"}
            )
            continue
        if end_ms <= start_ms:
            rejected.append(
                {
                    "index": index,
                    "reason": f"片段起止颠倒或时长为零 "
                    f"(start_ms={start_ms}, end_ms={end_ms})",
                }
            )
            continue

        start_frame = _ms_to_frames(start_ms, framerate)
        end_frame = _ms_to_frames(end_ms, framerate)

        if start_frame >= total_frames or end_frame > total_frames:
            rejected.append(
                {
                    "index": index,
                    "reason": f"片段范围超出输入音频 "
                    f"(请求帧 [{start_frame}, {end_frame})，"
                    f"输入共 {total_frames} 帧)",
                }
            )
            continue

        seg_frames = end_frame - start_frame
        if seg_frames <= 0:
            # 起止为正但四舍五入后落在同一帧，等价于零时长
            rejected.append(
                {"index": index, "reason": "片段时长为零 (换算后不足 1 个采样帧)"}
            )
            continue

        fade_in_ms = seg.get("fade_in_ms")
        fade_out_ms = seg.get("fade_out_ms")
        if fade_in_ms is not None and (
            not _is_finite_number(fade_in_ms) or fade_in_ms < 0
        ):
            rejected.append(
                {"index": index, "reason": "fade_in_ms 非法 (必须为 null 或非负数字)"}
            )
            continue
        if fade_out_ms is not None and (
            not _is_finite_number(fade_out_ms) or fade_out_ms < 0
        ):
            rejected.append(
                {"index": index, "reason": "fade_out_ms 非法 (必须为 null 或非负数字)"}
            )
            continue

        fade_in_frames = (
            0 if fade_in_ms is None else _ms_to_frames(fade_in_ms, framerate)
        )
        fade_out_frames = (
            0 if fade_out_ms is None else _ms_to_frames(fade_out_ms, framerate)
        )

        # 淡变合计 >= 片段时长会导致增益区间重叠 (负增益/互相抵消)，
        # 不允许靠钳制硬导出，直接拒绝该片段。
        if fade_in_frames + fade_out_frames >= seg_frames:
            rejected.append(
                {
                    "index": index,
                    "reason": f"淡入淡出合计 {fade_in_frames + fade_out_frames} 帧 "
                    f"不小于片段时长 {seg_frames} 帧，拒绝导出以避免负增益/失真",
                }
            )
            continue

        chunk = array.array("h", source[start_frame:end_frame])
        _apply_fades(chunk, fade_in_frames, fade_out_frames)
        output.extend(chunk)

        accepted.append(
            {
                "index": index,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "frames": seg_frames,
                "fade_in_frames": fade_in_frames,
                "fade_out_frames": fade_out_frames,
            }
        )

    # 序号升序 (处理本身即按序号，这里显式排序作为报告结构保证)
    accepted.sort(key=lambda item: item["index"])
    rejected.sort(key=lambda item: item["index"])
    return output, accepted, rejected


def _apply_fades(
    chunk: array.array, fade_in_frames: int, fade_out_frames: int
) -> None:
    """原地施加线性淡入/淡出。

    淡入: 第 0 帧增益 0，淡入区间末帧增益 1，区间内线性上升。
    淡出: 淡出区间首帧增益 1，最后一帧增益 0，区间内线性下降。
    增益恒在 [0, 1]，不会出现负增益或放大。
    """
    n = len(chunk)
    if fade_in_frames > 0:
        for i in range(fade_in_frames):
            gain = i / fade_in_frames  # i=0 -> 0.0
            chunk[i] = int(round(chunk[i] * gain))
    if fade_out_frames > 0:
        for i in range(fade_out_frames):
            # 距片段末尾的距离: 1..fade_out_frames，末帧 -> 0
            gain = i / fade_out_frames
            pos = n - 1 - i
            chunk[pos] = int(round(chunk[pos] * gain))


# ----------------------------------------------------------------------
# 整体音量与导出
# ----------------------------------------------------------------------
def apply_volume(samples: array.array, volume: int) -> array.array:
    """按百分比统一缩放全部样本，超界取边界值 (钳制，绝不回绕)。"""
    if volume == DEFAULT_VOLUME or len(samples) == 0:
        return samples
    factor = volume / 100.0
    scaled = array.array("h")
    append = scaled.append
    for value in samples:
        v = int(round(value * factor))
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        append(v)
    return scaled


def write_wav(path: str, samples: array.array, framerate: int) -> None:
    """写出单声道 16bit PCM WAV。"""
    wf = wave.open(path, "wb")
    try:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(framerate)
        wf.writeframes(samples.tobytes())
    finally:
        wf.close()


def build_report(
    requested_volume: int,
    applied_volume: int,
    volume_valid: bool,
    volume_reason: str | None,
    framerate: int,
    accepted: list[dict],
    rejected: list[dict],
    total_frames: int,
) -> dict:
    return {
        "format_version": 1,
        "sample_rate": framerate,
        "channels": 1,
        "sample_width_bits": 16,
        "volume": {
            "requested": requested_volume,
            "applied": applied_volume,
            "valid": volume_valid,
            "reason": volume_reason,
        },
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "total_frames": total_frames,
        "duration_ms": round(total_frames * 1000.0 / framerate, 6),
        "accepted_segments": accepted,
        "rejected_segments": rejected,
    }


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def main(argv: list[str]) -> int:
    # 必须恰好 4 个参数；参数校验先于一切文件操作，保证不创建任何文件
    if len(argv) != 5:
        print(USAGE, file=sys.stderr)
        return 2

    input_wav, script_path, output_wav, report_path = argv[1:5]

    try:
        # 先完成所有读取与校验，再做任何写出动作
        source, framerate = read_mono_pcm16_wav(input_wav)
        script = load_script(script_path)

        requested_volume = script.get("volume", DEFAULT_VOLUME)
        applied_volume, volume_valid, volume_reason = validate_volume(
            requested_volume
        )

        mixed, accepted, rejected = process_segments(
            source, framerate, script["segments"]
        )
        mixed = apply_volume(mixed, applied_volume)

        report = build_report(
            requested_volume=requested_volume,
            applied_volume=applied_volume,
            volume_valid=volume_valid,
            volume_reason=volume_reason,
            framerate=framerate,
            accepted=accepted,
            rejected=rejected,
            total_frames=len(mixed),
        )

        # 全部数据在内存中就绪后才落盘
        write_wav(output_wav, mixed, framerate)
        with open(report_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
    except EditorError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # 防御性兜底: 任何失败都以非零码退出
        print(f"错误: 处理失败 - {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(
        f"导出完成: {output_wav} ({len(mixed)} 帧, "
        f"{len(accepted)} 个片段成功, {len(rejected)} 个片段拒绝); "
        f"报告: {report_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
