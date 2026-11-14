#!/usr/bin/env python3
"""Seamless loop exporter for 44.1 kHz / 16-bit / mono WAV sound effects.

Reads a manifest JSON, validates every entry, evaluates the candidate seam
points by comparing the head and tail of each audio file, exports the
smoothest loop as a WAV, and writes a machine-readable evaluation report.

Usage:
    python loop_seams.py MANIFEST.json [--report REPORT.json]

Exit codes:
    0  manifest processed (individual entries may still be incomplete/invalid)
    2  fatal manifest structure problem (no partial output is produced)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

try:
    import numpy as np
except ImportError as exc:  # pragma: no cover - friendly startup error
    print(
        "错误: 需要 NumPy。请先运行 `python -m pip install numpy`。",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc


SAMPLE_RATE = 44100
SAMPLE_WIDTH = 2
CHANNELS = 1
# Number of samples compared at the head/tail of the loop.
SEAM_WINDOW = 64
DEFAULT_REPORT_FILENAME = "loop_report.json"

STATUS_EXPORTED = "exported"
STATUS_INCOMPLETE = "incomplete"
STATUS_INVALID = "invalid"
STATUS_LABELS = {
    STATUS_EXPORTED: "完整导出",
    STATUS_INCOMPLETE: "未完成",
    STATUS_INVALID: "无效",
}

# Field aliases keep the parser tolerant of reasonable manifest spellings.
OUTPUT_DIR_KEYS = (
    "output_dir",
    "output_directory",
    "outputDir",
    "out_dir",
    "output_path",
    "destination_dir",
    "输出目录",
)
AUDIO_DIR_KEYS = (
    "audio_dir",
    "wav_dir",
    "wav_directory",
    "source_dir",
    "sounds_dir",
    "audio_root",
    "input_dir",
    "base_dir",
)
ENTRIES_KEYS = (
    "entries",
    "sounds",
    "sound_effects",
    "effects",
    "sfx",
    "items",
    "audio_files",
    "clips",
)
FILENAME_KEYS = (
    "filename",
    "file_name",
    "file",
    "wav_file",
    "wav",
    "audio_file",
    "name",
    "path",
    "文件名",
)
EXPECTED_KEYS = (
    "expected_loop_ms",
    "expected_loop_length_ms",
    "expected_length_ms",
    "expected_loop_duration_ms",
    "loop_length_ms",
    "loop_ms",
    "expected_ms",
)
CANDIDATE_KEYS = (
    "candidate_seams_ms",
    "seam_candidates_ms",
    "candidates_ms",
    "candidate_seam_points_ms",
    "candidate_points_ms",
    "seam_points_ms",
    "seam_positions_ms",
    "candidate_seams",
    "seams_ms",
    "seam_points",
    "候选接缝点",
)


# ---------------------------------------------------------------------------
# Errors / small data types
# ---------------------------------------------------------------------------


class ManifestError(Exception):
    """Fatal problem with the manifest as a whole."""


class AudioProblem(Exception):
    """A per-entry audio problem that maps to an 'incomplete' status."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Manifest:
    path: Path
    output_dir: Path
    audio_dir: Path | None
    entries: list


@dataclass(frozen=True)
class AudioData:
    samples: "np.ndarray"
    sample_rate: int
    channels: int
    sample_width: int
    frames: int

    @property
    def duration_ms(self) -> float:
        return self.frames * 1000.0 / self.sample_rate


# ---------------------------------------------------------------------------
# Manifest parsing / validation
# ---------------------------------------------------------------------------


