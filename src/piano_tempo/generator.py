"""确定性练习序列生成器。

按固定种子生成每个学生若干曲目的逐音事件（右手旋律 / 左手伴奏），
内嵌逐音速度真值、变点与异常片段，并以三种格式版本序列化：

  v1: MIDI 整数音高 + 毫秒时刻 + 按秒采样参考速度曲线
  v2: 音名加八度音高 + 拍时刻 + 按拍采样参考速度曲线
  v3: 音名加八度音高 + 毫秒时刻 + 按拍采样参考速度曲线
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

PITCH_CLASS_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
VOICES = ("rh", "lh")
N_BEATS = 64
SECTION_LEN = 16
NOTE_VALUE_BEATS = 2.0


def midi_to_name(midi: int) -> str:
    return f"{PITCH_CLASS_NAMES[midi % 12]}{midi // 12 - 1}"


@dataclass
class CanonicalNote:
    voice: str
    pitch: int
    nominal_onset: float
    performed_onset: float
    duration_beats: float
    true_bpm: float


def _piece_seed(base_seed: int, student_idx: int, piece_idx: int) -> int:
    return base_seed + 1000 * student_idx + piece_idx


def _build_piece(rng: np.random.Generator) -> dict:
    """构建一首曲目的节奏网格、参考速度曲线与内嵌真值。"""
    base_bpm = int(rng.integers(88, 121))
    grid = np.arange(0.0, N_BEATS + 0.01, NOTE_VALUE_BEATS)

    # 参考（标注）速度曲线：整体平稳，仅在难点段落轻微放缓
    reference = np.full_like(grid, float(base_bpm))
    difficulty = [
        {"section_id": "B", "start_beat": 16.0, "end_beat": 32.0, "label": "hard"},
        {"section_id": "D", "start_beat": 48.0, "end_beat": 64.0, "label": "hard"},
    ]
    for section in difficulty:
        mask = (grid >= section["start_beat"]) & (grid < section["end_beat"])
        reference[mask] = base_bpm * 0.97

    # 真实演奏速度：分段倍率 + 轻微抖动
    true_tempo = np.full_like(grid, float(base_bpm))
    changepoints = [
        {"beat": 12.0, "factor": 1.18, "direction": "accel"},
        {"beat": 28.0, "factor": 0.8, "direction": "ritard"},
        {"beat": 44.0, "factor": 1.12, "direction": "accel"},
    ]
    for cp in sorted(changepoints, key=lambda item: item["beat"]):
        true_tempo[grid >= cp["beat"]] = base_bpm * cp["factor"]
    true_tempo *= 1.0 + rng.normal(0.0, 0.015, size=true_tempo.shape)

    # 节奏偏移段落：相邻音间隔整体拉长 25%
    offset_mask = (grid >= 48.0) & (grid < 56.0)
    onset_scale = np.where(offset_mask, 1.25, 1.0)
    # 真实演奏时刻由真实 BPM 反推：实际间隔 = 时值 / (BPM/60) * 拉伸系数。
    # 累积实际间隔构成真实演奏时间轴，逐音真值即该间隔对应的 BPM。
    performed_ioi = (
        NOTE_VALUE_BEATS / (true_tempo / 60.0) * onset_scale
    )
    performed_time = np.concatenate(
        [[0.0], np.cumsum(performed_ioi[:-1])]
    )
    # 真实异常片段坐标映射到真实演奏时间轴
    def _to_performed(nominal_beat: float) -> float:
        return float(np.interp(nominal_beat, grid, performed_time))

    true_sections = [
        {
            "kind": "tempo_jump",
            "start_beat": _to_performed(12.0),
            "end_beat": _to_performed(16.0),
            "direction": "accel",
            "magnitude": float(base_bpm * 0.18),
        },
        {
            "kind": "tempo_jump",
            "start_beat": _to_performed(28.0),
            "end_beat": _to_performed(32.0),
            "direction": "ritard",
            "magnitude": float(base_bpm * 0.2),
        },
        {
            "kind": "rhythm_offset",
            "start_beat": _to_performed(48.0),
            "end_beat": _to_performed(56.0),
            "direction": "drag",
            "magnitude": 0.25,
        },
    ]
    difficulty = [
        {
            "section_id": item["section_id"],
            "start_beat": _to_performed(item["start_beat"]),
            "end_beat": _to_performed(item["end_beat"]),
            "label": item["label"],
        }
        for item in difficulty
    ]
    # 参考曲线重采样到真实演奏时间轴，保证与真值同坐标
    reference = np.interp(performed_time, grid, reference)

    notes: list[CanonicalNote] = []
    for voice_idx, voice in enumerate(VOICES):
        low, high = (60, 84) if voice == "rh" else (40, 60)
        scale = np.array([0, 2, 4, 5, 7, 9, 11]) + 12 * (low // 12)
        scale = scale[(scale >= low) & (scale <= high)]
        pitch = int(rng.choice(scale))
        for idx, nominal in enumerate(grid):
            pitch = int(
                np.clip(
                    pitch + int(rng.choice([-2, 0, 0, 2, 4, -4])),
                    low,
                    high,
                )
            )
            # 轻微人为抖动，避免完全机械；抖动幅度远小于网格步长
            jitter = float(rng.normal(0.0, 0.01)) if idx > 0 else 0.0
            performed = performed_time[idx] + float(voice_idx) + jitter
            notes.append(
                CanonicalNote(
                    voice=voice,
                    pitch=pitch,
                    nominal_onset=float(nominal + voice_idx),
                    performed_onset=max(0.0, performed),
                    duration_beats=NOTE_VALUE_BEATS,
                    true_bpm=float(true_tempo[idx]),
                )
            )

    practice_dates = [
        f"2026-11-{day:02d}"
        for day in rng.choice(np.arange(2, 27), size=4, replace=False)
    ]
    return {
        "base_bpm": base_bpm,
        "grid": performed_time,
        "reference": reference,
        "true_tempo": true_tempo,
        "notes": notes,
        "difficulty": difficulty,
        "true_sections": true_sections,
        "changepoints": changepoints,
        "practice_dates": sorted(practice_dates),
    }


def _serialize(version: str, piece: dict) -> dict:
    """把规范曲目序列化为三种格式版本之一。"""
    base_bpm = piece["base_bpm"]
    grid = piece["grid"]
    beat_seconds = 60.0 / base_bpm

    events = []
    for note in piece["notes"]:
        event: dict = {
            "voice": note.voice,
            "duration_beats": note.duration_beats,
            "true_bpm": note.true_bpm,
        }
        if version == "v1":
            event["pitch_midi"] = note.pitch
            event["onset_ms"] = round(note.performed_onset * beat_seconds * 1000.0, 3)
        elif version == "v2":
            event["pitch_name"] = midi_to_name(note.pitch)
            event["onset_beat"] = round(note.performed_onset, 6)
        else:  # v3
            event["pitch_name"] = midi_to_name(note.pitch)
            event["onset_ms"] = round(note.performed_onset * beat_seconds * 1000.0, 3)
        events.append(event)

    if version == "v1":
        # 按秒采样参考速度曲线
        end_second = grid[-1] * beat_seconds
        seconds = np.arange(0.0, end_second + 1e-9, 1.0)
        beat_axis = seconds / beat_seconds
        tempo = np.interp(beat_axis, grid, piece["reference"])
        reference_curve = {
            "sampling": "per_second",
            "time_unit": "second",
            "times": [round(float(value), 6) for value in seconds],
            "tempo_bpm": [round(float(value), 6) for value in tempo],
        }
    else:
        # v2 / v3 按拍采样
        reference_curve = {
            "sampling": "per_beat",
            "time_unit": "beat",
            "times": [round(float(value), 6) for value in grid],
            "tempo_bpm": [round(float(value), 6) for value in piece["reference"]],
        }

    return {
        "format_version": version,
        "base_bpm": base_bpm,
        "time_unit": "millisecond" if version != "v2" else "beat",
        "pitch_encoding": "midi" if version == "v1" else "name_octave",
        "practice_dates": piece["practice_dates"],
        "difficulty_sections": piece["difficulty"],
        "reference_curve": reference_curve,
        "truth": {
            "grid_beats": [round(float(value), 6) for value in grid],
            "tempo_bpm": [round(float(value), 6) for value in piece["true_tempo"]],
            "sections": piece["true_sections"],
            "changepoints": piece["changepoints"],
        },
        "events": events,
    }


def generate_dataset(
    seed: int,
    n_students: int,
    pieces_per_student: int,
    output_dir: str | Path,
) -> list[Path]:
    """运行确定性生成器，落盘三版本练习记录，返回文件路径列表。"""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for student_idx in range(n_students):
        student_id = f"S{student_idx + 1:03d}"
        for piece_idx in range(pieces_per_student):
            piece_id = f"P{piece_idx + 1:02d}"
            rng = np.random.default_rng(
                _piece_seed(seed, student_idx, piece_idx)
            )
            piece = _build_piece(rng)
            for version in ("v1", "v2", "v3"):
                payload = {
                    "student_id": student_id,
                    "piece_id": piece_id,
                    **_serialize(version, piece),
                }
                target = output_path / f"{student_id}_{piece_id}_{version}.json"
                target.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                written.append(target)
    return written
