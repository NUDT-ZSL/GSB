"""WAV 静音区间标记工具（仅依赖 Python 标准库）。

功能：
  - 按文件名字典序处理指定目录下全部 .wav 文件（不递归子目录）；
  - 从 JSON 配置读取 threshold_db / min_silence_ms，缺失或非法时回退默认值；
  - 对单声道 16 位 PCM 文件，按采样绝对值与分贝阈值换算的幅度比较，
    找出长度达到 min_silence_ms 的连续静音区间；
  - 每个合法文件输出 <name>.marks.json，并生成汇总报告 report.json；
  - 损坏 / 格式不符的文件跳过并记录原因，不影响其他文件。

所有输出均为 UTF-8 编码、合法 JSON 对象。
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import sys
import wave

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ---- 业务默认值 ---------------------------------------------------------
DEFAULT_THRESHOLD_DB = -40.0
DEFAULT_MIN_SILENCE_MS = 500
# 16 位 PCM 满量程幅度（采样值范围 -32768..32767）
FULL_SCALE_AMPLITUDE = 32768.0


class WavFormatError(Exception):
    """WAV 文件无法解析或格式不符合单声道 16 位 PCM 要求。"""


# ---- 配置读取 -----------------------------------------------------------
def _is_finite_number(value) -> bool:
    """布尔值视为非法配置（bool 是 int 的子类，需显式排除）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def load_config(config_path: str | None) -> tuple[float, float, list[str]]:
    """读取 JSON 配置。

    返回 (threshold_db, min_silence_ms, notes)。
    配置文件缺失、内容非法或字段非法时使用默认值，并把原因写入 notes，
    由调用方写入报告，保证任何输入下都不崩溃。
    """
    notes: list[str] = []
    cfg: dict = {}

    if config_path is None:
        notes.append("未指定配置文件，threshold_db 与 min_silence_ms 均使用默认值")
    else:
        try:
            # utf-8-sig 同时兼容带 BOM 与不带 BOM 的 UTF-8（Windows 记事本常见）
            with open(config_path, "r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                notes.append("配置文件顶层不是 JSON 对象，全部参数使用默认值")
            else:
                cfg = loaded
        except FileNotFoundError:
            notes.append(f"配置文件不存在: {config_path}，全部参数使用默认值")
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            notes.append(f"配置文件无法解析({exc.__class__.__name__}: {exc})，全部参数使用默认值")

    # 阈值
    raw_threshold = cfg.get("threshold_db")
    if _is_finite_number(raw_threshold):
        threshold_db = float(raw_threshold)
    else:
        threshold_db = DEFAULT_THRESHOLD_DB
        if "threshold_db" in cfg:
            notes.append(
                f"threshold_db 字段非法({raw_threshold!r})，采用默认阈值 "
                f"{DEFAULT_THRESHOLD_DB:g}dB"
            )
        else:
            notes.append(
                f"配置缺少 threshold_db 字段，采用默认阈值 {DEFAULT_THRESHOLD_DB:g}dB"
            )

    # 最短静音时长
    raw_min_ms = cfg.get("min_silence_ms")
    if _is_finite_number(raw_min_ms) and raw_min_ms >= 0:
        min_silence_ms = float(raw_min_ms)
    else:
        min_silence_ms = float(DEFAULT_MIN_SILENCE_MS)
        if "min_silence_ms" in cfg:
            notes.append(
                f"min_silence_ms 字段非法({raw_min_ms!r})，采用默认值 "
                f"{DEFAULT_MIN_SILENCE_MS}ms"
            )
        else:
            notes.append(
                f"配置缺少 min_silence_ms 字段，采用默认值 {DEFAULT_MIN_SILENCE_MS}ms"
            )

    return threshold_db, min_silence_ms, notes


# ---- WAV 读取与静音检测 -------------------------------------------------
def read_mono_pcm16_wav(path: str) -> tuple[array.array, int]:
    """读取单声道 16 位 PCM WAV，返回 (16 位有符号采样数组, 采样率)。

    任何解析失败或格式不符都抛 WavFormatError，由上层统一记录。
    """
    try:
        with wave.open(path, "rb") as wf:
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            sample_rate = wf.getframerate()
            frame_count = wf.getnframes()
            raw = wf.readframes(frame_count)
    except (wave.Error, EOFError, OSError, ValueError) as exc:
        raise WavFormatError(f"WAV 解析失败: {exc.__class__.__name__}: {exc}") from exc

    if wf.getcomptype() != "NONE":
        raise WavFormatError(f"不支持压缩格式: {wf.getcompname()}")
    if channels != 1:
        raise WavFormatError(f"仅支持单声道文件，实际声道数: {channels}")
    if sample_width != 2:
        raise WavFormatError(f"仅支持 16 位 PCM，实际采样位宽: {sample_width * 8} 位")
    if sample_rate <= 0:
        raise WavFormatError(f"采样率非法: {sample_rate}")

    try:
        samples = array.array("h")
        samples.frombytes(raw)
    except (ValueError, EOFError) as exc:
        raise WavFormatError(f"PCM 数据损坏: {exc}") from exc

    return samples, sample_rate


def db_to_amplitude(threshold_db: float) -> float:
    """分贝阈值换算为线性幅度：amplitude = 32768 * 10^(db/20)。

    结果裁剪到 [0, 32768]，任何极端输入（0dB、-96dB 等）都得到有限可用值。
    """
    amplitude = FULL_SCALE_AMPLITUDE * (10.0 ** (threshold_db / 20.0))
    if not math.isfinite(amplitude):
        amplitude = 0.0 if threshold_db < 0 else FULL_SCALE_AMPLITUDE
    return max(0.0, min(FULL_SCALE_AMPLITUDE, amplitude))


def find_silence_marks(
    samples: array.array,
    sample_rate: int,
    threshold_db: float,
    min_silence_ms: float,
) -> list[dict[str, int]]:
    """找出长度达到 min_silence_ms 的连续静音区间。

    静音判定：|sample| < amplitude（严格小于，采样值 0 恒为静音）。
    区间端点为整数毫秒并向下取整：ms = sample_index * 1000 // sample_rate。
    """
    amplitude = db_to_amplitude(threshold_db)
    min_silence_samples = int(math.ceil(min_silence_ms * sample_rate / 1000.0))

    marks: list[dict[str, int]] = []
    run_start: int | None = None

    for index, sample in enumerate(samples):
        is_silent = abs(sample) < amplitude
        if is_silent and run_start is None:
            run_start = index
        elif not is_silent and run_start is not None:
            if index - run_start >= min_silence_samples:
                marks.append(
                    {
                        "start_ms": run_start * 1000 // sample_rate,
                        "end_ms": index * 1000 // sample_rate,
                    }
                )
            run_start = None

    # 文件结尾仍在静音段中
    if run_start is not None:
        total = len(samples)
        if total - run_start >= min_silence_samples:
            marks.append(
                {
                    "start_ms": run_start * 1000 // sample_rate,
                    "end_ms": total * 1000 // sample_rate,
                }
            )

    return marks


# ---- 目录级批处理 -------------------------------------------------------
def process_directory(
    input_dir: str,
    config_path: str | None,
    output_dir: str | None = None,
) -> dict:
    """处理目录内全部 .wav 文件，写标记文件与汇总报告，返回报告对象。"""
    if not os.path.isdir(input_dir):
        raise NotADirectoryError(f"输入目录不存在或不是目录: {input_dir}")

    threshold_db, min_silence_ms, notes = load_config(config_path)
    target_dir = output_dir or input_dir
    os.makedirs(target_dir, exist_ok=True)

    # 字典序、仅当前层、不递归；统一用小写后缀匹配 .wav
    wav_names = sorted(
        name
        for name in os.listdir(input_dir)
        if os.path.isfile(os.path.join(input_dir, name)) and name.lower().endswith(".wav")
    )

    files_report: list[dict] = []
    total_marks = 0

    for name in wav_names:
        wav_path = os.path.join(input_dir, name)
        entry: dict = {"file": name, "status": "", "error": None, "marks": []}
        try:
            samples, sample_rate = read_mono_pcm16_wav(wav_path)
            marks = find_silence_marks(samples, sample_rate, threshold_db, min_silence_ms)

            mark_payload = {
                "file": name,
                "sample_rate": sample_rate,
                "frames": len(samples),
                "threshold_db": threshold_db,
                "min_silence_ms": min_silence_ms,
                "marks": marks,
            }
            mark_path = os.path.join(target_dir, f"{name}.marks.json")
            with open(mark_path, "w", encoding="utf-8") as f:
                json.dump(mark_payload, f, ensure_ascii=False, indent=2)

            entry["status"] = "ok"
            entry["marks"] = marks
            entry["mark_file"] = os.path.basename(mark_path)
            total_marks += len(marks)
        except WavFormatError as exc:
            # 损坏或格式不符：跳过且不生成标记文件，不影响后续文件
            entry["status"] = "error"
            entry["error"] = str(exc)
        except OSError as exc:
            entry["status"] = "error"
            entry["error"] = f"文件读取失败: {exc.__class__.__name__}: {exc}"

        files_report.append(entry)

    report = {
        "input_dir": os.path.abspath(input_dir),
        "output_dir": os.path.abspath(target_dir),
        "config": {
            "threshold_db": threshold_db,
            "min_silence_ms": min_silence_ms,
            "config_path": os.path.abspath(config_path) if config_path else None,
            "notes": notes,
        },
        "files": files_report,
        "total_marks": total_marks,
        "total_files": len(files_report),
        "success_files": sum(1 for e in files_report if e["status"] == "ok"),
        "failed_files": sum(1 for e in files_report if e["status"] == "error"),
    }

    report_path = os.path.join(target_dir, "report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    return report


# ---- 命令行入口 ---------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="扫描目录中的单声道 16 位 PCM WAV 文件并标记静音区间（纯标准库）"
    )
    parser.add_argument("input_dir", help="包含 .wav 文件的输入目录（不递归子目录）")
    parser.add_argument(
        "-c", "--config", dest="config_path", default=None,
        help="JSON 配置文件，可含 threshold_db 与 min_silence_ms；缺失字段回退默认值",
    )
    parser.add_argument(
        "-o", "--output-dir", dest="output_dir", default=None,
        help="标记文件与 report.json 的输出目录（默认与输入目录相同）",
    )
    args = parser.parse_args(argv)

    try:
        report = process_directory(args.input_dir, args.config_path, args.output_dir)
    except (NotADirectoryError, OSError) as exc:
        print(f"[致命错误] {exc}", file=sys.stderr)
        return 2

    print(f"处理目录: {report['input_dir']}")
    for note in report["config"]["notes"]:
        print(f"[配置] {note}")
    print(
        f"生效配置: threshold_db={report['config']['threshold_db']:g}dB, "
        f"min_silence_ms={report['config']['min_silence_ms']:g}ms"
    )
    for entry in report["files"]:
        if entry["status"] == "ok":
            print(f"  [OK]    {entry['file']}: {len(entry['marks'])} 个静音区间")
        else:
            print(f"  [ERROR] {entry['file']}: {entry['error']}")
    print(
        f"汇总: 文件 {report['total_files']} 个（成功 {report['success_files']} / "
        f"失败 {report['failed_files']}），total_marks={report['total_marks']}"
    )
    print(f"报告: {os.path.join(report['output_dir'], 'report.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
