"""核心切片、淡入淡出、RMS 响度归一化与报告逻辑。

时间单位：剪辑点 JSON 中的 start/end 一律为秒（float）。
仅支持：16 位 PCM、单声道或立体声、44.1kHz / 48kHz。
所有数值计算使用确定性的定点规则，重复运行输出字节一致。
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import sys
import wave
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 常量

DEFAULT_FADE_SECONDS = 0.01      # 默认淡入/淡出时长（秒）
DEFAULT_TARGET_DBFS = -20.0      # 默认目标响度（dBFS）
GAIN_MIN = 0.1                   # 增益下限（防止过度衰减）
GAIN_MAX = 10.0                  # 增益上限（防止过度放大）
FULL_SCALE = 32768.0             # 16 位 PCM 满量程参考
ALLOWED_SAMPLE_RATES = (44100, 48000)
ALLOWED_CHANNELS = (1, 2)
SAMPLE_WIDTH = 2                 # 16 位
REPORT_NAME = "report.json"


class ValidationError(Exception):
    """输入 WAV / 剪辑点校验失败（任务终止，不产出任何 WAV）。"""

    def __init__(self, message: str,
                 failures: Optional[List[Dict[str, str]]] = None):
        super().__init__(message)
        # 结构化失败明细 [{"id": 片段标识, "reason": 原因}]
        self.failures: List[Dict[str, str]] = failures or []


class ProcessingInterrupt(Exception):
    """处理过程中发生 I/O 故障，处理中断（已产出文件保留）。"""


# ---------------------------------------------------------------- 数据结构


@dataclass
class ClipSpec:
    clip_id: str
    start: float
    end: float


@dataclass
class WavData:
    frames: array.array            # 交错排列的 int16 样本
    sample_rate: int
    channels: int

    @property
    def n_frames(self) -> int:
        return len(self.frames) // self.channels

    @property
    def duration(self) -> float:
        return self.n_frames / self.sample_rate


@dataclass
class ProcessResult:
    return_code: int
    report_path: str
    report: Dict[str, Any]
    interrupted: bool = False


# ---------------------------------------------------------------- WAV 读写


def read_wav(path: str) -> WavData:
    """读取并校验 16 位 PCM WAV，不满足约束抛 ValidationError。"""
    try:
        with wave.open(path, "rb") as wf:
            sample_width = wf.getsampwidth()
            channels = wf.getnchannels()
            sample_rate = wf.getframerate()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
    except (wave.Error, EOFError, OSError) as exc:
        raise ValidationError(f"无法读取 WAV 文件 {path}: {exc}") from exc

    problems = []
    if sample_width != SAMPLE_WIDTH:
        problems.append(f"位深为 {sample_width * 8} 位，要求 16 位 PCM")
    if channels not in ALLOWED_CHANNELS:
        problems.append(f"声道数为 {channels}，要求单声道(1)或立体声(2)")
    if sample_rate not in ALLOWED_SAMPLE_RATES:
        problems.append(f"采样率为 {sample_rate}Hz，要求 44100 或 48000Hz")
    if problems:
        raise ValidationError(
            f"输入 WAV {path} 格式不满足要求: " + "；".join(problems)
        )

    frames = array.array("h")
    frames.frombytes(raw)
    # array('h') 按本机字节序解释，WAV 为小端，非小端平台需要交换
    if sys.byteorder != "little":
        frames.byteswap()
    return WavData(frames=frames, sample_rate=sample_rate, channels=channels)


def write_wav(path: str, frames: array.array, sample_rate: int,
              channels: int) -> None:
    """以与输入一致的采样率/声道写出 16 位 PCM WAV（确定性字节）。"""
    out = frames
    if sys.byteorder != "little":
        out = array.array("h", frames)
        out.byteswap()
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(sample_rate)
        wf.writeframes(out.tobytes())


# ---------------------------------------------------------------- 剪辑点解析


def _safe_clip_id(raw: Any) -> str:
    """检查片段标识，防止路径穿越（不得含路径分隔符或 .. ）。"""
    cid = str(raw)
    if not cid or cid in (".", "..") or os.sep in cid or "/" in cid or "\\" in cid:
        raise ValidationError(f"非法片段标识: {raw!r}")
    return cid


def parse_clips(doc: Any, audio_duration: float) -> List[ClipSpec]:
    """解析剪辑点 JSON 并全量校验，任一不合法即抛 ValidationError。

    支持 {"clips": [ ... ]} 或裸列表 [ ... ]。
    """
    if isinstance(doc, dict) and "clips" in doc:
        items = doc["clips"]
    elif isinstance(doc, list):
        items = doc
    else:
        raise ValidationError("剪辑点 JSON 必须是含 clips 列表的对象或列表")
    if not isinstance(items, list) or not items:
        raise ValidationError("剪辑点列表为空或格式错误")

    clips: List[ClipSpec] = []
    seen = set()
    failures: List[Dict[str, str]] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            failures.append({"id": f"#index_{idx + 1}",
                             "reason": "剪辑点不是 JSON 对象"})
            continue
        cid_raw = item.get("id")
        if cid_raw is None:
            failures.append({"id": f"#index_{idx + 1}",
                             "reason": "缺少 id 字段"})
            continue
        try:
            cid = _safe_clip_id(cid_raw)
        except ValidationError as exc:
            failures.append({"id": str(cid_raw), "reason": str(exc)})
            continue
        if cid in seen:
            failures.append({"id": cid, "reason": "标识重复"})
            continue
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            failures.append({"id": cid, "reason": "start/end 缺失或不是数字"})
            continue
        if not (math.isfinite(start) and math.isfinite(end)):
            failures.append({"id": cid, "reason": "start/end 必须是有限数值"})
            continue
        if start >= end:
            failures.append({
                "id": cid,
                "reason": f"起点({start:.6f}s)不小于终点({end:.6f}s)",
            })
            continue
        if start < 0.0:
            failures.append({
                "id": cid,
                "reason": f"起点({start:.6f}s)为负数",
            })
            continue
        if end > audio_duration + 1e-9:
            failures.append({
                "id": cid,
                "reason": f"终点({end:.6f}s)超出音频时长"
                          f"({audio_duration:.6f}s)",
            })
            continue
        seen.add(cid)
        clips.append(ClipSpec(cid, start, end))

    if failures:
        message = "剪辑点校验失败:\n" + "\n".join(
            f"  - 片段 {f['id']}: {f['reason']}" for f in failures)
        raise ValidationError(message, failures=failures)
    return clips


def load_clips(json_path: str, audio_duration: float) -> List[ClipSpec]:
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"无法读取剪辑点 JSON {json_path}: {exc}") from exc
    return parse_clips(doc, audio_duration)


# ---------------------------------------------------------------- DSP


def rms_dbfs(samples: array.array) -> float:
    """交错样本整体 RMS（线性），返回 dBFS；静音返回 -inf。"""
    if not samples:
        return float("-inf")
    total = 0.0
    for v in samples:
        total += float(v) * float(v)
    rms = math.sqrt(total / len(samples))
    if rms <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(rms / FULL_SCALE)


def channel_rms_list(samples: array.array, channels: int) -> List[float]:
    """各声道单独的 RMS（dBFS）。"""
    acc = [0.0] * channels
    cnt = [0] * channels
    for i, v in enumerate(samples):
        fv = float(v)
        ch = i % channels
        acc[ch] += fv * fv
        cnt[ch] += 1
    out = []
    for ch in range(channels):
        if cnt[ch] == 0 or acc[ch] <= 0.0:
            out.append(float("-inf"))
        else:
            rms = math.sqrt(acc[ch] / cnt[ch])
            out.append(20.0 * math.log10(rms / FULL_SCALE))
    return out


def _clamp_int16(x: float) -> int:
    """确定性四舍五入（银行家规则的对称实现）并夹到 int16 范围。"""
    if x >= 0.0:
        q = int(x + 0.5)
    else:
        q = int(x - 0.5)
    if q > 32767:
        return 32767
    if q < -32768:
        return -32768
    return q


def apply_fades(samples: array.array, channels: int, n_frames: int,
                fade_in_frames: int, fade_out_frames: int) -> array.array:
    """线性淡入/淡出，立体声时左右声道同步施加相同系数。"""
    out = array.array("h", [0]) * len(samples)
    fi = max(fade_in_frames, 0)
    fo = max(fade_out_frames, 0)
    for frame in range(n_frames):
        gain = 1.0
        if frame < fi:
            # 第 0 帧系数 0，最后一个淡入帧系数接近 1
            gain = (frame + 1) / (fi + 1)
        elif frame >= n_frames - fo:
            step = n_frames - frame  # 最后一帧为 1
            gain = step / (fo + 1)
        base = frame * channels
        for ch in range(channels):
            out[base + ch] = _clamp_int16(samples[base + ch] * gain)
    return out


def apply_gain(samples: array.array, gain: float) -> Tuple[array.array, bool]:
    """施加统一增益并夹幅，返回（新样本, 是否发生削波）。"""
    out = array.array("h", [0]) * len(samples)
    clipped = False
    for i, v in enumerate(samples):
        scaled = v * gain
        if scaled > 32767.0 or scaled < -32768.0:
            clipped = True
        out[i] = _clamp_int16(scaled)
    return out, clipped


def _short_fade_samples(requested_seconds: float, sample_rate: int,
                        n_frames: int) -> Tuple[int, int, bool]:
    """计算实际淡入/淡出帧数；超过片段一半时缩短到一半并提示。"""
    requested_frames = int(round(requested_seconds * sample_rate))
    half = n_frames // 2
    adjusted = False
    if requested_frames > half:
        requested_frames = half
        adjusted = True
    return requested_frames, half, adjusted


# ---------------------------------------------------------------- 单片段处理


def _build_failure(clip_id: str, reason: str) -> Dict[str, Any]:
    return {"id": clip_id, "reason": reason}


def process_clip_with_target(clip: ClipSpec, wav: WavData, out_dir: str,
                             fade_seconds: float,
                             target_dbfs: float) -> Dict[str, Any]:
    sr = wav.sample_rate
    ch = wav.channels
    start_frame = int(round(clip.start * sr))
    end_frame = int(round(clip.end * sr))
    n_frames = end_frame - start_frame
    if n_frames <= 0:
        raise ValueError(
            f"裁切后为空（start_frame={start_frame}, end_frame={end_frame}）"
        )

    raw = wav.frames[start_frame * ch: end_frame * ch]
    if len(raw) != n_frames * ch:
        raise ValueError("裁切样本数与帧数不一致，数据异常")

    # 1) 淡入淡出
    fi_frames, _, fi_adjusted = _short_fade_samples(fade_seconds, sr, n_frames)
    fo_frames, _, fo_adjusted = _short_fade_samples(fade_seconds, sr, n_frames)
    faded = apply_fades(raw, ch, n_frames, fi_frames, fo_frames)
    notes: List[str] = []
    if fi_adjusted:
        notes.append(
            f"请求淡入 {fade_seconds:.4f}s 超过片段时长一半，"
            f"实际淡入 {fi_frames / sr:.6f}s（{fi_frames} 帧）"
        )
    if fo_adjusted:
        notes.append(
            f"请求淡出 {fade_seconds:.4f}s 超过片段时长一半，"
            f"实际淡出 {fo_frames / sr:.6f}s（{fo_frames} 帧）"
        )

    # 2) RMS 归一化：增益按淡变后整体 RMS 计算，立体声共用同一增益
    original_rms_db = rms_dbfs(raw)
    pre_rms_db = rms_dbfs(faded)
    gain_clamped = False
    if pre_rms_db == float("-inf"):
        gain = 1.0
        notes.append("片段为静音（RMS=0），无法按 RMS 归一化，增益保持 1.0")
        processed = faded
        clipped = False
    else:
        # RMS 归一化：增益 = 10^((目标dB - 当前dB)/20)，立体声共用
        desired_gain = 10.0 ** ((target_dbfs - pre_rms_db) / 20.0)
        gain = desired_gain
        if gain < GAIN_MIN:
            notes.append(
                f"计算增益 {desired_gain:.4f} 低于下限 {GAIN_MIN}，"
                f"限制为 {GAIN_MIN}"
            )
            gain = GAIN_MIN
            gain_clamped = True
        elif gain > GAIN_MAX:
            notes.append(
                f"计算增益 {desired_gain:.4f} 超过上限 {GAIN_MAX}，"
                f"限制为 {GAIN_MAX}（防止过度放大）"
            )
            gain = GAIN_MAX
            gain_clamped = True
        processed, clipped = apply_gain(faded, gain)
        if clipped:
            notes.append("施加增益后样本超出 int16 范围，已夹幅")

    post_rms_db = rms_dbfs(processed)
    post_channels_db = channel_rms_list(processed, ch)
    original_channels_db = channel_rms_list(raw, ch)
    duration = n_frames / sr

    out_path = os.path.join(out_dir, f"{clip.clip_id}.wav")
    existed = os.path.exists(out_path)
    write_wav(out_path, processed, sr, ch)  # I/O 错误向上抛 -> 中断

    entry: Dict[str, Any] = {
        "id": clip.clip_id,
        "output_file": os.path.basename(out_path),
        "overwritten": existed,
        "sample_rate": sr,
        "channels": ch,
        "frames": n_frames,
        "original_duration_seconds": duration,
        "processed_duration_seconds": duration,
        "original_rms_dbfs": _json_float(original_rms_db),
        "processed_rms_dbfs": _json_float(post_rms_db),
        "original_channel_rms_dbfs": [_json_float(x) for x in original_channels_db],
        "processed_channel_rms_dbfs": [_json_float(x) for x in post_channels_db],
        "gain": gain,
        "gain_clamped": gain_clamped,
        "fade_applied": (fi_frames > 0 or fo_frames > 0),
        "fade_in_seconds": fi_frames / sr,
        "fade_out_seconds": fo_frames / sr,
        "fade_in_frames": fi_frames,
        "fade_out_frames": fo_frames,
        "notes": notes,
    }
    return entry


def _json_float(x: float) -> Optional[float]:
    """JSON 不支持 ±inf/NaN，统一转为 None（静音 RMS 场景）。"""
    if x is None:
        return None
    if math.isinf(x) or math.isnan(x):
        return None
    return x


def _json_safe(value: Any) -> Any:
    """把 -inf 等 JSON 不支持的值转成 None，保证 json.dumps 合法。"""
    if isinstance(value, float):
        if math.isinf(value) or math.isnan(value):
            return None
        return value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


# ---------------------------------------------------------------- 报告与主流程


def build_input_summary(wav_path: str, wav: WavData, clip_count: int,
                        fade_seconds: float, target_dbfs: float,
                        clips_json_path: str) -> Dict[str, Any]:
    return {
        "input_wav": os.path.abspath(wav_path),
        "clips_json": os.path.abspath(clips_json_path),
        "sample_rate": wav.sample_rate,
        "channels": wav.channels,
        "sample_width_bits": SAMPLE_WIDTH * 8,
        "total_frames": wav.n_frames,
        "duration_seconds": wav.duration,
        "clip_count": clip_count,
        "fade_seconds_requested": fade_seconds,
        "target_dbfs": target_dbfs,
        "gain_limits": {"min": GAIN_MIN, "max": GAIN_MAX},
    }


def _write_report(report: Dict[str, Any], out_dir: str,
                  report_name: str) -> str:
    """优先写输出目录；目录不可写时回退到父目录/当前目录（任务14）。"""
    payload = json.dumps(_json_safe(report), ensure_ascii=False,
                         indent=2, sort_keys=False)
    primary = os.path.join(out_dir, report_name)
    try:
        with open(primary, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
        return primary
    except OSError:
        parent = os.path.dirname(os.path.abspath(out_dir)) or os.getcwd()
        fallback = os.path.join(parent, f"{report_name}.interrupted.json")
        with open(fallback, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
        return fallback


def run(wav_path: str, clips_json_path: str, out_dir: str,
        fade_seconds: float = DEFAULT_FADE_SECONDS,
        target_dbfs: float = DEFAULT_TARGET_DBFS,
        report_name: str = REPORT_NAME,
        verbose: bool = True) -> ProcessResult:
    """完整处理流程。

    返回 ProcessResult；校验失败抛 ValidationError，I/O 中断抛
    ProcessingInterrupt（报告已尽力落盘）。
    """
    if fade_seconds < 0:
        raise ValidationError("淡入淡出时长不能为负数")

    # 1) 输入 WAV 校验（不通过直接终止，不创建任何 WAV）
    wav = read_wav(wav_path)

    # 2) 剪辑点全量校验（任一非法整体失败，不产出 WAV）。
    #    捕获后写失败报告再向上抛，保证报告可点名失败片段。
    try:
        clips = load_clips(clips_json_path, wav.duration)
    except ValidationError as exc:
        structured = sorted(exc.failures, key=lambda x: x["id"]) \
            if exc.failures else [{"id": "_validation", "reason": str(exc)}]
        failure_report = {
            "status": "validation_failed",
            "input_summary": {
                "input_wav": os.path.abspath(wav_path),
                "clips_json": os.path.abspath(clips_json_path),
                "sample_rate": wav.sample_rate,
                "channels": wav.channels,
                "sample_width_bits": SAMPLE_WIDTH * 8,
                "total_frames": wav.n_frames,
                "duration_seconds": wav.duration,
                "fade_seconds_requested": fade_seconds,
                "target_dbfs": target_dbfs,
                "gain_limits": {"min": GAIN_MIN, "max": GAIN_MAX},
            },
            "success_count": 0,
            "failure_count": len(structured),
            "successes": [],
            "failures": structured,
            "interrupted_reason": None,
        }
        # 此刻才创建输出目录，仅用于放报告；绝不写 WAV
        try:
            os.makedirs(out_dir, exist_ok=True)
            _write_report(failure_report, out_dir, report_name)
        except OSError:
            pass
        raise

    # 3) 输出目录（缺失自动创建；创建失败即 I/O 故障）
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        raise ProcessingInterrupt(f"输出目录无法创建: {out_dir}: {exc}") from exc

    successes: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    interrupted_reason: Optional[str] = None

    # 4) 逐片段处理
    for clip in clips:
        try:
            entry = process_clip_with_target(
                clip, wav, out_dir, fade_seconds, target_dbfs
            )
        except (OSError,) as exc:
            # I/O 故障：中断，已完成片段保留，报告记录中断原因
            interrupted_reason = (
                f"片段 {clip.clip_id} 写出失败（I/O 故障）: {exc}"
            )
            break
        except Exception as exc:  # 单个片段失败不影响其他片段
            reason = f"{type(exc).__name__}: {exc}"
            failures.append(_build_failure(clip.clip_id, reason))
            if verbose:
                print(f"[失败] 片段 {clip.clip_id}: {reason}", flush=True)
            continue
        successes.append(entry)
        if verbose:
            extra = "（覆盖已有文件）" if entry["overwritten"] else ""
            print(
                f"[完成] 片段 {entry['id']}: {entry['frames']} 帧, "
                f"{entry['original_duration_seconds']:.4f}s, "
                f"增益 x{entry['gain']:.4f}, "
                f"处理后 RMS {entry['processed_rms_dbfs']:.2f} dBFS{extra}",
                flush=True,
            )

    # 5) 汇总报告（失败列表按标识升序）
    failures.sort(key=lambda x: x["id"])
    status = "interrupted" if interrupted_reason else (
        "completed_with_failures" if failures else "completed")
    report: Dict[str, Any] = {
        "status": status,
        "input_summary": build_input_summary(
            wav_path, wav, len(clips), fade_seconds, target_dbfs,
            clips_json_path),
        "success_count": len(successes),
        "failure_count": len(failures),
        "successes": successes,
        "failures": failures,
        "interrupted_reason": interrupted_reason,
    }
    report_path = _write_report(report, out_dir, report_name)

    if verbose:
        print("-" * 60, flush=True)
        print(f"成功 {len(successes)} 个，失败 {len(failures)} 个", flush=True)
        print(f"输出目录: {os.path.abspath(out_dir)}", flush=True)
        print(f"报告文件: {os.path.abspath(report_path)}", flush=True)
        if interrupted_reason:
            print(f"中断原因: {interrupted_reason}", flush=True)

    if interrupted_reason:
        raise ProcessingInterrupt(interrupted_reason)

    return_code = 0 if not failures else 5
    return ProcessResult(
        return_code=return_code, report_path=report_path, report=report)


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audio-cutter",
        description="配音长录音按剪辑点切片，加淡入淡出并做 RMS 响度归一化。",
    )
    parser.add_argument("--input", required=True, help="输入 16 位 PCM WAV 路径")
    parser.add_argument("--clips", required=True, help="剪辑点 JSON 路径")
    parser.add_argument("--output-dir", required=True, help="片段输出目录")
    parser.add_argument(
        "--fade", type=float, default=DEFAULT_FADE_SECONDS,
        help=f"淡入/淡出时长（秒），默认 {DEFAULT_FADE_SECONDS}")
    parser.add_argument(
        "--target-dbfs", type=float, default=DEFAULT_TARGET_DBFS,
        help=f"目标 RMS 响度（dBFS），默认 {DEFAULT_TARGET_DBFS}")
    parser.add_argument(
        "--report-name", default=REPORT_NAME, help="报告文件名，默认 report.json")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    # argparse 缺必填参数时自动打印用法并以退出码 2 退出
    args = parser.parse_args(argv)
    try:
        result = run(
            args.input, args.clips, args.output_dir,
            fade_seconds=args.fade,
            target_dbfs=args.target_dbfs,
            report_name=args.report_name,
        )
    except ValidationError as exc:
        print(f"[校验失败] {exc}", file=sys.stderr)
        return 3
    except ProcessingInterrupt as exc:
        print(f"[处理中断] {exc}", file=sys.stderr)
        return 4
    return result.return_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
