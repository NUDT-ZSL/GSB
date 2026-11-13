#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
podcast_editor.py —— 播客单集主干录音批量粗剪工具（Python 3.10+ 标准库）

功能：
  1. 读取并校验剪辑清单 JSON（clip_id 唯一、必填字段齐全、单一主干、提示音仅允许相对路径）。
  2. 按清单顺序应用「删除区间」与「插入提示音」两类操作。
  3. 单个片段输入问题（删除越界/起止非法、提示音文件缺失等）只令该片段未处理，
     不影响其余片段；整体性错误（清单非法、主干不可读等）整体拒绝、不生成任何产物。
  4. 提示音重采样到主干采样率，成品统一为单声道 16-bit PCM WAV，并保证样本不越界。
  5. 整段峰值归一化到命令行指定目标峰值（含目标峰值合法性校验）。
  6. 成品与 JSON 报告写入输出目录；输出不可写或同名成品已存在时拒绝、绝不覆盖。

退出码：
  0  成功（报告中可能存在 status=unprocessed 的片段，属于业务结果而非运行失败）
  2  整体性错误（参数、清单、主干、输出目录等问题）
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import wave
from array import array
from pathlib import Path

OUTPUT_WAV_NAME = "podcast_output.wav"
OUTPUT_REPORT_NAME = "processing_report.json"

# 16-bit PCM 格式范围
SAMPLE_MIN = -(2 ** 15)
SAMPLE_MAX = 2 ** 15 - 1

# 目标峰值（线性幅值，针对 16-bit）允许范围
PEAK_MIN = 1
PEAK_MAX = SAMPLE_MAX

REQUIRED_CLIP_FIELDS = ("clip_id", "type")
DELETE_FIELDS = ("start_sec", "end_sec")
INSERT_FIELDS = ("at_sec", "tone")


class JobError(Exception):
    """整体性错误：必须整体拒绝、不产生成品与报告。"""


