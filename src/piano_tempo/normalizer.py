"""三个格式版本练习序列的归一化读取器。

统一口径：
  * 音高：MIDI 整数与音名加八度统一为 MIDI 编号；
  * 时刻：毫秒与拍统一换算为以拍为单位的浮点时刻（拍速取自记录元数据）；
  * 参考速度曲线：按秒 / 按拍采样统一插值到等间隔拍网格；
  * 保留声部标记与音符时值。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .models import NoteEvent, NormalizedRecord

PITCH_CLASS_TO_INDEX = {
    "C": 0, "C#": 1, "DB": 1, "D": 2, "D#": 3, "EB": 3,
    "E": 4, "FB": 4, "F": 5, "F#": 6, "GB": 6, "G": 7,
    "G#": 8, "AB": 8, "A": 9, "A#": 10, "BB": 10, "B": 11,
}
INDEX_TO_PITCH_CLASS = [
    "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B",
]


def name_to_midi(pitch_name: str) -> int:
    """音名加八度（如 C#4、Bb3）转 MIDI 整数。"""
    text = pitch_name.strip().upper()
    if len(text) < 2:
        raise ValueError(f"非法音名: {pitch_name}")
    octave = int(text[-1])
    pitch_class = PITCH_CLASS_TO_INDEX[text[:-1]]
    return (octave + 1) * 12 + pitch_class


def midi_to_name(midi: int) -> str:
    return f"{INDEX_TO_PITCH_CLASS[midi % 12]}{midi // 12 - 1}"


def _unify_pitch(event: dict) -> int:
    if "pitch_midi" in event:
        return int(event["pitch_midi"])
    return name_to_midi(event["pitch_name"])


def _unify_onset(event: dict, time_unit: str, base_bpm: float) -> float:
    if "onset_beat" in event:
        return float(event["onset_beat"])
    if "onset_ms" in event:
        if time_unit != "millisecond":
            raise ValueError("毫秒时刻记录的 time_unit 元数据应为 millisecond")
        return float(event["onset_ms"]) / 1000.0 * base_bpm / 60.0
    raise KeyError("事件缺少起始时刻字段")


def _unify_reference_curve(
    reference_curve: dict,
    base_bpm: float,
    grid_step_beats: float,
    end_beat: float,
) -> dict[str, np.ndarray]:
    times = np.asarray(reference_curve["times"], dtype=float)
    tempos = np.asarray(reference_curve["tempo_bpm"], dtype=float)
    if reference_curve["time_unit"] == "second":
        beat_times = times * base_bpm / 60.0
    else:
        beat_times = times
    order = np.argsort(beat_times)
    beat_times, tempos = beat_times[order], tempos[order]

    grid = np.arange(0.0, end_beat + grid_step_beats * 0.5, grid_step_beats)
    grid = grid[grid <= end_beat + 1e-9]
    interpolated = np.interp(grid, beat_times, tempos)
    return {"grid_beats": grid, "tempo_bpm": interpolated}


def normalize_record(payload: dict, grid_step_beats: float) -> NormalizedRecord:
    """把单条三版本记录字典归一化为统一逐音事件序列对象。"""
    base_bpm = float(payload["base_bpm"])
    time_unit = payload.get("time_unit", "beat")

    events: list[NoteEvent] = []
    true_bpm_by_voice: dict[str, list[float]] = {"rh": [], "lh": []}
    for raw in payload["events"]:
        pitch = _unify_pitch(raw)
        onset = _unify_onset(raw, time_unit, base_bpm)
        voice = str(raw["voice"])
        events.append(
            NoteEvent(
                pitch=pitch,
                pitch_name=midi_to_name(pitch),
                onset_beat=onset,
                duration_beats=float(raw["duration_beats"]),
                voice=voice,
            )
        )
        true_bpm_by_voice[voice].append(float(raw.get("true_bpm", np.nan)))

    events.sort(key=lambda item: (item.voice, item.onset_beat))
    end_beat = max(event.onset_beat for event in events)
    reference = _unify_reference_curve(
        payload["reference_curve"], base_bpm, grid_step_beats, end_beat
    )

    truth = payload.get("truth", {})
    true_grid = np.asarray(truth.get("grid_beats", []), dtype=float)
    true_tempo = np.asarray(truth.get("tempo_bpm", []), dtype=float)
    true_note_tempo: dict[str, np.ndarray] = {}
    for voice in ("rh", "lh"):
        voice_events = [event for event in events if event.voice == voice]
        if true_grid.size and true_tempo.size:
            interpolated = np.interp(
                [event.onset_beat for event in voice_events],
                true_grid,
                true_tempo,
            )
            true_note_tempo[voice] = interpolated
        else:
            true_note_tempo[voice] = np.asarray(true_bpm_by_voice[voice])

    return NormalizedRecord(
        student_id=payload["student_id"],
        piece_id=payload["piece_id"],
        format_version=payload["format_version"],
        base_bpm=base_bpm,
        practice_dates=list(payload.get("practice_dates", [])),
        difficulty_sections=list(payload.get("difficulty_sections", [])),
        events=events,
        reference_curve=reference,
        true_note_tempo=true_note_tempo,
        true_sections=list(truth.get("sections", [])),
        truth_grid=true_grid,
        truth_tempo=true_tempo,
    )


def load_record(path: str | Path, grid_step_beats: float) -> NormalizedRecord:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return normalize_record(payload, grid_step_beats)