def _find_field(mapping: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in mapping:
            return True, mapping[key], key
    return False, None, None


def _resolve_path(raw: str, base: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    return path


def load_manifest(manifest_path: Path) -> Manifest:
    """Load the manifest and validate its top-level structure.

    Any problem here is fatal: no output directory is created and no report
    is produced.
    """
    try:
        text = manifest_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ManifestError(f"无法读取清单文件 {manifest_path}: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(
            f"清单不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列）: {exc.msg}"
        ) from exc

    if not isinstance(data, dict):
        raise ManifestError("清单整体结构非法：顶层必须是 JSON 对象")

    has_output, output_raw, output_key = _find_field(data, OUTPUT_DIR_KEYS)
    if not has_output:
        raise ManifestError(f"清单缺少输出目录字段（{OUTPUT_DIR_KEYS[0]}），无法运行")
    if not isinstance(output_raw, str) or not output_raw.strip():
        raise ManifestError(f"输出目录字段 {output_key!r} 不能为空，且必须是字符串")

    base_dir = manifest_path.parent
    output_dir = _resolve_path(output_raw.strip(), base_dir)

    has_audio, audio_raw, audio_key = _find_field(data, AUDIO_DIR_KEYS)
    audio_dir = None
    if has_audio:
        if not isinstance(audio_raw, str) or not audio_raw.strip():
            raise ManifestError(f"音频目录字段 {audio_key!r} 必须是非空字符串")
        audio_dir = _resolve_path(audio_raw.strip(), base_dir)

    has_entries, entries_raw, entries_key = _find_field(data, ENTRIES_KEYS)
    if not has_entries:
        raise ManifestError("清单缺少音效列表字段（例如 entries / sounds / effects）")
    if not isinstance(entries_raw, list):
        raise ManifestError(f"音效列表字段 {entries_key!r} 必须是数组")

    return Manifest(
        path=manifest_path,
        output_dir=output_dir,
        audio_dir=audio_dir,
        entries=entries_raw,
    )


def _base_entry(index: int) -> dict:
    return {
        "index": index,
        "filename": None,
        "expected_loop_ms": None,
        "candidate_seams_ms": None,
        "selected_seam_ms": None,
        "exported_frames": None,
        "exported_file": None,
        "status": None,
        "status_zh": None,
        "reason": None,
        "message": None,
        "seam_difference": None,
        "candidate_evaluations": None,
        "audio_duration_ms": None,
        "source_file": None,
        "validation_errors": [],
    }


def validate_entry(index: int, raw) -> tuple[dict, dict | None]:
    """Validate one manifest entry.

    Returns (report_entry, normalized) where normalized is None when the
    entry cannot be treated as a valid sound effect.
    """
    entry = _base_entry(index)
    errors: list[dict] = []

    def err(code: str, message: str) -> None:
        errors.append({"code": code, "message": message})

    if not isinstance(raw, dict):
        err("entry_not_object", "条目必须是 JSON 对象")
        entry["status"] = STATUS_INVALID
        entry["status_zh"] = STATUS_LABELS[STATUS_INVALID]
        entry["reason"] = errors[0]["code"]
        entry["message"] = errors[0]["message"]
        entry["validation_errors"] = errors
        return entry, None

    # --- filename -------------------------------------------------------
    has_file, file_value, file_key = _find_field(raw, FILENAME_KEYS)
    filename = None
    if not has_file or file_value is None:
        err("missing_filename", f"缺少文件名字段（{FILENAME_KEYS[0]}）")
    elif not isinstance(file_value, str):
        err("invalid_filename_type", f"文件名字段 {file_key!r} 必须是字符串")
    elif not file_value.strip():
        err("empty_filename", "文件名字段不能为空字符串")
    else:
        filename = file_value.strip()
        entry["filename"] = filename

    # --- expected loop length ------------------------------------------
    has_expected, expected_value, expected_key = _find_field(raw, EXPECTED_KEYS)
    expected = None
    if not has_expected or expected_value is None:
        err(
            "missing_expected_loop_ms",
            f"缺少预期循环长度字段（{EXPECTED_KEYS[0]}）；该条目不作为有效音效",
        )
    elif isinstance(expected_value, bool) or not isinstance(expected_value, int):
        err(
            "invalid_expected_loop_ms_type",
            f"预期循环长度 {expected_key!r} 必须是正整数毫秒（当前类型：{type(expected_value).__name__}）",
        )
    elif expected_value <= 0:
        err(
            "invalid_expected_loop_ms_value",
            f"预期循环长度必须为正整数毫秒，当前值：{expected_value}",
        )
    else:
        expected = expected_value
        entry["expected_loop_ms"] = expected

    # --- candidate seam points -----------------------------------------
    has_candidates, candidates_value, candidates_key = _find_field(raw, CANDIDATE_KEYS)
    candidates = None
    if not has_candidates or candidates_value is None:
        err("missing_candidate_seams_ms", f"缺少候选接缝点字段（{CANDIDATE_KEYS[0]}）")
    elif not isinstance(candidates_value, list):
        err(
            "invalid_candidate_seams_type",
            f"候选接缝点 {candidates_key!r} 必须是数组",
        )
    else:
        bad_items = False
        for item_index, item in enumerate(candidates_value):
            if isinstance(item, bool) or not isinstance(item, int):
                err(
                    "invalid_candidate_seam_type",
                    f"候选接缝点[{item_index}] 必须是整数毫秒（当前类型：{type(item).__name__}）",
                )
                bad_items = True
            elif item < 0:
                err(
                    "negative_candidate_seam",
                    f"候选接缝点[{item_index}] 不能为负数：{item}",
                )
                bad_items = True
        if not candidates_value:
            err("empty_candidate_seams", "候选接缝点数组不能为空")
            bad_items = True
        if not bad_items:
            candidates = list(candidates_value)
            entry["candidate_seams_ms"] = candidates

    entry["validation_errors"] = errors
    if errors:
        entry["status"] = STATUS_INVALID
        entry["status_zh"] = STATUS_LABELS[STATUS_INVALID]
        entry["reason"] = errors[0]["code"]
        entry["message"] = "；".join(error["message"] for error in errors)
        return entry, None

    normalized = {
        "filename": filename,
        "expected_loop_ms": expected,
        "candidate_seams_ms": candidates,
    }
    return entry, normalized


# ---------------------------------------------------------------------------
# Audio loading / seam evaluation
# ---------------------------------------------------------------------------


def source_path_for(manifest: Manifest, filename: str) -> Path:
    raw = Path(filename).expanduser()
    if raw.is_absolute():
        return raw
    if manifest.audio_dir is not None:
        return manifest.audio_dir / raw
    return manifest.path.parent / raw


def load_audio(path: Path) -> AudioData:
    """Decode and validate one WAV file, raising AudioProblem on failure."""
    if not path.is_file():
        raise AudioProblem("file_missing", f"音频文件不存在：{path}")

    try:
        wave_file = wave.open(str(path), "rb")
    except (wave.Error, EOFError, OSError, ValueError) as exc:
        raise AudioProblem("decode_error", f"无法按 WAV 解码：{exc}") from exc

    with wave_file:
        try:
            channels = wave_file.getnchannels()
            sample_width = wave_file.getsampwidth()
            sample_rate = wave_file.getframerate()
            frames = wave_file.getnframes()
            comptype = wave_file.getcomptype()
        except (wave.Error, EOFError) as exc:
            raise AudioProblem("decode_error", f"WAV 头信息损坏：{exc}") from exc

        if comptype != "NONE":
            raise AudioProblem(
                "unsupported_encoding",
                f"仅支持未压缩 PCM WAV，当前压缩类型：{comptype}",
            )
        if channels != CHANNELS:
            raise AudioProblem(
                "unsupported_channels",
                f"要求 {CHANNELS} 声道，实际为 {channels} 声道",
            )
        if sample_rate != SAMPLE_RATE:
            raise AudioProblem(
                "unsupported_sample_rate",
                f"要求采样率 {SAMPLE_RATE} Hz，实际为 {sample_rate} Hz",
            )
        if sample_width != SAMPLE_WIDTH:
            raise AudioProblem(
                "unsupported_bit_depth",
                f"要求 16 位（2 字节）样本，实际为 {sample_width * 8} 位",
            )
        if frames <= 0:
            raise AudioProblem("empty_audio", "音频没有任何采样帧")

        try:
            payload = wave_file.readframes(frames)
        except (wave.Error, EOFError) as exc:
            raise AudioProblem("decode_error", f"读取采样数据失败：{exc}") from exc

    expected_bytes = frames * channels * sample_width
    if len(payload) != expected_bytes:
        raise AudioProblem(
            "truncated_audio",
            f"WAV 数据不完整：应有 {expected_bytes} 字节，实际读取 {len(payload)} 字节",
        )

    samples = np.frombuffer(payload, dtype="<i2").copy()
    return AudioData(
        samples=samples,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        frames=frames,
    )


def sample_index_for_ms(ms: int) -> int:
    return int(round(ms * SAMPLE_RATE / 1000.0))


def seam_difference(samples: "np.ndarray", index: int) -> float | None:
    """Return a lower-is-smoother difference score for a candidate seam.

    The score combines the instantaneous head/tail jump (the audible click)
    with the RMS mismatch of the first/last window of samples (waveform
    shape continuity).  An index of 0 has no tail to compare and is
    considered unevaluable.
    """
    n = int(samples.shape[0])
    if index <= 0 or index > n:
        return None
    window = min(SEAM_WINDOW, index, n)
    head = samples[:window].astype(np.int32)
    tail = samples[index - window : index].astype(np.int32)
    rms = float(np.sqrt(np.mean((head - tail) ** 2)))
    jump = abs(int(samples[index - 1]) - int(samples[0]))
    return 2.0 * jump + rms


def choose_seam(
    evaluations: list[dict],
) -> tuple[int | None, int | None, float | None]:
    """Pick the smoothest candidate seam from precomputed evaluations.

    Candidate 0 ms would produce an empty loop and is not selectable; if it
    is the only candidate the entry is reported as incomplete by the caller.
    """
    best = None  # (score, ms, frame)
    for item in evaluations:
        score = item["difference"]
        if score is None:
            continue
        if best is None or (score, item["ms"]) < (best[0], best[1]):
            best = (score, item["ms"], item["frame"])
    if best is None:
        return None, None, None
    return best[1], best[2], best[0]


# ---------------------------------------------------------------------------
# Output / report helpers
# ---------------------------------------------------------------------------


def safe_stem(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"[^\w\-.]+", "_", stem, flags=re.UNICODE)
    stem = stem.strip("._-")
    return stem or "audio"


def output_filename(filename: str, index: int) -> str:
    # The manifest index guarantees that two entries using the same source
    # file (or the same filename text) never overwrite one another.
    return f"{safe_stem(filename)}__loop_{index:03d}.wav"


def write_wav(path: Path, samples: "np.ndarray", audio: AudioData) -> None:
    """Write a mono WAV preserving the source's rate, depth and channels."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        with wave.open(str(temp_path), "wb") as wav_out:
            wav_out.setnchannels(audio.channels)
            wav_out.setsampwidth(audio.sample_width)
            wav_out.setframerate(audio.sample_rate)
            wav_out.writeframes(samples.astype("<i2", copy=False).tobytes())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def mark_invalid(entry: dict, code: str, message: str, **extra) -> None:
    entry.update(
        {
            "status": STATUS_INVALID,
            "status_zh": STATUS_LABELS[STATUS_INVALID],
            "reason": code,
            "message": message,
            "selected_seam_ms": None,
            "exported_frames": None,
            "exported_file": None,
        }
    )
    entry.update(extra)


def mark_incomplete(entry: dict, code: str, message: str, **extra) -> None:
    entry.update(
        {
            "status": STATUS_INCOMPLETE,
            "status_zh": STATUS_LABELS[STATUS_INCOMPLETE],
            "reason": code,
            "message": message,
            "selected_seam_ms": None,
            "exported_frames": None,
            "exported_file": None,
        }
    )
    entry.update(extra)


# ---------------------------------------------------------------------------
# Per-entry processing
# ---------------------------------------------------------------------------


def process_entry(manifest: Manifest, entry: dict, normalized: dict) -> None:
    filename = normalized["filename"]
    expected_ms = normalized["expected_loop_ms"]
    candidates_ms = normalized["candidate_seams_ms"]

    source = source_path_for(manifest, filename)
    entry["source_file"] = str(source)

    try:
        audio = load_audio(source)
    except AudioProblem as exc:
        mark_incomplete(entry, exc.code, exc.message)
        return

    entry["audio_duration_ms"] = round(audio.duration_ms, 6)

    # Requirement 2: a seam point outside the audio duration invalidates the
    # whole entry; it must not be exported.
    out_of_range = []
    for ms in candidates_ms:
        index = sample_index_for_ms(ms)
        if ms > audio.duration_ms + 1e-9 or index > audio.frames:
            out_of_range.append(ms)
    if out_of_range:
        mark_invalid(
            entry,
            "candidate_seam_out_of_range",
            "候选接缝点超出音频时长："
            + ", ".join(str(ms) for ms in out_of_range)
            + f" ms（音频时长 {audio.duration_ms:.3f} ms）",
        )
        return

    # Requirement 10: an entirely silent file must not be exported as a
    # successful loop.
    if not np.any(audio.samples):
        mark_incomplete(
            entry,
            "silent_audio",
            "整段音频为静音（全部样本为零），不能作为可循环音效导出",
        )
        return

    # Requirement 9: the source must cover the expected loop length.
    if audio.frames * 1000 < expected_ms * audio.sample_rate:
        mark_incomplete(
            entry,
            "audio_too_short",
            f"音频时长 {audio.duration_ms:.3f} ms 不足预期循环长度 {expected_ms} ms",
        )
        return

    evaluations = []
    for ms in candidates_ms:
        index = sample_index_for_ms(ms)
        score = seam_difference(audio.samples, index)
        evaluations.append(
            {
                "ms": ms,
                "frame": index,
                "difference": None if score is None else round(float(score), 6),
            }
        )
    entry["candidate_evaluations"] = evaluations

    selected_ms, frames, score = choose_seam(evaluations)
    if selected_ms is None or frames is None or frames <= 0:
        mark_incomplete(
            entry,
            "zero_length_loop",
            "候选接缝点均无法产生非空循环（0 ms 接缝不导出）",
        )
        return

    exported_file = output_filename(filename, entry["index"] + 1)
    destination = manifest.output_dir / exported_file
    try:
        write_wav(destination, audio.samples[:frames], audio)
    except (OSError, wave.Error) as exc:
        mark_incomplete(entry, "export_failed", f"导出 WAV 失败：{exc}")
        return

    entry.update(
        {
            "status": STATUS_EXPORTED,
            "status_zh": STATUS_LABELS[STATUS_EXPORTED],
            "reason": None,
            "message": "已导出无缝循环",
            "selected_seam_ms": selected_ms,
            "exported_frames": frames,
            "exported_file": exported_file,
            "seam_difference": round(float(score), 6),
        }
    )


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


def build_report(manifest: Manifest, entries: list[dict]) -> dict:
    counts = {key: 0 for key in STATUS_LABELS}
    for entry in entries:
        counts[entry["status"]] += 1
    return {
        "tool": "loop_seams.py",
        "manifest": str(manifest.path),
        "output_dir": str(manifest.output_dir),
        "summary": {
            "total": len(entries),
            "exported": counts[STATUS_EXPORTED],
            "incomplete": counts[STATUS_INCOMPLETE],
            "invalid": counts[STATUS_INVALID],
            "labels": STATUS_LABELS,
        },
        "entries": entries,
    }


def print_summary(report: dict, report_path: Path) -> None:
    summary = report["summary"]
    print("=== 无缝循环导出摘要 ===")
    print(f"清单: {report['manifest']}")
    print(f"输出目录: {report['output_dir']}")
    print(
        "统计: 总条目 {total} | 完整导出 {exported} | 未完成 {incomplete} | 无效 {invalid}".format(
            **summary
        )
    )
    for entry in report["entries"]:
        label = STATUS_LABELS[entry["status"]]
        name = entry["filename"] or f"条目#{entry['index'] + 1}"
        if entry["status"] == STATUS_EXPORTED:
            print(
                f"  [完整导出] {name} -> {entry['exported_file']}"
                f"（接缝 {entry['selected_seam_ms']} ms，{entry['exported_frames']} 帧）"
            )
        else:
            print(f"  [{label}] {name}：{entry['message']}")
    print(f"报告: {report_path}")


def run(manifest_path: Path, report_override: Path | None) -> int:
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    try:
        manifest.output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"错误: 无法创建输出目录 {manifest.output_dir}: {exc}", file=sys.stderr)
        return 2
    if not manifest.output_dir.is_dir():
        print(
            f"错误: 输出目录不是目录：{manifest.output_dir}",
            file=sys.stderr,
        )
        return 2

    report_path = (
        report_override
        if report_override is not None
        else manifest.output_dir / DEFAULT_REPORT_FILENAME
    )

    entries: list[dict] = []
    for index, raw_entry in enumerate(manifest.entries):
        report_entry, normalized = validate_entry(index, raw_entry)
        if normalized is not None:
            try:
                process_entry(manifest, report_entry, normalized)
            except Exception as exc:  # never abort the batch on one entry
                mark_incomplete(
                    report_entry,
                    "unexpected_error",
                    f"处理该条目时发生意外错误：{type(exc).__name__}: {exc}",
                )
        entries.append(report_entry)

    report = build_report(manifest, entries)

    try:
        write_report(report_path, report)
    except OSError as exc:
        print(f"错误: 无法写入报告 {report_path}: {exc}", file=sys.stderr)
        return 2

    print_summary(report, report_path)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="评估循环音效的候选接缝点并导出无缝 WAV。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("manifest", type=Path, help="音效清单 JSON 路径")
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help=f"评估报告输出路径（默认：<output_dir>/{DEFAULT_REPORT_FILENAME}）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run(args.manifest, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