def _is_real_number(value) -> bool:
    """bool 不是数值；NaN/Infinity 也不接受（清单 JSON 本身也不允许这些 token）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# --------------------------------------------------------------------------- #
# 清单校验（整体性错误在此处全部拦截，通过后才会触碰任何音频/输出文件）
# --------------------------------------------------------------------------- #
def load_and_validate_manifest(manifest_path: Path) -> dict:
    if not manifest_path.is_file():
        raise JobError(f"剪辑清单不存在或不是文件: {manifest_path}")
    try:
        raw = manifest_path.read_text(encoding="utf-8")
        manifest = json.loads(raw)
    except UnicodeDecodeError as exc:
        raise JobError(f"剪辑清单不是有效的 UTF-8 文本: {manifest_path}（{exc}）")
    except json.JSONDecodeError as exc:
        raise JobError(f"剪辑清单 JSON 格式错误: {manifest_path}（第 {exc.lineno} 行第 {exc.colno} 列: {exc.msg}）")

    if not isinstance(manifest, dict):
        raise JobError("剪辑清单顶层必须是 JSON 对象")

    # ---- 单一主干录音约束 ----
    if "main_recording" not in manifest:
        raise JobError("剪辑清单缺少必需字段: main_recording")
    main_ref = manifest["main_recording"]
    if isinstance(main_ref, (list, dict)):
        raise JobError("单次处理只接受一个主干录音，清单引用了多个主干")
    if not isinstance(main_ref, str) or not main_ref.strip():
        raise JobError("main_recording 必须是非空字符串，且只能引用一个主干录音")
    if _is_absolute_or_drive_path(main_ref):
        raise JobError(f"main_recording 只允许相对路径，拒绝绝对路径引用: {main_ref}")

    clips = manifest.get("clips")
    if not isinstance(clips, list) or not clips:
        raise JobError("剪辑清单缺少必需字段 clips，且 clips 必须是非空数组")

    seen_ids: set[str] = set()
    for index, clip in enumerate(clips):
        where = f"clips[{index}]"
        if not isinstance(clip, dict):
            raise JobError(f"{where} 必须是 JSON 对象")

        for field in REQUIRED_CLIP_FIELDS:
            if field not in clip:
                raise JobError(f"{where} 缺少必需字段: {field}")

        clip_id = clip["clip_id"]
        if not isinstance(clip_id, str) or not clip_id.strip():
            raise JobError(f"{where} 的 clip_id 必须是非空字符串")
        if clip_id in seen_ids:
            raise JobError(f"剪辑清单整体拒绝：clip_id 重复: {clip_id!r}")
        seen_ids.add(clip_id)

        clip_type = clip["type"]
        if clip_type == "delete":
            for field in DELETE_FIELDS:
                if field not in clip:
                    raise JobError(f"片段 {clip_id!r}（delete）缺少必需字段: {field}")
            if not _is_real_number(clip["start_sec"]) or not _is_real_number(clip["end_sec"]):
                raise JobError(f"片段 {clip_id!r} 的 start_sec/end_sec 必须是有限数值")
        elif clip_type == "insert_tone":
            for field in INSERT_FIELDS:
                if field not in clip:
                    raise JobError(f"片段 {clip_id!r}（insert_tone）缺少必需字段: {field}")
            if not _is_real_number(clip["at_sec"]):
                raise JobError(f"片段 {clip_id!r} 的 at_sec 必须是有限数值")
            tone_ref = clip["tone"]
            if not isinstance(tone_ref, str) or not tone_ref.strip():
                raise JobError(f"片段 {clip_id!r} 的 tone 必须是非空字符串")
            if _is_absolute_or_drive_path(tone_ref):
                raise JobError(
                    f"剪辑清单整体拒绝：提示音只允许相对路径，片段 {clip_id!r} 使用了绝对路径: {tone_ref}"
                )
        else:
            raise JobError(f"片段 {clip_id!r} 的 type 非法: {clip_type!r}（仅支持 delete / insert_tone）")

    return manifest


def _is_absolute_or_drive_path(ref: str) -> bool:
    """识别 POSIX 绝对路径与 Windows 盘符/UNC 路径。"""
    if not isinstance(ref, str):
        return True
    if ref.startswith(("/", "\\")):
        return True
    # Windows 盘符形式，如 C:\... 或 C:/...
    if len(ref) >= 2 and ref[1] == ":" and ref[0].isalpha():
        return True
    return False


# --------------------------------------------------------------------------- #
# WAV 读写（仅依赖 wave / array 标准库）
# --------------------------------------------------------------------------- #
def read_wav_mono(path: Path):
    """返回 (samples: array('h'), sample_rate, sample_width, channels)。"""
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        frame_count = wf.getnframes()
        raw = wf.readframes(frame_count)

    if sample_width == 1:
        # 8-bit PCM 为无符号 0..255，中点 128
        samples = array("h", (b - 128 for b in raw))
    elif sample_width == 2:
        samples = array("h")
        samples.frombytes(raw)
        if sys.byteorder != "little":
            samples.byteswap()
    elif sample_width == 3:
        samples = array("h")
        for i in range(0, len(raw), 3):
            lo, mid, hi = raw[i], raw[i + 1], raw[i + 2]
            value = lo | (mid << 8) | (hi << 16)
            if value & 0x800000:
                value -= 1 << 24
            # 24-bit -> 16-bit，保留高 16 位有效精度
            samples.append(_clamp16(value >> 8))
    elif sample_width == 4:
        src = array("i")
        src.frombytes(raw)
        if sys.byteorder != "little":
            src.byteswap()
        samples = array("h", (_clamp16(v >> 16) for v in src))
    else:
        raise JobError(f"不支持的 WAV 位深（{sample_width * 8} bit）: {path}")

    if channels > 1:
        # 多声道先混为单声道（平均），保证后续处理始终是单声道
        mono = array("h")
        for i in range(0, len(samples), channels):
            frame = samples[i:i + channels]
            mono.append(_clamp16(round(sum(frame) / channels)))
        samples = mono

    return samples, sample_rate, sample_width, channels


def write_wav_mono16(path: Path, samples: array, sample_rate: int) -> None:
    # 'xb'：排他创建，目标已存在则直接失败，机制上保证绝不覆盖
    with open(path, "xb") as fp:
        with wave.open(fp, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(samples.tobytes())


def _clamp16(value: int) -> int:
    if value < SAMPLE_MIN:
        return SAMPLE_MIN
    if value > SAMPLE_MAX:
        return SAMPLE_MAX
    return value


# --------------------------------------------------------------------------- #
# 采样率转换：线性插值重采样到主干采样率（确定性、无外部库）
# --------------------------------------------------------------------------- #
def resample_linear(samples: array, src_rate: int, dst_rate: int) -> array:
    if src_rate == dst_rate:
        return array("h", samples)
    if not samples:
        return array("h")
    ratio = src_rate / dst_rate
    dst_len = int(round(len(samples) * dst_rate / src_rate))
    out = array("h")
    n = len(samples)
    for i in range(dst_len):
        src_pos = i * ratio
        i0 = int(math.floor(src_pos))
        frac = src_pos - i0
        if i0 + 1 < n:
            value = samples[i0] * (1.0 - frac) + samples[i0 + 1] * frac
        else:
            value = float(samples[-1])
        out.append(_clamp16(_round_half_away(value)))
    return out


def _round_half_away(x: float) -> int:
    """四舍五入（0.5 远离零），跨平台确定。"""
    if x >= 0:
        return int(math.floor(x + 0.5))
    return int(math.ceil(x - 0.5))


# --------------------------------------------------------------------------- #
# 峰值归一化
# --------------------------------------------------------------------------- #
def peak_normalize(samples: array, target_peak: int) -> tuple[array, float, int]:
    """
    返回 (归一化后样本, 增益, 归一化后实测峰值)。
    静音（全零）不做缩放，增益记为 1.0。
    """
    current_peak = max((abs(v) for v in samples), default=0)
    if current_peak == 0:
        return array("h", samples), 1.0, 0
    gain = target_peak / current_peak
    out = array("h", (_clamp16(_round_half_away(v * gain)) for v in samples))
    measured = max(abs(v) for v in out)
    return out, gain, measured


# --------------------------------------------------------------------------- #
# 输出目录与同名文件检查（不覆盖已有文件）
# --------------------------------------------------------------------------- #
def prepare_output_dir(output_dir: Path) -> None:
    if output_dir.exists():
        if not output_dir.is_dir():
            raise JobError(f"输出路径已存在且不是目录: {output_dir}")
    else:
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise JobError(f"无法创建输出目录 {output_dir}: {exc}")

    # 真实写入探针：Windows 目录只读属性不会被 os.access 可靠识别，
    # 直接尝试创建并删除一个临时文件才能跨平台确认可写。
    probe = output_dir / ".write_probe.tmp"
    # O_BINARY 仅 Windows 存在，POSIX 下退化为 0
    binary_flag = getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(str(probe), os.O_WRONLY | os.O_CREAT | os.O_EXCL | binary_flag)
        os.close(fd)
        probe.unlink()
    except OSError as exc:
        raise JobError(f"输出目录不可写: {output_dir}（{exc}）")

    wav_path = output_dir / OUTPUT_WAV_NAME
    report_path = output_dir / OUTPUT_REPORT_NAME
    if wav_path.exists():
        raise JobError(f"同名成品已存在，拒绝覆盖: {wav_path}")
    if report_path.exists():
        raise JobError(f"同名报告已存在，拒绝覆盖: {report_path}")


# --------------------------------------------------------------------------- #
# 主处理流程
# --------------------------------------------------------------------------- #
def process(manifest_path: Path, main_path: Path, output_dir: Path, target_peak: int) -> dict:
    # 1) 目标峰值合法性
    if not isinstance(target_peak, int) or not (PEAK_MIN <= target_peak <= PEAK_MAX):
        raise JobError(
            f"目标峰值非法: {target_peak}；允许范围为 {PEAK_MIN}..{PEAK_MAX}（16-bit 线性幅值）"
        )

    # 2) 清单整体校验（不通过则整体拒绝，此时尚未生成任何产物）
    manifest = load_and_validate_manifest(manifest_path)
    base_dir = manifest_path.parent

    # 3) 主干校验：必须真实存在且可读，错误信息包含路径
    if not main_path.is_file():
        raise JobError(f"主干录音不存在或不是文件: {main_path}")
    if not os.access(main_path, os.R_OK):
        raise JobError(f"主干录音不可读: {main_path}")

    main_ref = manifest["main_recording"]
    if main_path.name != Path(main_ref).name:
        raise JobError(
            f"命令行主干 {main_path.name} 与清单声明的主干 {main_ref} 不一致；单次处理只接受一个主干录音"
        )

    try:
        main_samples, main_rate, _main_width, _main_ch = read_wav_mono(main_path)
    except (wave.Error, EOFError, OSError) as exc:
        raise JobError(f"主干录音无法读取（不是合法 WAV）: {main_path}（{exc}）")

    # 4) 输出目录预检（不可写 / 同名成品 -> 拒绝，不覆盖）
    prepare_output_dir(output_dir)

    # 5) 按清单顺序逐片段应用；片段级问题只标记该片段，其余继续
    work = array("h", main_samples)
    clip_results: list[dict] = []
    for order, clip in enumerate(manifest["clips"]):
        clip_id = clip["clip_id"]
        clip_type = clip["type"]
        result = {
            "clip_id": clip_id,
            "type": clip_type,
            "manifest_index": order,
            "status": "processed",
            "reason": None,
        }
        try:
            if clip_type == "delete":
                start_sec = float(clip["start_sec"])
                end_sec = float(clip["end_sec"])
                if start_sec < 0 or end_sec < 0:
                    raise ValueError("删除区间不能为负时间")
                if start_sec >= end_sec:
                    raise ValueError(
                        f"删除起点不早于终点（start_sec={start_sec}, end_sec={end_sec}）"
                    )
                start_sample = int(round(start_sec * main_rate))
                end_sample = int(round(end_sec * main_rate))
                if start_sample > len(work) or end_sample > len(work):
                    raise ValueError(
                        f"删除区间越界（当前素材 {len(work) / main_rate:.3f}s，"
                        f"请求 {start_sec:.3f}s..{end_sec:.3f}s）"
                    )
                removed = end_sample - start_sample
                del work[start_sample:end_sample]
                result["detail"] = {
                    "start_sample": start_sample,
                    "end_sample": end_sample,
                    "removed_samples": removed,
                    "removed_seconds": removed / main_rate,
                }

            else:  # insert_tone
                at_sec = float(clip["at_sec"])
                if at_sec < 0:
                    raise ValueError("插入位置不能为负时间")
                # 相对清单目录解析提示音路径；清单中的引用形式保持相对路径不变
                tone_path = base_dir / clip["tone"]
                if not tone_path.is_file():
                    raise ValueError(f"提示音文件不存在: {tone_path}")
                if not os.access(tone_path, os.R_OK):
                    raise ValueError(f"提示音文件不可读: {tone_path}")
                try:
                    tone_samples, tone_rate, _tw, _tc = read_wav_mono(tone_path)
                except (wave.Error, EOFError, OSError) as exc:
                    raise ValueError(f"提示音不是合法 WAV: {tone_path}（{exc}）")

                tone_samples = resample_linear(tone_samples, tone_rate, main_rate)
                pos_sample = int(round(at_sec * main_rate))
                if pos_sample > len(work):
                    raise ValueError(
                        f"插入位置越界（当前素材 {len(work) / main_rate:.3f}s，请求 at_sec={at_sec:.3f}s）"
                    )
                work[pos_sample:pos_sample] = tone_samples
                result["detail"] = {
                    "at_sample": pos_sample,
                    "inserted_samples": len(tone_samples),
                    "inserted_seconds": len(tone_samples) / main_rate,
                    "tone_native_rate": tone_rate,
                    "resampled_to_rate": main_rate,
                    "tone_ref": clip["tone"],
                }
        except ValueError as exc:
            result["status"] = "unprocessed"
            result["reason"] = str(exc)
            result["detail"] = None
        clip_results.append(result)

    # 6) 整段峰值归一化（先钳制再断言，任何样本都不会超出 16-bit 范围）
    normalized, gain, measured_peak = peak_normalize(work, target_peak)
    overrange = [v for v in normalized if v < SAMPLE_MIN or v > SAMPLE_MAX]
    if overrange:  # 理论不可达：_clamp16 已保证
        raise JobError("内部错误：归一化后存在越界样本")

    # 7) 排他写入成品与报告（'x' 模式：目标已存在直接失败，绝不覆盖；
    #    预检已先行拦截，此处是第二道保险）
    wav_path = output_dir / OUTPUT_WAV_NAME
    report_path = output_dir / OUTPUT_REPORT_NAME

    processed_count = sum(1 for r in clip_results if r["status"] == "processed")
    unprocessed_count = len(clip_results) - processed_count
    report = {
        "schema_version": 1,
        "main_recording": {
            "ref": manifest["main_recording"],
            "resolved_path": str(main_path),
            "sample_rate": main_rate,
            "sample_width_bits": 16,
            "channels": 1,
            "original_samples": len(main_samples),
            "original_duration_sec": len(main_samples) / main_rate,
        },
        "target_peak": target_peak,
        "normalization": {
            "gain": gain,
            "measured_peak_after": measured_peak,
            "within_target": measured_peak <= target_peak,
        },
        "output": {
            "wav_file": OUTPUT_WAV_NAME,
            "sample_rate": main_rate,
            "sample_width_bits": 16,
            "channels": 1,
            "total_samples": len(normalized),
            "duration_sec": len(normalized) / main_rate,
        },
        "clips": clip_results,
        "summary": {
            "total_clips": len(clip_results),
            "processed": processed_count,
            "unprocessed": unprocessed_count,
        },
        # 需求 11：明确给出可还原清单原始顺序的排序依据
        "ordering": {
            "field": "manifest_index",
            "rule": (
                "clips 数组已按剪辑清单中的原始先后顺序排列；每个片段的 manifest_index "
                "为其在清单 clips 数组中的下标（从 0 开始）。若需自行还原，请按 "
                "manifest_index 升序排序；manifest_index 相同或缺失时再按 clip_id "
                "字典序作为确定性兜底。"
            ),
        },
    }

    wav_created = False
    try:
        write_wav_mono16(wav_path, normalized, main_rate)
        wav_created = True
        # 'x'：排他创建，报告同名存在时失败而非覆盖；
        # newline='\n' 保证报告字节跨平台一致
        with open(report_path, "x", encoding="utf-8", newline="\n") as fp:
            json.dump(report, fp, ensure_ascii=False, indent=2, sort_keys=False)
            fp.write("\n")
    except OSError as exc:
        # 报告创建失败时回滚刚创建的成品，避免留下半成品
        if wav_created:
            try:
                wav_path.unlink()
            except OSError:
                pass
        raise JobError(f"成品或报告写入失败: {exc}")

    return report


# --------------------------------------------------------------------------- #
# 命令行入口
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="podcast_editor.py",
        description="播客主干录音批量粗剪：删除区间、插入提示音、峰值归一化，并产出 JSON 报告。",
    )
    parser.add_argument("--manifest", required=True, help="剪辑清单 JSON 路径")
    parser.add_argument("--main", required=True, help="主干录音 WAV 路径（单声道 16-bit PCM）")
    parser.add_argument("--output-dir", required=True, help="成品与报告的输出目录（不可写或同名文件已存在时拒绝）")
    parser.add_argument(
        "--target-peak",
        required=True,
        type=int,
        help=f"整段峰值归一化目标（16-bit 线性幅值，允许 {PEAK_MIN}..{PEAK_MAX}）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # 保证 Windows 控制台中文错误信息不被编码成 GBK 后丢失
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        report = process(
            manifest_path=Path(args.manifest),
            main_path=Path(args.main),
            output_dir=Path(args.output_dir),
            target_peak=args.target_peak,
        )
    except JobError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    print(
        "处理完成：成功 {processed}/{total} 个片段，未处理 {unprocessed} 个；"
        "成品时长 {dur:.3f}s，归一化后峰值 {peak}（目标 {target}）".format(
            processed=report["summary"]["processed"],
            total=report["summary"]["total_clips"],
            unprocessed=report["summary"]["unprocessed"],
            dur=report["output"]["duration_sec"],
            peak=report["normalization"]["measured_peak_after"],
            target=report["target_peak"],
        )
    )
    print(f"成品 WAV : {Path(args.output_dir) / OUTPUT_WAV_NAME}")
    print(f"处理报告: {Path(args.output_dir) / OUTPUT_REPORT_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
